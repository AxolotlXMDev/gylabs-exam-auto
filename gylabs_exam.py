#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
广石化 实验室安全教育与考试系统 —— 题库缓存 + 自动做题（纯 HTTP，不依赖浏览器页面操作）

用法
----
1) 登录一次，把会话 Cookie 存下来（验证码需要人眼看一眼）
python gylabs_exam.py login --user 你的学号 --password '你的密码' --cookie-jar cookie.txt
   （不想跑登录也可以： --cookie "JSESSIONID=xxxxxxxx"）

2) 累积题库（自测不限次数，跑几轮即可覆盖整套题库；可反复执行增量更新）
     python gylabs_exam.py bank --cookie-jar cookie.txt --self-test 16770044 --runs 4 --bank bank.json
   也可顺手抓「院系题库/知识点题库」浏览页（注意：这些库不一定等于考试卷的题池）
     python gylabs_exam.py bank --cookie-jar cookie.txt --dept 447555 --bank bank.json

3) 自动做考试（默认答完直接交卷；--dry-run 只答题不交卷）
     python gylabs_exam.py exam --cookie-jar cookie.txt --bank bank.json --exam 16385289
     python gylabs_exam.py exam --cookie-jar cookie.txt --bank bank.json --exam 16385289 --dry-run

关键结论（实测）
----------------
* 题干是图片：\\upload\\theme_picture\\<md5>.jpeg，md5 就是内容文件名，跨场次稳定。
* 选项每次会打乱 → 答案必须按「选项文本」匹配，不能记字母。
* 自测(exameid=16770044)不限次：每交卷一次就多一份带答案的回顾页，
  把当次逐页图片 md5 与回顾页题号对齐，就能得到 md5 -> 正确答案，几轮即可覆盖题库。
