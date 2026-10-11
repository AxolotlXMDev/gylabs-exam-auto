# gylabs-exam-auto

一个纯 HTTP 的小工具：**离线缓存题库 + 自动做题**。

---

## 0. 简短教程：(适用于外国语安全准入考试)
1. 浏览器拿cookie（JSESSIONID）放cookie.txt
2. `python gylabs_exam.py exam --cookie-jar cookie.txt --bank bank-外国语学院语言实验室安全准入考试.json --exam 16385289`


## 1. 目录内容

| 文件 | 作用 |
|---|---|
| `gylabs_exam.py` | 主脚本，四个子命令：`list` / `login` / `bank` / `exam` |
| `bank.json` | 题库缓存。**随包发布的是空壳**，跑一次 `bank` 就会填满（并发模式下只要十几秒） |
| `cookie.txt.example` | Cookie 模板；复制成 `cookie.txt` 再填自己的 `JSESSIONID`（`cookie.txt` 不要外传） |
| `gylabs-脚本化做题方案.md` | 原理、接口清单、踩坑记录（想二次开发看它） |

---

## 2. 环境要求

* Python 3.8+（实测 3.14），**不需要 pip 安装任何东西**。
* 能访问 `https://gylabs.gdupt.edu.cn`（校园网 / VPN）。
* Windows 下命令示例用 PowerShell，`cd` 到本目录再执行。

---

## 3. 第一步：准备 Cookie

所有联网命令都需要一个**有效的登录会话**。三选一：

**方式 A（最稳，推荐）** 浏览器里登录 → F12 → **Application / 应用** → 左侧 Cookies → 点 `https://gylabs.gdupt.edu.cn` → 复制 `JSESSIONID` 的值 → 写进 `cookie.txt`：

（先 `copy cookie.txt.example cookie.txt` 生成这个文件）

```
JSESSIONID=你复制到的值
```

（`#` 开头的行会被当注释忽略；脚本每次跑完会把最新值写回这个文件。）

**方式 B** 不写文件，直接命令行传：

```powershell
python gylabs_exam.py list --cookie "JSESSIONID=你复制到的值"
```

**方式 C** 让脚本自己登录（需要人眼看验证码，且该站是两步登录，偶尔要重试）：

```powershell
python gylabs_exam.py login --user 你的学号 --password '你的密码' --cookie-jar cookie.txt
```
它会把验证码存成 `captcha.jpg`，打开看清 4 位数字后输入即可。

---

## 4. 第二步：看有哪些考试 / 拿 id

```powershell
python gylabs_exam.py list --cookie-jar cookie.txt
```

输出每场考试的 **exameid + 名称**，例如：

```
  16385289  外国语学院语言实验室安全准入考试（学生版）
  16770044  外国语学院语言实验室安全准入自测（不限次，用来刷题库）
```

* **考试 id**：上面这个列表里直接拿。
* **自测 id**：网页版点顶部「自测」→ F12 搜 `gotoTest('` → 引号里的数字就是（外国语学院自测 = `16770044`）。
* 常见 id 也内置在脚本里，`list` 输出末尾会一并打印。

---

## 5. 第三步：刷题库（自测不限次，随便刷）

```powershell
python gylabs_exam.py bank --cookie-jar cookie.txt --self-test 16770044 --runs 5 --bank bank.json
```

---

## 6. 第四步：做题

**尝试作答（不交卷，不消耗机会）**：

```powershell
python gylabs_exam.py exam --cookie-jar cookie.txt --bank bank.json --exam 16385289 --dry-run
```

```
  第  1页 单选题 md5=46496284 -> B
  第  2页 多选题 md5=96a297ae -> A,B,C,D
  第 85页 判断题 md5=unknown  -> 题库里没有，跳过（需人工）
```

**正式交卷**：

```powershell
python gylabs_exam.py exam --cookie-jar cookie.txt --bank bank.json --exam 16385289
```

---

## 7. 命令参数速查

```
python gylabs_exam.py list  [--cookie-jar cookie.txt | --cookie "JSESSIONID=..."]
python gylabs_exam.py login --user 学号 --password 密码 [--cookie-jar cookie.txt] [--captcha-out captcha.jpg]
python gylabs_exam.py bank  [--cookie-jar cookie.txt] [--bank bank.json]
                            [--self-test 16770044 --runs 5]   # 用自测刷题库
                            [--jobs 4] [--pace 0.12]          # 并发线程数 / 请求最小间隔，1=顺序
                            [--dept 447555] [--know <知识点id>] # 抓院系/知识点题库浏览页
python gylabs_exam.py exam  [--cookie-jar cookie.txt] [--bank bank.json] --exam 16385289 [--dry-run]
```

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 报「您不在允许考试的考生范围内」 | 浏览器打开进入考试页面刷新一下 |
| 提示「题库里没有，跳过」很多 | 题库没刷够，多跑 `bank --runs 3`；判断题尤其要多刷。 |
| 觉得刷库还是慢 | 加大 `--jobs`（如 8~12），或减少 `--runs`；题库到“新增 0 题”就满了。 |

---

## 9. 原理（为什么这么做）

1. **题干是图片**：`\upload\theme_picture\<md5>.jpeg`，md5 是内容文件名，跨场次稳定 → 拿它当题库主键最可靠。
2. **选项每次打乱**：所以答案按**选项文本**存（`bank.json` 的 `by_sig`），答题时再映射成当前页的字母。
3. **自测不限次**：交卷后能打开“整卷回顾页”，里面有题干**文本**、选项、参考答案 → 与当次逐页的 md5 按题号对齐，等于免费给题库打标。
4. **95 分及格**：容错很小，所以判断题这种“只能按 md5 命中”的题型要多刷几轮补全。

---