* 交卷回顾页（previewMinePaperShow）会给出整卷的题干文本、选项、参考答案，是核对/扩展题库的金矿。
"""

import argparse
import concurrent.futures
import http.cookiejar
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE = "https://gylabs.gdupt.edu.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

EXAM_ACTION = BASE + "/exam/studentexame/studentExameAction.do"
TEST_ACTION = BASE + "/exam/studenttest/studentTestAction.do"
SCORE_ACTION = BASE + "/exam/studentscore/studentScoreAction.do"

ANSWER_INPUT_RE = re.compile(
    r'<input[^>]*type\s*=\s*"?(?:radio|checkbox)"?[^>]*name\s*=\s*"?(answerradio|answercheckbox|answer_state)"?[^>]*>',
    re.I)
INPUT_TAG_RE = re.compile(r'<input\b[^>]*>', re.I)

# 请求起步节奏：多个线程同时握手容易被服务端/中间设备掐断（SSL EOF / 读超时），
# 用全局最小间隔把请求错开。
PACE = 0.12
_RATE_LOCK = threading.Lock()
_RATE_NEXT = [0.0]


def _pace():
    with _RATE_LOCK:
        now = time.time()
        wait = _RATE_NEXT[0] - now
        if wait > 0:
            time.sleep(wait)
        _RATE_NEXT[0] = max(now, _RATE_NEXT[0]) + PACE


# --------------------------------------------------------------------------- http

class Session:
    """极简会话：手动维护 Cookie。

    该站的 JSESSIONID 带 secure/httponly 又没有 Domain，标准 cookiejar 会把它丢掉，
    所以这里直接抓 Set-Cookie 再手动回填。
    """

    def __init__(self, cookie_jar=None, cookie_header=None):
        self.cookies = {}
        self.path = cookie_jar
        self._lock = threading.Lock()
        # 站点的部分入口会校验 Referer；同时保留最终响应 URL，
        # 这样列表页 → 确认页 → 试卷页的请求链与浏览器一致。
        self.last_url = None
        self.last_html = ""
        if cookie_header:
            for part in cookie_header.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    self.cookies[k.strip()] = v.strip()
        elif cookie_jar and os.path.exists(cookie_jar):
            with open(cookie_jar, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        self.cookies[k.strip()] = v.strip()

    def header(self):
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    def absorb(self, headers):
        with self._lock:
            all_headers = []
            for name in ("Set-Cookie", "Set-Cookie2"):
                all_headers.extend(headers.get_all(name) or [])
            for raw in all_headers:
                first = raw.split(";")[0].strip()
                if "=" in first:
                    k, v = first.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if not k:
                        continue
                    # 服务器用空值/Max-Age=0 清理 Cookie 时同步删除本地值。
                    if not v or re.search(r"(?:^|;)\s*max-age\s*=\s*0(?:;|$)", raw, re.I):
                        self.cookies.pop(k, None)
                    else:
                        self.cookies[k] = v

    def save(self):
        if not self.path:
            return
        with open(self.path, "w", encoding="utf-8") as fh:
            for k, v in self.cookies.items():
                fh.write("%s=%s\n" % (k, v))


def make_opener(cookie_jar=None, cookie_header=None):
    return Session(cookie_jar, cookie_header)


class _SessionRedirectHandler(urllib.request.HTTPRedirectHandler):
    """在 urllib 自动跟随 30x 时也保存中间响应下发的 Cookie。"""

    def __init__(self, session):
        super().__init__()
        self.session = session

    def _save(self, headers):
        self.session.absorb(headers)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # 将刚从 30x 响应中收到的路由/会话 Cookie 带到下一跳。
            if self.session.cookies:
                redirected.headers["Cookie"] = self.session.header()
            redirected.headers["Referer"] = req.full_url
        return redirected

    def http_error_301(self, req, fp, code, msg, headers):
        self._save(headers)
        return super().http_error_301(req, fp, code, msg, headers)

    def http_error_302(self, req, fp, code, msg, headers):
        self._save(headers)
        return super().http_error_302(req, fp, code, msg, headers)

    def http_error_303(self, req, fp, code, msg, headers):
        self._save(headers)
        return super().http_error_303(req, fp, code, msg, headers)

    def http_error_307(self, req, fp, code, msg, headers):
        self._save(headers)
        return super().http_error_307(req, fp, code, msg, headers)

    def http_error_308(self, req, fp, code, msg, headers):
        self._save(headers)
        return super().http_error_308(req, fp, code, msg, headers)


def _request(session, url, data=None, tries=3):
    if isinstance(data, (dict, list)):
        body = urllib.parse.urlencode(data).encode()
    else:
        body = data
    last = None
    for i in range(tries):
        try:
            _pace()
            headers = {"User-Agent": UA,
                       "Referer": session.last_url or BASE + "/",
                       "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
            if session.cookies:
                headers["Cookie"] = session.header()
            req = urllib.request.Request(url, data=body, headers=headers)
            opener = urllib.request.build_opener(_SessionRedirectHandler(session))
            with opener.open(req, timeout=30) as resp:
                raw = resp.read()
                session.absorb(resp.headers)
                session.last_url = resp.geturl()
            for enc in ("utf-8", "gb18030"):
                try:
                    text = raw.decode(enc)
                    session.last_html = text
                    return text
                except UnicodeDecodeError:
                    continue
            text = raw.decode("utf-8", "replace")
            session.last_html = text
            return text
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                print("    ！请求失败，%.1fs 后重试（%s）" % (1.0 + i, exc))
            time.sleep(1.0 + i)
    raise RuntimeError("请求失败 %s: %s" % (url, last))


def get(session, url, tries=3):
    return _request(session, url, tries=tries)


def post(session, url, data, tries=3):
    return _request(session, url, data=data, tries=tries)


def save_cookies(session, path=None):
    session.save()


def get_bytes(session, url, tries=3):
    """下载二进制资源（验证码图片）。"""
    last = None
    for i in range(tries):
        try:
            _pace()
            headers = {"User-Agent": UA,
                       "Referer": session.last_url or BASE + "/",
                       "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
            if session.cookies:
                headers["Cookie"] = session.header()
            req = urllib.request.Request(url, headers=headers)
            opener = urllib.request.build_opener(_SessionRedirectHandler(session))
            with opener.open(req, timeout=30) as resp:
                data = resp.read()
                session.absorb(resp.headers)
                session.last_url = resp.geturl()
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 + i)
    raise RuntimeError("下载失败 %s: %s" % (url, last))


def login_expired(html):
    """识别站点返回的统一登录失效页。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.I | re.S)
    text = re.sub(r"\s+", " ", clean_block(text))
    markers = (
        "登录已经失效", "登录已失效", "登录超时", "登录提示",
        "立即重新登录", "账号在其他地方登录", "一个账号只能登录一次",
    )
    return any(marker in text for marker in markers)


def whoami(session):
    """返回已登录用户名；未登录/失效返回 None。"""
    try:
        html = get(session, BASE + "/userOperation.do?ms=afterLogin", tries=1)
    except Exception:  # noqa: BLE001
        return None
    if login_expired(html):
        return None
    if "您好" not in html:
        return None
    m = re.search(r'用户\s*([^，,<\s]{1,20})', html)
    return m.group(1) if m else "?"


def require_login(session, cookie_hint):
    print("检查登录状态…（Cookie 文件：%s）" % cookie_hint)
    name = whoami(session)
    if name:
        print("已登录：%s" % name)
        return True
    print("!! Cookie 无效或已过期，无法登录系统。")
    print("   该站一个账号只能保留一个会话；浏览器重新登录/点击考试可能会把旧 Cookie 踢下线。")
    print("   请在当前仍登录的浏览器会话中复制最新 Cookie，写进 %s（内容形如 JSESSIONID=xxxx）。" % cookie_hint)
    print("   或改用： --cookie \"JSESSIONID=xxxx\"")
    return False


# --------------------------------------------------------------------------- parsing

def attrs(tag):
    """解析 HTML 标签属性，兼容双引号、单引号和未加引号的值。"""
    out = {}
    pattern = r'''([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))'''
    for m in re.finditer(pattern, tag):
        out[m.group(1).lower()] = next(value for value in m.groups()[1:] if value is not None)
    return out


def clean_label(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'^[A-D][、.,：:]\s*', '', text).strip()


def parse_hidden(html):
    """表单里的隐藏域（exameid / pageNum / paperid / answerRealtimeId ...）。"""
    out = {}
    for tag in INPUT_TAG_RE.findall(html):
        a = attrs(tag)
        if a.get("type", "").lower() == "hidden" and a.get("name"):
            out[a["name"]] = a.get("value", "")
    return out


FORM_RE = re.compile(r'<form\b([^>]*)>(.*?)</form\s*>', re.I | re.S)
BUTTON_RE = re.compile(r'<button\b([^>]*)>(.*?)</button\s*>', re.I | re.S)


def _paper_page(html):
    """判断响应是否已经是可答题的试卷页。"""
    return "exameform" in html.lower() and bool(question_md5(html))


def _control_label(tag, body=""):
    """取按钮/提交控件上显示给用户的文字。"""
    a = attrs(tag)
    return clean_label(a.get("value", "") or body)


def _confirmation_label(text):
    """返回文本是否像“确认进入考试”按钮。"""
    text = clean_label(text).lower()
    if not text:
        return False
    if re.search(r"取消|返回|退出|否|cancel|no\b", text, re.I):
        return False
    return bool(re.search(r"确认|确定|进入|开始|继续|同意|是|confirm|sure|start|continue|yes\b", text, re.I))


def _confirmation_request(html, exameid, current_url):
    """从进入考试的确认页提取下一次请求。

    不同考试配置生成的确认页并不完全相同：有的用 form 提交，有的用
    ``location.href``，还有的只放一个带“确定/进入”文字的链接。这里仅
    跟随明确指向考试 action 的确认控件，避免把普通错误页再次提交。
    返回 ``(method, url, data)``，找不到确认控件时返回 ``None``。
    """
    exam_id = str(exameid)

    def target_url(action, ensure_exam_id=True):
        if not action or action.lower().startswith("javascript:"):
            return None
        url = urllib.parse.urljoin(current_url, action)
        parsed = urllib.parse.urlsplit(url)
        if "studentExameAction.do" not in parsed.path:
            return None
        # 某些确认页的 action 没带考试 id，沿用当前命令的 id。
        if ensure_exam_id and "exameid=" not in parsed.query.lower():
            sep = "&" if parsed.query else "?"
            url += sep + urllib.parse.urlencode({"exameid": exam_id})
        return url

    def form_pairs(body, submit_tag=None):
        pairs = []
        for tag in INPUT_TAG_RE.findall(body):
            a = attrs(tag)
            name = a.get("name")
            typ = a.get("type", "text").lower()
            if not name or typ in ("button", "reset", "file", "image", "submit"):
                continue
            if typ in ("checkbox", "radio") and "checked" not in tag.lower():
                continue
            pairs.append((name, a.get("value", "")))
        if submit_tag is not None:
            a = attrs(submit_tag)
            if a.get("name"):
                pairs.append((a["name"], a.get("value", "")))
        return pairs

    # 优先处理 form。只自动提交带明确确认文字的 submit/button，避免误点“取消”。
    for form_attrs, body in FORM_RE.findall(html):
        form_action = attrs(form_attrs).get("action", "")
        controls = []
        for tag in INPUT_TAG_RE.findall(body):
            a = attrs(tag)
            typ = a.get("type", "").lower()
            onclick = a.get("onclick", "").lower()
            if typ == "submit" or (typ == "button" and _confirmation_label(a.get("value", ""))
                                    and (form_action or re.search(
                                        r"submit|location|goto|enter|start|begin|confirm|sure",
                                        onclick))):
                controls.append((tag, ""))
        controls.extend((tag, inner) for tag, inner in BUTTON_RE.findall(body)
                        if attrs(tag).get("type", "submit").lower() not in ("button", "reset")
                        or (attrs(tag).get("type", "").lower() == "button"
                            and _confirmation_label(_control_label(tag, inner))
                            and (form_action or re.search(
                                r"submit|location|goto|enter|start|begin|confirm|sure",
                                attrs(tag).get("onclick", "").lower()))))
        selected = next(((tag, inner) for tag, inner in controls
                         if _confirmation_label(_control_label(tag, inner))), None)
        form_text = clean_label(body)
        if selected is None and len(controls) == 1 and _confirmation_label(form_text):
            selected = controls[0]
        if selected is None:
            continue
        action = form_action
        # 表单通常把 exameid 放在 hidden input 中。该站点的 gotoExame
        # 对“URL 查询串 + POST 同名字段”的组合会误回登录失效页，
        # 所以保留 action 原样，只通过表单字段传 exam id。
        url = target_url(action, ensure_exam_id=False) if action else target_url(current_url)
        if not url:
            continue
        submit_tag, _ = selected
        pairs = form_pairs(body, submit_tag)
        query = urllib.parse.urlsplit(url).query.lower()
        if (not any(k.lower() in ("exameid", "examid") for k, _ in pairs)
                and "exameid=" not in query and "examid=" not in query):
            pairs.append(("exameid", exam_id))
        method = attrs(form_attrs).get("method", "post").lower()
        return method, url, pairs

    # 其次处理确认链接或 onclick/location 跳转。
    link_re = re.compile(r'<a\b([^>]*)>(.*?)</a\s*>', re.I | re.S)
    for link_attrs, body in link_re.findall(html):
        if not _confirmation_label(body):
            continue
        url = target_url(attrs(link_attrs).get("href", ""))
        if url:
            return "get", url, None

    # 老页面常把确认写成 JavaScript 的 location.href/location.replace。
    js_re = re.compile(
        r'''(?:location(?:\.href)?|window\.location(?:\.href)?|location\.replace)\s*\(?(?:\s*=\s*)?['"]([^'"]+)['"]''',
        re.I)
    for action in js_re.findall(html):
        url = target_url(action)
        if url:
            return "get", url, None
    return None


def question_md5(html):
    m = re.findall(r'<img[^>]*src\s*=\s*"([^"]+)"', html, re.I)
    for src in m:
        mm = re.search(r'([0-9a-f]{16,})\.jpe?g$', src, re.I)
        if mm:
            return mm.group(1)
    return None


def question_type(html):
    m = re.search(r'(单选题|多选题|判断题|填空题|问答题|综合题)', html)
    return m.group(1) if m else ""


def parse_options(html):
    """返回 {letter: label_text}，以及答案输入框的 name。"""
    opts, name = {}, None
    for m in ANSWER_INPUT_RE.finditer(html):
        tag = m.group(0)
        a = attrs(tag)
        fname = a.get("name", "")
        if fname == "answer_state":
            continue
        name = fname
        tail = html[m.end():m.end() + 400]
        tail = tail.split("<")[0]
        opts[a.get("value", "")] = clean_label(tail)
    return opts, name


def parse_page(html):
    hidden = parse_hidden(html)
    opts, name = parse_options(html)
    page_no = int(hidden.get("pageNum") or 0)
    if not page_no:
        # 首次进入时的 pageNum 隐藏域是空的，从题号单元格兜底
        mn = re.search(r'<td[^>]*>\s*(\d{1,3})\s*\.\s*</td>', html)
        if mn:
            page_no = int(mn.group(1))
    return {
        "page": page_no,
        "type": question_type(html),
        "md5": question_md5(html),
        "options": opts,
        "answer_field": name or ("answercheckbox" if question_type(html) == "多选题" else "answerradio"),
        "hidden": hidden,
    }


def parse_review(html):
    """解析交卷回顾页：题号 / 题干文本 / 选项 / 参考答案。"""
    out = []
    # 每道题都以 <tr align="left"> 开头，题号在紧随其后的 <td> 里
    parts = re.split(r'<tr[^>]*align\s*=\s*"left"[^>]*>', html)[1:]
    for idx, part in enumerate(parts, 1):
        text = clean_block(part)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        mn = re.match(r'^(\d{1,3})\.?$', lines[0])
        n = int(mn.group(1)) if mn else idx
        stem = lines[1] if len(lines) > 1 else ""
        opts, seen = [], set()
        for ln in lines[1:]:
            if "参考答案" in ln or "考生答案" in ln:
                break
            om = re.match(r'^([A-D])[\s、.．]{1,3}(\S.*)$', ln)
            if om and om.group(1) not in seen:
                seen.add(om.group(1))
                opts.append({"letter": om.group(1), "text": om.group(2).strip()})
        rm = re.search(r'参考答案[：:]\s*(\S+)', text)
        out.append({
            "n": n,
            "stem": stem,
            "options": opts,
            "ref": (rm.group(1) if rm else "").replace(" ", ""),
        })
    return out


def clean_block(html):
    text = re.sub(r'<(br|/tr|/td|/div|/p)[^>]*>', "\n", html, flags=re.I)
    text = re.sub(r'<[^>]+>', "", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = re.sub(r'[ \t\u3000]+', " ", text)
    text = re.sub(r'\n\s*\n+', "\n", text)
    return text.strip()


def readable(html, limit=200):
    """把一页 HTML 压成一句人话（含 <style>/<script> 先去掉），用于报错提示。"""
    txt = re.sub(r'<(style|script)[^>]*>.*?</\1>', ' ', html, flags=re.I | re.S)
    txt = re.sub(r'\s+', ' ', clean_block(txt))
    return txt.strip()[:limit]


def norm(text):
    return re.sub(r'\s+', '', clean_label(text))


def signature(options):
    return "|".join(sorted(norm(v) for v in options_map(options).values()))


def options_map(options):
    """统一选项格式：
    * 试卷页解析出来是 {'A': '文本', ...}
    * 整卷回顾页解析出来是 [{'letter': 'A', 'text': '文本'}, ...]
    两种都要能入库/比对。
    """
    if isinstance(options, dict):
        return dict(options)
    out = {}
    for opt in options or []:
        if isinstance(opt, dict) and opt.get("letter"):
            out[opt["letter"]] = opt.get("text", "")
    return out


# --------------------------------------------------------------------------- bank

def bank_load(path):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "by_md5" in data and "by_sig" in data:
                return data
            print("!! %s 结构不对（可能被写坏），本次先用空题库" % path)
        except Exception as exc:  # noqa: BLE001
            print("!! 读取 %s 失败：%s（本次先用空题库）" % (path, exc))
    return {"by_md5": {}, "by_sig": {}}


def bank_save(bank, path):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bank, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)          # 原子写，避免中途被打断写坏题库


def bank_add(bank, md5, page):
    """把一道题（含正确答案字母）写进题库；答案同时按选项文本保存，避免选项乱序。"""
    if not md5:
        return False
    ref = page.get("ref") or ""
    opts = options_map(page.get("options"))
    correct = []
    if opts:
        for letter in ref:
            if letter in opts:
                correct.append(norm(opts[letter]))
        sig = signature(opts)
        if correct and sig not in bank["by_sig"]:
            bank["by_sig"][sig] = correct
    entry = {"type": page.get("type", ""), "ref": ref, "correct": correct}
    if bank["by_md5"].get(md5) == entry:
        return False
    bank["by_md5"][md5] = entry
    return True


def bank_resolve(bank, page):
    """返回要勾选的字母列表（判断题为 A=对 / B=错）。"""
    md5 = page.get("md5")
    opts = options_map(page.get("options"))
    entry = bank["by_md5"].get(md5) if md5 else None
    if entry:
        if page["type"] == "判断题":
            if entry.get("ref") == "对":
                return ["A"]
            if entry.get("ref") == "错":
                return ["B"]
        letters = [l for l, t in opts.items() if norm(t) in entry.get("correct", [])]
        if letters:
            return sorted(letters)
    if page["type"] == "判断题" or not opts:
        return None
    correct = bank["by_sig"].get(signature(opts))
    if correct:
        letters = [l for l, t in opts.items() if norm(t) in correct]
        if letters:
            return sorted(letters)
    return None


# --------------------------------------------------------------------------- flows

def fetch_paper_page(opener, exameid):
    """进入考试/自测，返回第 1 页 HTML。

    部分考试在真正的试卷页前增加“确认进入考试”页。浏览器会自动按该
    页的 form/链接/脚本继续请求，纯 HTTP 请求也需要复现这一步；否则
    ``do_exam`` 会把确认页误报成“没有考试权限”。
    """
    url = "%s?ms=beforeGotoExame&exameid=%s&TimeStemp=%f" % (EXAM_ACTION, exameid, random.random())
    html = get(opener, url)
    for _ in range(3):
        if _paper_page(html):
            return html
        request = _confirmation_request(html, exameid, url)
        if not request:
            return html
        method, next_url, data = request
        if method == "get":
            if data:
                query = urllib.parse.urlencode(data, doseq=True)
                next_url += ("&" if "?" in next_url else "?") + query
            html = get(opener, next_url)
        else:
            html = post(opener, next_url, data)
        url = next_url
    return html


def save_and_next(opener, html, page, letters, next_page, tries=3):
    data = dict(page["hidden"])
    data["pageNum"] = str(next_page)
    data.setdefault("zc", "")
    for letter in letters:
        data[page["answer_field"]] = letter if len(letters) == 1 else data.get(page["answer_field"], "")
    if len(letters) > 1:  # 多选：同名参数重复提交
        pairs = [(k, v) for k, v in data.items() if k != page["answer_field"]]
        pairs += [(page["answer_field"], l) for l in letters]
        return post(opener, EXAM_ACTION + "?ms=saveAnswerAndGetNextExameTheme", pairs, tries=tries)
    return post(opener, EXAM_ACTION + "?ms=saveAnswerAndGetNextExameTheme", data, tries=tries)


def submit_paper(opener, html, page, letters):
    pairs = [(k, v) for k, v in page["hidden"].items()]
    if page["answer_field"]:
        pairs += [(page["answer_field"], l) for l in letters]
    return post(opener, EXAM_ACTION + "?ms=submitPaperComputeGrade", pairs)


def do_exam(opener, bank, exameid, dry_run=False, max_pages=100):
    html = fetch_paper_page(opener, exameid)
    if login_expired(html):
        raise RuntimeError(
            "进入考试时当前会话已失效（通常是浏览器/其他脚本重新登录后把此 Cookie 踢下线）。\n"
            "    请从当前仍登录的浏览器会话重新复制 Cookie，再重试；列表页曾成功不代表考试入口仍使用同一会话。"
        )
    if "exameform" not in html or not parse_page(html)["md5"]:
        raise RuntimeError("进不去这场考试，服务端返回的不是试卷页。\n    服务端提示：%s\n"
                           "    （常见原因：不在该考试的考生范围内 / 次数用尽 / 考试已结束；"
                           "可用 `list` 看这场考试是否还在你可参加的列表里）" % readable(html))
    unknown = []
    for p in range(1, max_pages + 1):
        page = parse_page(html)
        if p > 1 and not page["md5"]:
            print("  试卷已到末页，停止继续请求。")
            break
        if page["page"] and page["page"] != p:
            print("  [警告] 期望第 %d 页，服务器返回第 %s 页" % (p, page["page"]))
        letters = bank_resolve(bank, page)
        label = "第%3d页 %s md5=%s" % (p, page["type"], (page["md5"] or "?")[:8])
        if letters is None:
            unknown.append(p)
            print("  %s -> 题库里没有，跳过（需人工）" % label)
        else:
            print("  %s -> %s" % (label, "".join(letters)))
        if dry_run:
            if p == max_pages:
                break
            html = save_and_next(opener, html, page, letters or [], p + 1)
            continue
        if p == max_pages:
            # 先保存末页，再交卷（末页直接交卷历史上有丢答案的情况）
            save_and_next(opener, html, page, letters or [], max_pages)
            html = post(opener, EXAM_ACTION + "?ms=saveAnswerAndGetNextExameTheme",
                        dict(list(page["hidden"].items()) + [("pageNum", str(max_pages)), ("zc", "1")]))
            return submit_paper(opener, html, page, letters or [])
        html = save_and_next(opener, html, page, letters or [], p + 1)
    return None


BANK_ROW_RE = (r'<img[^>]*src\s*=\s*"([^"]+)"[^>]*>'
               r'(?:(?!<img).)*?<font[^>]*class\s*=\s*"an"[^>]*>(.*?)</font>')
BANK_PAGES_RE = r'共有\s*<span[^>]*>\s*(\d+)\s*</span>\s*页'


def _bank_page_url(dept_id, know_id, page_no):
    return ("%s?ms=gotoSearchTheme&knowid=%s&deptId=%s&searchword=&themelx=&pageNum=%d&pageMax=20"
            % (TEST_ACTION, know_id, dept_id, page_no))


def crawl_dept_bank(session, bank, dept_id, know_id="-1", jobs=4):
    """抓「院系/知识点题库」浏览页（每页 20 题，题干图片 md5 + 正确答案）。"""
    first = get(session, _bank_page_url(dept_id, know_id, 1))
    m = re.search(BANK_PAGES_RE, first)
    total = int(m.group(1)) if m else 1
    pages = {1: first}
    if total > 1:
        def one(p):
            try:
                return p, get(session, _bank_page_url(dept_id, know_id, p))
            except Exception:  # noqa: BLE001
                return p, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            for p, html in pool.map(one, range(2, total + 1)):
                if html:
                    pages[p] = html
    added = 0
    for p in sorted(pages):
        for src, ans in re.findall(BANK_ROW_RE, pages[p], re.S):
            mm = re.search(r'([0-9a-f]{16,})\.jpe?g$', src, re.I)
            if mm and bank_add(bank, mm.group(1), {"ref": clean_label(ans).replace(" ", "")}):
                added += 1
        print("  题库浏览第 %d/%d 页，累计 %d 题" % (p, total, len(bank["by_md5"])))
    return added


def newest_attempt_id(opener, exameid):
    html = get(opener, "%s?ms=getExameScoreDetailList&exameid=%s" % (SCORE_ACTION, exameid))
    ids = re.findall(r"exameDetail\('(\d+)'\)", html)
    return ids[0] if ids else None


def _fetch_pages(session, fields, pages, jobs, tries=3, label="抓取"):
    """用「跳页」方式取回指定页（jobs=1 即串行）。每一页互不依赖，失败按 tries 重试。"""
    out = {}
    pages = list(pages)
    done = [0]
    lock = threading.Lock()

    def one(p):
        data = dict(fields)
        data["pageNum"] = str(p)
        data.setdefault("zc", "")
        try:
            return p, post(session, EXAM_ACTION + "?ms=saveAnswerAndGetNextExameTheme", data, tries=tries)
        except Exception:  # noqa: BLE001
            return p, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(jobs))) as pool:
        for p, html in pool.map(one, pages):
            with lock:
                done[0] += 1
                if done[0] % 25 == 0:
                    print("    %s %d/%d 页…" % (label, done[0], len(pages)))
            if html:
                out[p] = html
    return out


def harvest_self_test(session, bank, self_test_id, max_pages=100, jobs=4):
    """跑一轮自测（交白卷也行）：收集逐页题干 md5 + 该次回顾页参考答案，对齐后入库。

    翻页统一用「跳页」（带第 1 页的表单隐藏域、只改 pageNum），所以各页互不依赖；
    失败会降级重试（并发 → 串行 → 更大重试次数），个别页始终拿不到就跳过，不中断整轮。
    """
    t0 = time.time()
    print("    进入自测（exameid=%s）…" % self_test_id)
    first = fetch_paper_page(session, self_test_id)
    if "exameform" not in first:
        raise RuntimeError("进入自测失败（没拿到试卷页）。服务端提示：%s\n"
                           "    （Cookie 可能失效，或该账号暂无此自测权限）" % readable(first))
    page1 = parse_page(first)
    if not page1["md5"] or not page1["hidden"]:
        raise RuntimeError("第 1 页没拿到题干或表单隐藏域")
    fields = page1["hidden"]
    mds = {1: page1["md5"]}
    htmls = {1: first}

    for label, jobn, tries in (("并发", jobs, 3), ("串行", 1, 3), ("串行重试", 1, 4)):
        need = [p for p in range(1, max_pages + 1) if p not in mds]
        if not need:
            break
        if jobn > 1 and len(need) == 1:
            jobn, label = 1, "串行"
        print("    %s抓取 %d 页（%d 线程）…" % (label, len(need), max(1, jobn)))
        for p, html in _fetch_pages(session, fields, need, jobn, tries=tries, label=label).items():
            pg = parse_page(html)
            if pg["md5"]:
                mds[p] = pg["md5"]
                htmls[p] = html

    missing = [p for p in range(1, max_pages + 1) if p not in mds]
    print("    采集完成：%d/%d 页，用时 %.1fs%s"
          % (len(mds), max_pages, time.time() - t0,
             ("；缺页 %s（这些题本轮不入库）" % missing[:10]) if missing else ""))
    ordered = [mds.get(p) for p in range(1, max_pages + 1)]

    # 保存末页 → 交卷（优先用真正取到的末页表单，否则退回第 1 页的表单）
    page_last = parse_page(htmls.get(max_pages) or first)
    if not page_last["hidden"]:
        page_last = page1
    try:
        post(session, EXAM_ACTION + "?ms=saveAnswerAndGetNextExameTheme",
             dict(list(page_last["hidden"].items()) + [("pageNum", str(max_pages)), ("zc", "1")]), tries=3)
        post(session, EXAM_ACTION + "?ms=submitPaperComputeGrade", page_last["hidden"], tries=3)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("交卷请求失败：%s" % exc)

    attempt = newest_attempt_id(session, self_test_id)
    if not attempt:
        raise RuntimeError("没有拿到自测的答卷 id（可能没交卷成功）")
    review = parse_review(get(session, "%s?ms=previewMinePaperShow&answerid=%s" % (SCORE_ACTION, attempt)))
    added = 0
    for i, item in enumerate(review):
        if i < len(ordered) and ordered[i]:
            if bank_add(bank, ordered[i], item):
                added += 1
    print("    自测 %s：回顾 %d 题，新增 %d 题，累计 %d 题"
          % (attempt, len(review), added, len(bank["by_md5"])))
    return added


# --------------------------------------------------------------------------- cli

def cmd_login(args):
    opener = make_opener()
    get(opener, BASE + "/")            # 先落地一次，拿到 JSESSIONID
    img = get_bytes(opener, BASE + "/Kaptcha")
    path = args.captcha_out or "captcha.jpg"
    with open(path, "wb") as fh:
        fh.write(img)
    print("验证码已保存到 %s，请打开看清 4 位数字后输入。" % path)
    code = input("验证码: ").strip()
    # 第一步：/logined.jsp 返回一个自动提交的 #roamform
    step1 = post(opener, BASE + "/logined.jsp",
                 {"username": args.user, "password": args.password, "checkcodeuser": code})
    action = "/j_spring_security_check"
    mt = re.search(r'<form[^>]*id\s*=\s*"roamform"[^>]*>', step1, re.I)
    if mt:
        action = attrs(mt.group(0)).get("action", action) or action
    # 第二步：真正的鉴权（Spring Security），字段名是 j_username / j_password
    post(opener, urllib.parse.urljoin(BASE + "/", action),
         {"j_username": args.user, "j_password": args.password, "checkcodeuser": code})
    check = get(opener, BASE + "/userOperation.do?ms=afterLogin")
    if "您好" in check:
        print("登录成功。")
        save_cookies(opener, args.cookie_jar)
        if args.cookie_jar:
            print("Cookie 已保存到 %s" % args.cookie_jar)
    else:
        hint = ""
        for kw in ("验证码错误", "密码错误", "用户名", "不存在", "锁定", "已经在", "其他地方"):
            if kw in check:
                hint = kw
                break
        print("登录可能失败（%s）。" % (hint or "无明确提示"))
        print("返回页摘要：" + clean_block(check)[:200].replace("\n", " "))
        sys.exit(2)


def cmd_bank(args):
    opener = make_opener(args.cookie_jar, args.cookie)
    if not require_login(opener, args.cookie_jar):
        sys.exit(2)
    bank = bank_load(args.bank)
    before = len(bank["by_md5"])
    if args.dept:
        for dept in args.dept.split(","):
            crawl_dept_bank(opener, bank, dept.strip(), know_id="-1", jobs=args.jobs)
    if args.know:
        for know in args.know.split(","):
            crawl_dept_bank(opener, bank, "-1", know_id=know.strip(), jobs=args.jobs)
    if args.self_test:
        for i in range(args.runs):
            print("自测第 %d/%d 轮（每轮 100 页）" % (i + 1, args.runs))
            try:
                harvest_self_test(opener, bank, args.self_test, jobs=args.jobs)
            except Exception as exc:  # noqa: BLE001
                print("  本轮中断：%s: %s" % (type(exc).__name__, exc))
                if args.debug:
                    import traceback
                    traceback.print_exc()
                break
            bank_save(bank, args.bank)
            print("  当前题库累计 %d 题" % len(bank["by_md5"]))
    bank_save(bank, args.bank)
    save_cookies(opener, args.cookie_jar)
    print("题库：%d -> %d 条（md5 维度），选项签名 %d 条；已写入 %s"
          % (before, len(bank["by_md5"]), len(bank["by_sig"]), args.bank))


def cmd_exam(args):
    opener = make_opener(args.cookie_jar, args.cookie)
    if not require_login(opener, args.cookie_jar):
        sys.exit(2)
    bank = bank_load(args.bank)
    if not bank["by_md5"]:
        print("题库为空，先用 `bank` 子命令累积题库。")
    html = do_exam(opener, bank, args.exam, dry_run=args.dry_run)
    save_cookies(opener, args.cookie_jar)
    if html:
        text = clean_block(html)
        print("---- 交卷结果 ----")
        print(text[:600])


KNOWN_IDS = [
    ("16385289", "外国语学院语言实验室安全准入考试（学生版）"),
    ("16770044", "外国语学院语言实验室安全准入自测（不限次，用来刷题库）"),
    ("51500827", "2026年下半年实验室安全准入考试（非石油化工类）"),
    ("51500828", "2026年下半年实验室安全准入考试（石油化工类）"),
    ("12453622", "计算机学院实验室安全考试"),
    ("7425627", "计算机专业实验室安全考试"),
]


def cmd_list(args):
    opener = make_opener(args.cookie_jar, args.cookie)
    if not require_login(opener, args.cookie_jar):
        sys.exit(2)
    html = get(opener, EXAM_ACTION + "?ms=getExame")
    rows = []
    for tr in re.split(r'<tr[^>]*>', html)[1:]:
        mid = re.search(r"gotoExame\('(\d+)'\)", tr)
        if not mid:
            continue
        name = re.sub(r'\s+', ' ', clean_block(tr.split("</td>")[0]))[:46]
        rows.append((mid.group(1), name))
    print("考试列表（共 %d 场，格式：exameid  名称）：" % len(rows))
    for eid, name in rows:
        print("  %s  %s" % (eid, name))
    print("\n自测 id 同理：网页版点顶部「自测」，F12 搜 gotoTest(' ，引号里的数字就是 id。")
    print("已确认的几个 id（可直接用）：")
    for eid, name in KNOWN_IDS:
        print("  %s  %s" % (eid, name))


def main():
    # 输出重定向/被捕获时 Python 会缓冲 stdout，导致“跑了但一直没反应”，这里强制行缓冲
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="广石化实验室安全考试：题库缓存 + 自动做题")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = dict()
    for name, fn in (("login", cmd_login), ("bank", cmd_bank), ("exam", cmd_exam), ("list", cmd_list)):
        p = sub.add_parser(name)
        p.add_argument("--cookie-jar", default="cookie.txt", help="Cookie 文件（保存/复用会话）")
        p.add_argument("--cookie", default=None, help="直接给 Cookie 字符串，如 'JSESSIONID=xxx'")
        p.set_defaults(func=fn)
        if name == "login":
            p.add_argument("--user", required=True)
            p.add_argument("--password", required=True)
            p.add_argument("--captcha-out", default="captcha.jpg")
        if name in ("bank", "exam"):
            p.add_argument("--bank", default="bank.json")
            p.add_argument("--debug", action="store_true", help="出错时打印完整堆栈（排查用）")
        if name == "bank":
            p.add_argument("--dept", default=None, help="院系 id，如外语学院 447555（逗号分隔）")
            p.add_argument("--know", default=None, help="知识点 id（逗号分隔）")
            p.add_argument("--self-test", default=None, help="自测 exameid，如外国语学院自测 16770044")
            p.add_argument("--runs", type=int, default=3, help="自测跑几轮")
            p.add_argument("--jobs", type=int, default=4, help="并发线程数；设 1 = 纯顺序（最稳最慢）")
            p.add_argument("--pace", type=float, default=0.12, help="请求最小间隔(秒)；老被掐连接就调大")
        if name == "exam":
            p.add_argument("--exam", required=True, help="考试 exameid，如 16385289")
            p.add_argument("--dry-run", action="store_true", help="只答题不交卷（演练）")
    args = ap.parse_args()
    if getattr(args, "pace", None):
        global PACE
        PACE = float(args.pace)
    args.func(args)


if __name__ == "__main__":
    main()
