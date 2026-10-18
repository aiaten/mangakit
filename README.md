# 漫卷 MangaKit

> 版本 **0.4.1** ｜ 通用漫画批量下载器 ｜ Windows

**漫卷 MangaKit 是一个本地规则驱动的漫画下载软件**：把漫画网站的页面结构写成 JSON 规则，
由程序直接抓取详情页、章节列表和图片地址并下载。它**不走任何第三方服务器**，
全部解析在你自己电脑上完成。

*（上面这段自我描述与版本号引自 `使用文档.md` 开头的版本栏与
`mangakit/__init__.py` 里的 `__version__`，两处当前都是 **0.4.1**。
本文所有对文档的引用一律按**章节名**给出，不写行号 —— 那份文档在改的时候行号会漂。）*

## 给谁用

- 想把自己在追的漫画**离线备份**到本地、自己决定看什么用什么阅读器的普通用户；
- 不想装一堆浏览器插件、也不想把网址交给在线解析站点的人；
- 动手能力强一点、遇到新站愿意自己加规则的高级用户（支持图形界面「添加网站」或手写 JSON）。

**不需要会写代码**：内置 22 条站点规则，装好就能用。

---

## 功能特性

### 搜索与浏览

- **全站并发搜索**：「全部站点」同时向所有启用了搜索的站点并发请求，**哪个站先出结果就先显示哪一批**，
  不用等最慢的站。每站单独走 `search_partial` 推流（实测首批 **0.38s**、全量 **3.08s**，
  见 `使用文档.md` 的「二、图形界面使用 → 1. 搜索 / 打开」一节）。
- **封面异步加载**：结果先以标题占位秒出，封面由 `CoverLoader` 线程池（6 线程）后台取，
  取到一张补一张，界面不冻结。封面缓存在 `%TEMP%\mangakit_cover`，上限 600 张。
- **双击即打开**：结果条目从创建那一刻起就是整格可点的（未取到封面时显示满尺寸灰色占位图，
  而不是空图标 —— 空图标会被 `setUniformItemSizes(True)` 把命中区冻在 80×25，导致「封面看得见点不到」）。
- **「打开网址…」**：直接粘详情页网址打开，与双击结果**走完全相同的链路**。

### 漫画详情

- 封面、标题、作者、状态、标签、章节列表。
- 章节批量勾选：全选 / 反选 / 取消选择 / **仅选未下载** / 选最新 20 话，表头方框一键全选。
- 正序 / 倒序切换（部分站的章节顺序是反的）。
- **「展开全部章节」**：不少站详情页只渲染最近 20～24 话，剩下藏在「加载更多」后面。
  程序分两条通道补齐：
  1. `chapters.api` —— 站方章节列表接口（实测顶漫画 24 话 → **677 话**，D漫画 24 话 → **70 话**）；
  2. `chapters.embed` —— 完整数组内嵌在页面脚本里（实测神奇漫画 20 话 → **333 话**）。

  展开前勾好的章节**不会丢**（按章节网址保留并自动勾回）。按钮可用性由规则的
  `chapters.api` / `chapters.embed` 决定：不支持的站点置灰并说明原因，不会让你白等一次完整重载。
  *（22 条规则中目前只有 **3 个站**配了补齐通道 —— 已实测核对，
  与 `使用文档.md` 的「三、支持的站点 → 会补齐折叠章节的站」一节一致。）*

### 下载

- **断点续传**：任务进度存在 SQLite 里，软件被强杀、关机、重启后未完成的章节
  **从已下载的页继续**，不重头再来。
- 任务管理：全部继续 / 全部暂停 / 清理已完成 / 暂停 / 继续 / 重试 / 删除 / 打开下载目录。
  表格显示进度、状态、速度（页/秒）、错误信息，下方实时日志。
- 并发可调：单章内图片并发默认 6，同时下载章节数默认 3。
- 重试 3 次 / 超时 20 秒 / 同域限速可设 / 单图体积上限 40 MB。
- **写入预检**：下载前先真落盘自检（建目录 **+ 试建文件**），目录不可写时直接报人话
  （哪个目录、什么原因、怎么办），而不是等抓完十几张图才抛 `PermissionError`。
- WAF 拦截与站方故障页识别：能区分「站方数据库挂了返回 200 + 一句报错」和真正的「没有章节」。

### 导出

下载完自动打包，四种格式任选：

| 格式 | 说明 |
| --- | --- |
| **CBZ** | 漫画通用格式，图片按页序命名，大多数阅读器直接支持 |
| **ZIP** | 普通压缩包 |
| **PDF** | `img2pdf` 生成，适合打印 |
| **EPUB** | 电子书格式，元数据从漫画标题写入 |

另支持**长条图拼接**、WebP → PNG 自动转换、超长图自动切割（阈值默认 1600px）、
下载后校验图片文件头（跳过伪装成图片的广告页）。

### 收藏与追更

- **收藏是手动的**（v0.4.1）：在漫画详情页点「收藏」按钮才会进收藏，再点一次取消。
  以前是「打开过的漫画自动进收藏」，搜出来看一眼就多一条，很烦，也放大了
  v0.3.0 那次「收藏被测试清空」事故的破坏面。收藏列：漫画 / 作者 / 来源 /
  最新章节 / **上次章节数** / 状态 / 上次检查 / 网址。
- 标题过滤，多选 + 右键菜单删除，删除前一律弹确认。
- **检查更新**：**并发 6 路**同时查（单本就是一个详情页请求）。
  实测 8 本：串行 3.0 秒 → 6 路并发 **0.38 秒**（见 `使用文档.md` 的
  「二、图形界面使用 → 4. 收藏 → 检查更新」一节）。
  单本最多等 45 秒，超时记一笔继续下一本，不会让某一本卡死整次检查；
  查询失败的**不覆盖基准**，只检查成功的才更新。
  首次检查只记基线（绿色「已记录当前进度」），之后再点才对比出「有 N 话更新」。
- **「最新章节」按话号取，不按列表位置**（v0.3.0）：有些站章节是**倒序**的，
  旧版取「列表最后一条」，检查完《香格里拉》会显示成「第 1 话」。
  现在用严格话号正则取最大话号（`第12话` / `Chapter 12` / `第3卷`…），
  认不出话号的条目（如「2026新年贺图」）不参与比较；若过半条目都认不出话号，
  整站退回按列表位置。实站验证：manhuagui_cc《香格里拉》254 话，
  旧逻辑取到 index 253 = 「第1话 你为何而游戏」，新逻辑正确取到最高话号。
- **「上次章节数」列**（v0.3.0）：显示**上一次**检查时源站的话数，
  与本次结果并列即可看出有没有更新（涨/跌标橙，悬停给出「1060 话 → 1068 话」）。
  点完检查后这一列**仍显示旧值**（不是本次结果），否则等于白加这一列。

### 站点管理

- 逐个启用 / 禁用站点，某个站出问题先禁用，不影响其他站。
- **添加网站**：贴入一个漫画详情页网址，程序自动探测（打开页面 → 找章节链接 →
  抓一话试拿正文图片 → 真下几张图 → 顺带找搜索入口），**能下才让你加**，
  加进来名字前带 `[自建]`。探测不过会写清楚原因。
- 删除站点：自建的可删，内置的删不掉（只能取消勾选停用）。

### 图源仓库与内置浏览器（v0.2.0 新增，均已接 GUI）

- **异次元图源导入**（`mangakit/ycksource.py`）：把异次元 App 格式的图源对象翻译成
  MangaKit 站点规则。转换必须在真实引擎里跑一遍 `parse_chapters` / `parse_pages` 才算数，
  宁可报「不支持」也不产出看着能用的死规则。
  已经接入图形界面：**「图源仓库」页（导航第 6 项）**可以搜索 / 排序 yck2026 图源索引、
  单条校验、一键加成自定义站点规则（写进配置后会立刻出现在设置页的站点表里）。
  图源模块 import 失败时**只有这一页降级**并写明原因，其余页面照常用。
  对标注为「加密源」（关键字段是 `:_R002_…` 这类不透明块、由阅读器 App 内部解密）的图源
  **直接不支持，本项目不做解密**。实测口径：抽 60 条源，**14 条明文可转、46 条不可转**
  （其中 **32 条**是 `:_R002_` 加密串，客户端不解密）。
- **内置浏览器**（QtWebEngine）：**已实现**，导航第 7 项，默认打开
  yck2026 图源索引页（`DEFAULT_HOME_URL`）。内核**懒加载** —— 只有真的切到这一页
  才 import `PySide6.QtWebEngineWidgets`，首次进入约 3-4 秒，之后即开即用；
  构造主窗口时一行 WebEngine 代码都不碰。详情页 / 图源页的「在浏览器中打开」
  也走这一页（站点要过验证码、图片防盗链或解析失败时，不用把用户丢到系统浏览器）。
  WebEngine 不可用时这一页显示人话提示并降级，不影响其余功能。

---

## 快速开始

### 方式一：直接用 exe（推荐）

1. 从 [Releases](../../releases) 下载 `MangaKit.exe`（`启动漫卷.bat` 可选，见下）。
2. **直接双击 `MangaKit.exe` 即可** —— v0.2.0 起不再需要外挂启动器。

**为什么现在可以双击了**：单文件 exe 运行时仍要先解包自己，但解包位置不再由
`%TEMP%` 决定 —— `mangakit.spec` 里写了
`runtime_tmpdir=r'%LOCALAPPDATA%\MangaKit\run'`，把解包目录固定在**用户目录**下，
与 `%TEMP%` 是否健康（条目过多、被清理软件锁住、磁盘满、被安全软件拦）彻底解耦。
用绝对路径而不是相对路径，是因为相对路径按运行时的**当前工作目录**解析：
从快捷方式/计划任务启动时 cwd 可能是 `C:\Windows\System32`，解包就落到系统目录里去了。

启动自检也不再靠 bat：`mangakit/main.py` 的 `_startup_preflight()` 会检查
**下载目录能不能真建文件**，不能就弹 GUI 提示说清楚哪个目录、什么原因、怎么办。

`启动漫卷.bat` 现已**降级为可选的兜底入口**，保留它有两个用处：
一是给仍按老习惯启动的人兜底（顺手清一次 `%LOCALAPPDATA%\MangaKit\run` 下崩溃残留的
`_MEI*`，且只在没有实例运行时才清）；二是**参数转发** ——
`启动漫卷.bat --selftest report.txt` 这样把参数透传给 exe。

> ⚠️ **别把 exe 放在开发工具、沙箱、隔离环境的目录里跑。**
> 这类环境普遍限制程序往工作区以外写文件，症状是下载时报「拒绝访问 / Permission denied」
> ——**建目录常常还能成功，但建文件一定失败**。把 exe 放到**桌面**或「文档」目录即可解决。

程序首次运行会自动建数据目录（`%APPDATA%\mangakit\`，含 `config.json`、`mangakit.db`、`logs\`）。
若 `%APPDATA%` 不可写，会自动退回**便携模式**，把数据放到 exe 旁边的 `MangaKitData\`，
整个程序目录拷到 U 盘也能用。想手动指定，设环境变量 `MANGAKIT_HOME`。

### 方式二：用源码跑

需要 **Python 3.11+**：

```bash
cd mangakit
pip install PySide6 "httpx[http2]" lxml cssselect pillow img2pdf
python -m mangakit                    # 启动图形界面
```

> 仓库当前**没有** `requirements.txt`，依赖就是上面这一行（`mangakit.spec` 的
> `hiddenimports` 列出的运行时依赖：`PySide6` / `httpx` / `h2` / `lxml`+`cssselect` / `img2pdf`）。

### 方式三：自己打包

```bash
python -m PyInstaller --noconfirm --clean mangakit.spec
# 产物：dist/MangaKit.exe
```

打包前**先关掉正在运行的 exe**，否则写 `dist\MangaKit.exe` 会报
`PermissionError: [WinError 5] 拒绝访问`：

```powershell
Get-Process -Name MangaKit | Stop-Process -Force
```

打完**务必跑一次** `python tools\t_exe_ver.py` 核对 exe 是否比源码旧 —— 旧 exe 会让最新修复「测不出来」。

### 命令行模式

```bash
python -m mangakit --sites                                        # 列出所有内置站点
python -m mangakit --url https://manhuagui.cc/comic/douluodalu    # 解析详情页并列出章节
python -m mangakit --url <网址> --get 1 --limit 20                 # 下第 1 章前 20 张
python -m mangakit --selftest report.txt                          # 离线自检，结果写文件
python -m mangakit --version
```

| 参数 | 含义 |
| --- | --- |
| `--url URL` | 漫画详情页网址，自动识别站点并解析 |
| `--get N` | 下载第 N 章（不填只解析不下载） |
| `--limit N` | 每章最多下载 N 张，默认 8 |
| `--sites` | 列出内置站点及启用状态 |
| `--selftest FILE` | 自检（配置 / 规则 / 数据库 / 打包器 / **webengine 渲染**），结果写入 FILE |
| `--selftest-ui FILE` | 同 `--selftest`，额外弹出结果窗口（**仅打包后的 exe 可用**） |
| `--version` | 版本号 |

> 打包后的 `MangaKit.exe` 是 **windowed**（双击不弹黑框），因此 `--url` / `--sites`
> 的输出看不到，这两个参数请用源码方式运行。要验证 exe 本身是否正常，用 `--selftest`：

```bat
MangaKit.exe --selftest report.txt
```

> 五项（config / rules / store / exporter / **webengine**）全 OK 时会以
> **`SELFTEST PASS`** 结尾；任何一项失败则 `SELFTEST FAIL (N)`。
> 其中 `webengine` 这一项是真起一个 `QWebEngineView` 加载页面并断言 JS 跑通，
> 专抓「打包后浏览器白屏」（Chromium 沙箱挡住渲染进程）—— 源码运行看不出来，
> 只有打包产物才需要。加 `--selftest-ui` 会额外弹结果窗口，自动冒烟时不要加。

---

## 站点规则

### 内置规则

当前内置 **22 条规则**，其中：

| 指标 | 数量 | 数字出处 |
| --- | --- | --- |
| 规则文件总数 | **22** | `mangakit/rules/*.json`，实点数得 22 |
| 可见站点（`"hidden"` 未设） | **12** | 实点 JSON：12 个可见 / 10 个隐藏 |
| 支持站内搜索 | **12** | `Site.can_search`（`sites.py:387-391`）= `search.url` 或非空 `search_url`；22 条里恰好 12 条为真 |
| 配了折叠章节补齐通道 | **3** | 顶漫画 / D漫画 / 神奇漫画 |
| 通过实网下载验证 | **19/19** | `tools/verify.out.json`：19 条记录，`ok` 全为 `true` |

19 个实网站点覆盖漫画柜、漫画之家、漫画站、动漫网、動漫戲說、咚漫、漫画栈、漫画网、
上看漫画、D漫画、动漫啦、松子漫画、漫画屋、包子漫画、250漫画、顶漫画、漫画1234、
神奇漫画等（`tools/verify.out.json` 19 条记录逐条核对）。
另外 **2 条是站方模板/示例**（`templatedm`「通用 M 站模板」、`kanmanmanhua`「标准 M 站模板」注释里写明是规则示例），
**1 条 `manhuagui` 是旧域名别名**（`mhquan.com`；该站防火墙对普通浏览器 UA 全站返回非标状态码 702
拦截页，**建议改用 `manhuagui_cc`（manhuagui.cc）**）。
详细的逐站实测结果（章节数 / 图片数 / 搜索结果数）见 `使用文档.md` 第三节。

> **12 个可见站 / 10 个隐藏站**（`sites.py:1059` 的 `visible_sites()` = 已启用且规则未标 `hidden`）。
> 按 `Site.can_search` 口径（`sites.py:387-391`：`search.url` 或非空 `search_url`）实测这 22 条里
> **12 条可搜索、10 条不可**，与可见/隐藏**完全重合** —— 这不是巧合：`sites.py:383-384` 写明
> `hidden` 就是给「下载能用但没有站内搜索」的站准备的，功能保留，只是不占用户的选择列表。
> 用 `tools/verify.py` 的 `can_search` 字段交叉核对（19 个实网验证站里 11 条为 true），
> 结论一致（`tools/verify.py:95-96` 那行注释正是为「search 坏了却照样 PASS」这个坑加的）。
> 隐藏的站**规则仍在**：仍能被 `for_url` 按网址匹配，
> 仍可通过「打开网址…」直接下载，也会在 `verify.py` 里照跑（19 个实网验证站里就有 kanman、
> dongmanwang、manhuawang、manhuazhan 等隐藏站）。
> 想让某个站重新出现在下拉里，把规则文件里的 `"hidden": true` 删掉即可 ——
> 不支持搜索的站进下拉后会带「（不支持搜索）」后缀，并在全站搜索时被自动跳过
> （`main_window.py:218` / `main_window.py:266`）。

### 方式一：图形界面「添加网站」（推荐，不用写代码）

1. 浏览器打开那个站的**某本漫画的详情页**（能看到章节列表的那一页），复制网址。
   **要贴详情页，不是网站首页** —— 首页上找不到章节。
2. 漫卷 → 设置 → 站点管理 → 点「**添加网站**」。
3. 粘贴网址 → 「**开始探测**」。程序会自己走一遍：打开页面 → 找章节链接 →
   抓一话看能不能拿到正文图片 → 真下几张图试试 → 顺带找搜索入口。通常几秒，慢的站几十秒。
4. 「**探测通过：这个站可以下载 ✅**」→「添加到站点列表」按钮亮起，点它即加入（名字带 `[自建]`）。
   「**探测未通过 ❌**」→ 按钮灰着，下方会写清楚为什么。

自建的站和你手写的 `rules/*.json` **在引擎眼里完全等价**，存在配置文件里，升级程序不会丢。

**为什么会有站探测不过？** 因为不是所有站都能靠「读懂网页」来下载：

| 原因 | 说明 | 能不能救 |
| --- | --- | --- |
| 图片靠 JavaScript 现拼 | 网页源码里根本没有图片地址，是浏览器打开后临时算出来的（常带服务端签名）。典型：动漫屋 dm5 | 需要写专门适配器，暂时不行 |
| 拿不到正文图片 | 图片要登录、或被付费墙挡住 | 换个不需要登录的站 |
| 找不到章节链接 | 贴的是首页/列表页，或完全靠 JS 填章节 | 确认贴的是详情页 |
| 页面打不开 | 域名解析失败、站点关停、本地网络到不了 | 先在浏览器里确认能打开 |
| 生成的规则跑不通 | 少数站结构太特殊 | 需要手写规则文件 |

程序**宁可拒绝添加，也不会给你加一个下下来全是广告图的死站**。探测阶段的宽松识别逻辑
和规则阶段引擎的 `Selector` + `url_re` **不等价**，早期版本出现过「探测报 62 话、实测图片可下载」
但写成规则后引擎解析出 **0 章**（标题还识别成导航栏的「登录」）的假成功
（见 `使用文档.md` 的「九、目录结构（源码）→ 冒烟脚本分工」一节）。
现在 `_verify_rule()` 会把生成的规则**真跑一遍** `parse_chapters` / `parse_pages`，不过就拒绝添加
（`probe.py:235` 调用 / `probe.py:245` 定义）。

### 方式二：手写 JSON 规则

漫画站改版很频繁，程序把「怎么解析一个站」完全交给 JSON 规则，可以自己加新站，不用改代码。

1. 复制模板：`mangakit/rules/templatedm.json` → `mangakit/rules/你的站点id.json`。
2. 用 F12 看三处：**详情页、章节列表、章节页的图片**，把 CSS 选择器填进规则。
3. 命令行验证：`python -m mangakit --url <你的详情页> --get 1 --limit 3`。
4. 确认能下载后，在设置里勾选启用。

```jsonc
{
  "id": "站点唯一id",
  "name": "显示名称",
  "domains": ["example.com", "www.example.com"],   // 用来匹配网址
  "headers": { "Referer": "https://example.com/" }, // 每个站需要的请求头
  "cookies": "a=1; b=2",                            // 需要登录的站填这里

  "comic": {
    "title":  { "css": "h1", "text": true },
    "cover":  { "css": ".cover img", "attr": "src" },
    "tags":   { "css": ".tags a", "text": true, "all": true }
  },

  "chapters": {
    "list": { "css": "#chapterlist a" },       // 章节链接的选择器
    "url_re": "/chapter/\\d+",                 // 过滤非章节链接（正则）
    "max_pages": 20,                           // 章节列表分页时最多翻几页
    "next": { "css": "a.next", "attr": "href" },
    "api":   { "url": "https://example.com/comic/{id}", "method": "POST",
               "list": "data.chapters", "title": "chapterName" },   // 通道 1：补折叠章节
    "embed": { "start": "\\\"chapters\\\":(\\[)",
               "item": "\"id\":\"([^\"]+)\",\"title\":\"([^\"]*)\"",
               "fields": ["id", "title"], "url": "/read/{comic_id}/{id}" }  // 通道 2
  },

  "pages": {
    "list": { "css": ".reader img" },
    "url":  { "attr": "src" },
    "filter": "\\.(?:jpg|png|webp)",           // 白名单正则，滤掉封面/广告图
    "replace": "/dup//dup/",                   // 修正图片 URL 里重复的路径段
    "lazy_js": "var\\s+imgs\\s*=\\s*\\[([^\\]]+)\\]",  // 图片在 JS 变量里时用
    "split": "|||"
  },

  "search": {
    "url": "https://example.com/search?q={q}",
    "list": { "css": ".result li" },
    "link": { "css": "a.thumb", "attr": "href" },
    "title": { "css": ".name", "text": true }
  }
}
```

完整字段说明、`lazy_js` / `replace` / `chapters.api` / `chapters.embed` 的细节与踩坑记录，
见 `使用文档.md` 第六节。

---

## 目录结构

```
mangakit/
├── mangakit/                    程序包
│   ├── __main__.py              入口（兼容 PyInstaller 顶层执行）
│   ├── main.py                  GUI / CLI 入口，含 --selftest、编码兜底、规则目录定位
│   ├── config.py                配置读写（DEFAULTS 在 config.py:150）
│   ├── store.py                 SQLite 持久化（任务 / 章节 / 收藏 / 站点状态）
│   ├── http_client.py           httpx 客户端（重试 / 限速 / http2 / 编码嗅探）
│   ├── sites.py                 规则引擎：Selector / RuleAdapter / SiteRegistry
│   ├── crypto.py                页面加密解密（mhzh 系站点的 decryptData 端口）
│   ├── images.py                图片嗅探 / 校验 / 长图切割 / 命名
│   ├── downloader.py            下载管理器（独立线程跑 asyncio、断点续传）
│   ├── exporter.py              CBZ / ZIP / PDF / EPUB / 长条图导出
│   ├── service.py               业务编排，异步请求 + 结果回调
│   ├── probe.py                 「添加网站」的探测引擎
│   ├── ycksource.py             异次元图源 → 规则转换 + 图源索引抓取（「图源仓库」页后端）
│   ├── adapters.py              站点适配器
│   ├── models.py / logsetup.py  数据模型 / 日志
│   ├── gui/
│   │   ├── bridge.py            Qt 信号桥（后台线程 → UI 线程）
│   │   └── main_window.py       七个页面（搜索 / 详情 / 任务 / 收藏 / 设置 / 图源仓库 / 内置浏览器）
│   └── rules/
│       └── *.json               站点规则，一条一站（22 条）
├── tools/                       开发与测试脚本
│   ├── verify.py                实网验证：逐站解析 + 下载，输出 PASS/FAIL
│   ├── cases.json               验证用的测试用例
│   ├── test_block.py            WAF / 站方故障页识别 + 头值编码的离线回归
│   ├── test_write_guard.py      目录不可写时是否报人话而非裸 PermissionError
│   ├── exe_smoke.py             打包后 exe 的端到端冒烟（多站各下 2 张）
│   ├── gui_smoke.py             打包 exe 的 GUI 冒烟（53 条断言）
│   ├── t_exe_ver.py             打包时序核对：exe 是否比源码旧
│   ├── t_no_splash.py           启动窗口回归：归档无 splash 载荷 + 全程无 Tk 窗口
│   ├── t_yck_convert.py         图源转换回归：真抓真转真下载（29 项）
│   ├── t_gui_new_pages.py       新页面回归：图源仓库 + 内置浏览器（56 项）
│   ├── t_probe.py / t_roundtrip2.py / t_gui_addsite.py   「添加网站」探测链路
│   ├── t_updates.py / t_upd_bench.py / t_upd_timeout.py  收藏「检查更新」
│   └── t_publish.py             一键发布（add → commit → push → tag → gh release）
├── selfcheck.py                 离线冒烟测试（不联网，秒级）
├── mangakit.spec                PyInstaller 打包配置（正式 onefile 版，含 runtime_tmpdir）
├── assets/                      图标与启动图 / 版本资源（v0.2.0 起全部接进 exe）
│   ├── mangakit.ico             程序图标（46866 B，**7 档尺寸 16-256**，已接入 spec）
│   ├── splash.png               启动图（60789 B，**600×340**；v0.3.1 起不再被 bootloader 使用）
│   └── version_info.txt         版本资源（3488 B，版本号从 `__init__.py` 的 `__version__` 现读）
├── 启动漫卷.bat                 备用启动器（可选）：兼容旧习惯 + 参数转发，双击 exe 即可运行
├── 使用文档.md                  完整使用文档（**以这一份为准**）
└── 使用文档.txt                 文档的纯文本版
    └── 由 `tools\md2txt.py` 生成（`md2txt.py:16-17` 的 SRC/DST）；
        改完 `.md` 后请重跑该脚本同步 `.txt`
```

> 关于图标与启动画面：
> `mangakit.spec` 现在写的是 `icon=os.path.join(_ASSETS, "mangakit.ico")`，
> 所以 `dist\MangaKit.exe` 用的是**漫卷自己的图标**（7 档尺寸 16～256），
> 不再是 PyInstaller 那个 32×32 的默认图标。
>
> **启动画面在 v0.3.1 已移除。** v0.3.0 及更早的 spec 里有
> `Splash(assets/splash.png, ...)`，它是用户看到「启动时冒出一个标题为 `tk`
> 的空白小窗口、约 5 秒后才出现主界面」的原因——PyInstaller 的 splash 在
> Windows 上用 Tcl/Tk 实现（`excludes` 里的 `"tkinter"` 只排 Python 层绑定，
> 挡不住 bootloader 自带的 splash）。而且它**一直是坏的**：spec 里
> `text_font="Microsoft YaHei UI"` 带空格，模板原样拼进 Tcl 脚本没加引号，
> `font create myFont -family Microsoft YaHei UI -size 10` 处抛
> `bad option "YaHei"`，而 `wm overrideredirect` / `wm geometry` 在脚本末尾
> 才执行 —— Tk 于是退回默认窗口（216×239、带标题栏、标题 `tk`、内容全空白）。
> 移除后 exe 小了约 1.7 MB（不再打包 `tcl86t.dll`+`tk86t.dll`+`Splash-00.res`），
> 双击到主窗口出现**全程没有任何窗口**。
> `mangakit/main.py` 里的 `_splash_update()` / `_splash_close()` **保留未删**
> （现在是无害的空操作），以便将来要恢复时不用再改那 5 个调用点。
> **若要恢复，字体名必须写成 `text_font='"Microsoft YaHei UI"'`（带引号）**，
> 否则会原样退回上面那个空白小窗。回归项：`tools\t_no_splash.py`。
>
> **v0.3.2 补掉了移除 splash 留下的尾巴。** 移除 `Splash()` 之后 bootloader 不再设
> `_PYI_SPLASH_IPC`，而 `_splash_update()` 里那句 `import pyi_splash` 会在**模块级**
> 就抛 `KeyError`，被 `pyi_splash` 自己的 `except` 接住后往日志写一条 warning
> **外加 traceback** —— 每次启动都刷，日志越来越脏，排查真问题时会被误导。
> 修法是**先看环境变量再决定 import**（`mangakit/main.py` 的 `_splash_available()`）：
> ```
> if os.environ.get("_PYI_SPLASH_IPC") is None:
>     return False        # 没有启动画面，不碰 pyi_splash
> ```
> 结果用模块级 `_splash_state` 缓存 —— `pyi_splash` import 成功后会自己删掉那个
> 环境变量，现查的话第二次会误判成「没有启动画面」，`_splash_close()` 就会漏关。
> 将来真恢复启动画面时，变量由 bootloader 提供，这段守卫自动放行，5 个调用点仍不用改。
> 回归项：`tools\t_splash_guard.py`（离线桩模块）、`tools\t_no_splash.py` 第 ③ 层
> （起一次 exe，断言日志里没有 `pyi_splash` 字样）。
> 版本号由 `assets/version_info.txt` 作为 exe 的版本资源写入，
> 内容从 `mangakit/__init__.py` 的 `__version__` 现读，不再手改。

---

## 开发与测试

测试分五层（外加一个只跑图源转换的 3.5 层），**改完代码从下往上跑，越靠上的越贵**：

### 第 1 层：离线单元冒烟（不联网，秒级）

```bash
python selfcheck.py
```

覆盖配置 / 规则 / 数据库 / 打包器 / 下载器的离线逻辑。

### 第 2 层：离线回归（不联网，秒级）

```bash
python tools\test_block.py          # WAF 拦截识别、站方故障页识别、Referer 头中文编码
python tools\test_write_guard.py    # 目录不可写时必须报人话（WriteDenied）而非裸 PermissionError
python tools\t_gui_new_pages.py     # v0.2.0 新增两页：图源仓库 + 内置浏览器，56 项断言
python tools\t_updates2.py          # 检查更新：话号解析 14 例 + latest_chapter 7 例 + 判定口径，47 项
python tools\t_gui_fav.py           # 收藏页 8 列与「上次章节数」列，9 组断言
python tools\check_isolation.py     # ★ 数据隔离护栏：从 tools\ 起的脚本必须落到临时目录
python tools\check_dev_guard.py     # ★ 反向验证：四种正常启动方式必须拿到真库
python tools\t_no_splash.py         # 启动窗口回归：不得再有 tk 启动画面（会起一次 exe，约 8 秒）
```

> **改 `tools\` 下任何脚本后，中间两条必跑。** 原因见下方「数据隔离」一节。
> 改动 `mangakit.spec` 或重打包后，再补跑 `t_exe_ver.py` 与 `t_no_splash.py`。

`test_write_guard.py` 防的回归：曾经下载器**先抓页、后检查目录能不能写**，
用户要等抓完十几张图才看到一句 `PermissionError: [WinError 5] 拒绝访问`。
现在可写性闸门排在联网之前。

`t_gui_new_pages.py` 用桩数据把两个新页面从头到尾走一遍，**全程不联网**，
实测 **56 项全过**。它盯的是四类容易在后续改动里悄悄坏掉的东西：

- **懒加载**：构造主窗口时不许碰 WebEngine（`import PySide6.QtWebEngineWidgets`
  一次都不许发生），只有切到浏览器页才允许建 `QWebEngineView`；
- **构造期不联网**：图源页构造时不发任何请求，`SourcePage` 的桩会记录调用并断言为空；
- **校验失败不写 config**：图源校验没过时绝不把半成品规则写进配置；
- **模块不可用时降级**：把 `ycksource` 的 import 打桩成失败，断言只有图源页降级
  并给出原因，其余页面与主窗口照常构建。

### 第 3 层：实网验证（联网，每站几秒）

```bash
python tools\verify.py              # 全部 19 个实网站点
python tools\verify.py comicwang kanman   # 只验某几个
```

会真的去每站抓详情页、章节列表、章节页，并下载每站 3 张图；
`cases.json` 里带 `search` 字段的用例还会断言站内搜索结果非空。
结果写到 `tools\verify.out.json`（每次重写），同时在 `<下载目录>\_verify\<站点id>\` 留下样本图。
**站点改版后跑一遍就知道哪些坏了。**

> 注意：`tools\verify.out.json` 与 `tools\_smoke\` 都是**跑测试时在本地产出的东西**，
> 已经被 `.gitignore` 排除，不会进仓库 —— 你在 GitHub 上看不到它们是正常的，
> 本文引用的 19/19、53 条、56 条与 29 项断言都来自**本机实跑的那一份**，自己跑一遍即可复现。

### 第 3.5 层：图源转换的真跑回归（联网）

```bash
python tools\t_yck_convert.py       # 真抓 yck2026 图源 → 转换 → 真下载，29 项断言
```

实测 **29 项全过**，而且不是空跑：真跑通了 **yck938（10 话 / 20 图）** 与
**yck934（1 话 / 9 图）** 两条完整链路 —— 从图源 JSON 翻译成 MangaKit 规则、
在真实引擎里过 `parse_chapters` / `parse_pages`、再把图下到磁盘上。
末尾打印 `ALL YCK TESTS PASSED`（失败则列出失败项并非 0 退出）。
加密源（`:_R002_`）的「判定为不支持」也在断言里，属于**期望行为**而非漏测。

### 第 4 层：打包（离线，几分钟）

```bash
python -m PyInstaller --noconfirm --clean mangakit.spec
python tools\t_exe_ver.py           # 核对 exe 是否比源码旧 —— 打完必跑
```

### 第 5 层：打包产物端到端（联网）

```bash
python tools\exe_smoke.py           # 每个站单独准备 MANGAKIT_HOME，跑 --url --get 1 --limit 2
                                    # 然后检查磁盘上真的落盘了图片（实测 6/6 站有图）
python tools\gui_smoke.py           # 起一次打包 exe，确认窗口存活 30 秒不秒退 + 53 条界面断言
MangaKit.exe --selftest report.txt  # 最省事的整体自查，末尾须为 SELFTEST PASS（含 webengine 项）
```

`exe_smoke.py` 抓的是**打包环境独有的问题**：相对导入、stdout 编码、资源目录、onefile 解包。
`gui_smoke.py` 抓两类只在打包后才暴露的问题：onefile 解包失败
（弹 `Could not create temporary directory!`）与 stdout 编码把中文打成 ascii。

> 实测 **53 条断言全 PASS、退出码 0**（见 `使用文档.md` 的
> 「九、目录结构（源码）→ 冒烟脚本分工」一节；
> 断言条数与 `tools/gui_smoke.py` 里的 `PASS]` 打印逐一核对得 52）。
> 里面有**九个**纯离线的界面检查：站点下拉完整性、章节勾选链路、「展开全部章节」按钮、
> 搜索流式显示 + 条目可点性 + rid 守卫、封面异步加载、收藏删除、收藏检查更新、
> 添加/删除网站、双击打开详情页 + 打开网址。
>
> 改动章节列表、下载按钮、搜索页、收藏页、设置页、详情页或切页逻辑后**必跑**。
>
> **注意这 53 条不含 v0.2.0 新增的两个页面** —— 「图源仓库」与「内置浏览器」
> 的回归在 `tools\t_gui_new_pages.py`（56 项，见第 2 层）与
> `tools\t_yck_convert.py`（29 项，见第 3.5 层）。三层加起来才是完整口径。
>
> 打包后想一次性看整体是否正常，直接跑 `MangaKit.exe --selftest report.txt`：
> config / rules / store / exporter / **webengine** 五项全 OK 时以 **`SELFTEST PASS`** 结尾。

### 调试「添加网站」探测引擎时用

```bash
python tools\t_probe.py https://songzimanhua.com/book/15293/   # 单网址走一遍探测
python tools\t_roundtrip2.py <网址>                            # 探测 → 写 user_rules → 重载 → 引擎真跑 → 清理
python tools\t_gui_addsite.py                                  # 走真 GUI 对话框 + 真探测
```

### 发布

```bash
python tools\t_publish.py --dry-run          # 只打印将执行的命令，不改动任何东西
python tools\t_publish.py                    # add → commit → push → tag → gh release create
python tools\t_publish.py -m "修好搜索翻页"    # 自定义提交信息
```

tag 默认取 `mangakit/__init__.py` 里的 `__version__`（加 `v` 前缀）。
脚本用绝对路径探测 `git` / `gh`（本机 `git` 不在 PATH 上），失败时会打印**真实 stderr** 并停下。

### 数据隔离（★ 改 `tools\` 下脚本前必读）

这里记的是一次**真实数据事故**及其护栏，不是理论风险。

**事故**：2026-10-14，用户的收藏被清空。定位结果是
`tools\gui_smoke.py` 的界面检查函数直接调了 `store().clear_favorites()`，
而该脚本**没有隔离数据目录** → `store()` 落到 `%APPDATA%\mangakit\mangakit.db`，
也就是用户的真库。测试先清空、塞 4 条测试数据、最后再清一次。

**为什么不能靠静态检查防**：第一版护栏想通过扫源码里的写接口来判断危险脚本，
结果它**放过了 `tools\t_fix_check.py`** —— 该文件全文没有写接口，却照样写进了真库，
因为写操作藏在产品代码内部：

```
MangaPage._on_comic  →  内部自动 store().add_favorite()   （v0.4.1 起已删除）
```

「这个脚本会不会写」在静态层面无法判定，所以护栏必须做成**运行时单点防护**。

> **v0.4.1 补充**：上面这条自动写入已经从产品代码里彻底删掉了（改成详情页的
> 「收藏」按钮，用户点了才写库，见「图形界面」一节的收藏说明）。
> 但护栏**不拆** —— 它防的是「写操作藏在产品代码里」这个类别，不是防这一行。
> 下次再有人往产品代码的渲染路径里塞写库操作，这条护栏仍然会兜住测试脚本。

**现在的护栏**：

| 层 | 位置 | 作用 |
| --- | --- | --- |
| 单点防护（主要） | `mangakit/config.py` 的 `_dev_sandbox()` | 只要进程由 `<仓库>\tools\xxx.py` 启动，数据目录一律改到 `%TEMP%\mk_dev_xxxx`，绝不碰 `%APPDATA%\mangakit` |
| 显式声明（辅助） | 各测试脚本顶部设 `MANGAKIT_HOME` | 必须在**任何 `import mangakit` 之前**，否则配置已按真目录读入 |
| 拉起 exe 的脚本自行隔离 | 测试脚本自己的 `MANGAKIT_HOME`（写进临时 `.bat`） | 打包后的 exe 不受 `_dev_sandbox()` 管（`sys.frozen` 必须放行，否则用户双击也被隔离），所以**起 exe 的测试必须自己把目录传进去**；且 WMI 子进程不继承脚本环境，得用临时 `.bat` 里的 `set` |
| 逃生门 | 环境变量 `MANGAKIT_ALLOW_REAL_HOME=1` | 确需连真库调试时用 |

判定依据是启动脚本路径（`sys.argv[0]`）而非当前目录，所以 `os.chdir` 不会误判。
`tools\check_isolation.py` 除了跑探针，还会做一项**结构检查**：凡是会拉起 exe 的脚本
都必须在 `_DECLARED_EXE_USERS` 里登记性质（`launch` / `upload`），新写的脚本漏登记会直接判失败 ——
这条是写 `tools\t_no_splash.py` 时踩出来的：它用 WMI 起 exe，第一版把那轮观察直接打在了真库和真日志上。

**正常使用不受影响**：`python -m mangakit`、`python main.py`、打包后的 exe、
用户自己设的 `MANGAKIT_HOME` 都照常拿真库 —— `tools\check_dev_guard.py` 逐条验证这四种。

**最有说服力的一条证据**：把当年肇事的那套测试（`tools\gui_smoke.py`，含
「清空收藏生效」这条断言）对真库跑一遍，跑完 `mangakit.db` 的 SHA256 **逐位不变**。

> 取证资料（冻结的数据库副本、字节级打捞脚本、恢复出的收藏清单）保留在
> `tools\_incident\`，**已加进 `.gitignore`** —— 里面是本机用户的真实数据库副本，
> 绝不能被提交或外发。

---

## 打包说明

- 配置：`mangakit.spec`，用 `python -m PyInstaller --noconfirm --clean mangakit.spec`。
- 产物：`dist\MangaKit.exe`，**onefile**（单文件）、**windowed**（双击不弹黑框）。
- **实测体积 171.6 MB**（171,628,901 字节，`dist\MangaKit.exe`，2026-10-18 20:53:25 构建）。
  v0.1.0 时代是 63.4 MB —— 涨上来的部分基本就是 QtWebEngine 那套 Chromium 运行库。
  v0.3.1 移除启动画面后比 v0.3.0 小了约 1.7 MB（不再打包 Tcl/Tk 那套 DLL）。
- 站点规则以 data 文件形式打包（`datas` 里 `mangakit/rules` → `_MEIPASS/mangakit/rules`），
  运行时 `main.py` 会把 `MANGAKIT_RULES_DIR` 指过去，所以**规则跟着 exe 走**。
- 解包目录固定：`runtime_tmpdir=r'%LOCALAPPDATA%\MangaKit\run'`，不再往 `%TEMP%` 里塞，
  也不再受运行时 cwd 影响（详见 `mangakit.spec` 头部注释里的取舍说明）。
- `excludes` 里砍掉了 Qt3D / Quick / Qml / Multimedia / Charts / numpy / pandas 等重型模块以压体积。
  **`PySide6.QtWebEngineCore` / `QtWebEngineWidgets` 已从 `excludes` 移除** ——
  内置浏览器页需要它们（`QtWebEngineQuick` / `QtQml` / `QtQuick` 继续排除，Widgets 版用不到）。
- `datas` 里**过滤掉 `qtwebengine_devtools_resources*.pak`**（实测 **83.4 MB**）：
  PySide6 的 PyInstaller hook 会把它收进来，但那是给 F12 远程调试用的，打包应用根本不加载。
  不过滤就白胖 83 MB。
- 重打包前先关掉运行中的 exe，否则 `dist\MangaKit.exe` 报 `PermissionError: [WinError 5]`。
- 打完跑 `python tools\t_exe_ver.py` 核对时序。

### 关于发布资产体积

GitHub 对**单个 Git 对象**有 **100 MB** 硬上限，对 **Release 资产**单个文件上限 **2 GB**。
所以 exe 一律**只作为 Release 附件上传，不提交进 Git 仓库**（`.gitignore` 已排除 `dist/` 与 `*.exe`）。

- 当前正式版 exe **171.6 MB，已经超过 100 MB 的 Git 对象上限** ——
  所以「只走 Release 附件」在 v0.2.0 起**不再是可选项，而是硬要求**：
  想提交进仓库会直接被 GitHub 拒收。
- 下载者也因此要接受 **172 MB+** 的体积，这是启用内置浏览器换来的代价
  （对比：不含 WebEngine 的 v0.1.0 是 63.4 MB）。

---

## 常见问题

### 双击 exe 弹英文框「Could not create temporary directory!」然后秒退

**v0.2.0 起这个错基本不会再出现了**，因为解包目录已经固定在
`%LOCALAPPDATA%\MangaKit\run`（`mangakit.spec` 的 `runtime_tmpdir`），
不再依赖 `%TEMP%` 是否健康（条目过多、被清理软件锁住、磁盘满、被安全软件拦截）。

真遇到了，按顺序排查：

1. 确认你手上是 v0.2.0 及以后的 exe（右键 → 属性 → 详细信息 → FileVersion，见下文）。
   老 exe 仍走 `%TEMP%`，仍会踩这个坑。
2. `%LOCALAPPDATA%` 本身不可写（少见，常见于被策略锁死的机器）——
   手工 `mkdir` 一个可写目录，把 `TEMP` / `TMP` 指过去再启动。
3. 兜底入口还在：双击 **`启动漫卷.bat`**。它会把 `TEMP` / `TMP` 指向 exe 旁边一个干净的
   `.tmp` 文件夹（只影响 Python 层与子进程，解包位置仍由 exe 自己决定），
   并顺手清掉上次崩溃留下的 `_MEI*`（且只在没有实例运行时才清）。

### 启动后什么都没发生，日志也没有

先看是不是 `%LOCALAPPDATA%` 不可写导致解包失败（同上）。新版还多一层保护：
`_startup_preflight()` 在启动时就会检查下载目录能不能真建文件，不能会**弹 GUI 提示**
说清楚是哪个目录、什么原因、怎么办 —— 有提示窗口说明程序已经起来了，按提示走即可；
连提示都没有才是解包阶段就挂了。

### 内置浏览器第一次打开要等几秒？

正常现象。QtWebEngine 是**懒加载**的：主窗口构造时一行 WebEngine 代码都不碰，
只有你真的切到「内置浏览器」页（导航第 7 项）或点「在浏览器中打开」，
才去 import `PySide6.QtWebEngineWidgets` —— onefile 打包版这时要解包上百兆 Chromium 运行库，
**首次约 3-4 秒**，之后就即开即用了。这是刻意的设计，否则每次启动都要白等这几秒。

如果一直白屏 / 加载不出来：先跑 `MangaKit.exe --selftest report.txt`，
看 `webengine` 那一项是不是 PASS。**冻结态必须关掉 Chromium 沙箱** ——
`mangakit/main.py` 的 `_configure_webengine_sandbox()` 会在 import QtWebEngine **之前**
设好 `--no-sandbox` / `QTWEBENGINE_DISABLE_SANDBOX=1`，
否则 `QtWebEngineProcess.exe` 起不来，表现为**视图建得出来、`loadFinished` 却收到
`ok=False`、页面永远白**（源码运行看不出来，只有打包版会中）。改了这里务必重跑自检。

### 下载报「拒绝访问 / Permission denied」

这是最常见的一类报错，而且**大多数情况不是程序坏了，是程序被放在了一个写不进去的地方**。

新版在下载前会做一次真实落盘自检，失败时直接报：

```
无法写入目录：C:\Users\你的用户名\Downloads\MangaKit\某本漫画
原因：系统拒绝访问（权限不足，或该目录被其它程序占用）
怎么办：
  1) 把 MangaKit.exe 移到桌面或「文档」目录再运行
  2) 或在「设置 → 下载目录」改一个能写的路径
  3) 关掉占用该目录的其它程序
```

按顺序排查：

| 现象 | 判断 | 对策 |
| --- | --- | --- |
| exe 在**开发工具 / 沙箱 / 隔离环境的工作区**里 | 工作区外的路径被系统策略限制，**建目录常能成功，但建文件一定失败** | 把 exe 复制到桌面或「文档」再运行 |
| 程序旁边出现了 `MangaKitData` 文件夹 | `%APPDATA%` 不可写，程序退回了便携模式 —— **同一个病的症状** | 同上，换个位置运行 |
| exe 装在 `C:\Program Files` 下 | 普通用户对子目录没有写权限 | 用便携模式，或把 exe 移走 |
| 下载目录被别的程序占用 | 图片查看器 / 同步盘 / 杀软锁住了目录 | 关掉占用它的程序，或换一个空目录 |
| 磁盘满了 | — | 清理磁盘；程序会报「磁盘空间不足」而不是「拒绝访问」 |
| 启动时弹出「下载目录不可写」的提示框 | `_startup_preflight()` 启动自检已预先测出问题 | 按提示把 exe 移出受限目录，或在设置里改下载目录 |

> **怎么确认是「程序位置」的问题**：把 exe 原样复制到桌面，对同一部漫画再点一次下载。
> 如果桌面这份能下载，问题就出在原来那个位置，跟 MangaKit 无关。

### QtWebEngine / 内置浏览器怎么这么大？

**内置浏览器已经实现**（v0.2.0，导航第 7 项），所以 QtWebEngine 的那套 Chromium
运行库是真的打进 exe 了 —— 这就是体积从 63.4 MB 涨到 **171.6 MB** 的原因：

| 项 | 实测 |
| --- | --- |
| 正式版 exe 体积 | **171,628,901 字节（≈171.6 MB）**，2026-10-18 20:53:25 构建（v0.4.1） |
| 对比 v0.1.0（无 WebEngine） | 63.4 MB —— 差出来的约 109 MB 基本就是 Chromium |
| v0.3.1 减掉的 | 约 1.7 MB（移除启动画面，不再打包 `tcl86t.dll` 905 KB + `tk86t.dll` 707 KB + `Splash-00.res` 62 KB） |
| 被过滤掉的调试资源 | `qtwebengine_devtools_resources*.pak`，实测 **83.4 MB**（只给 F12 远程调试用） |
| 渲染进程 | QtWebEngine 会拉起独立的 `QtWebEngineProcess.exe` 子进程 |
| 首次进入耗时 | 约 3-4 秒（懒加载，见上文 FAQ） |

体积上的两个关键处理：

- **`qtwebengine_devtools_resources*.pak` 必须过滤**（`mangakit.spec` 里按
  `"devtools_resources" not in d[0]` 从 `datas` 里剔掉）：PySide6 的 PyInstaller hook
  会把它收进来，83.4 MB，而打包后的应用根本不加载它。不过滤就白胖 83 MB。
- **冻结态必须关 Chromium 沙箱**：`mangakit/main.py` 的 `_configure_webengine_sandbox()`
  在**任何** `import PySide6.QtWebEngine*` **之前**设好
  `QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox` 与 `QTWEBENGINE_DISABLE_SANDBOX=1`。
  不设的话渲染进程起不来，症状是**视图建得出来、`loadFinished` 收到 `ok=False`、
  页面永远白**（源码运行复现不了，只有 onefile 打包版会中）。

沙箱相关的坑（排查白屏时按这个顺序看）：

- 渲染进程需要能写自己的数据目录 / 缓存目录；在受限工作区里跑时，
  要给 `QTWEBENGINE_DISABLE_SANDBOX` / `--no-sandbox` 或把
  `QTWEBENGINE_CHROMIUM_FLAGS` 里的 `--user-data-dir` 指到一个**可写**的路径，
  否则渲染进程起不来、页面全白。
- 打包时必须把 `QtWebEngineProcess.exe`、`qtwebengine_resources.pak`、`resources/`、
  `translations/` 一起带上，漏一个就是白屏或直接崩溃。
  `PySide6.QtWebEngineQuick` / `QtQml` / `QtQuick` 可以继续 `excludes`
  （Widgets 版用不到），但 `QtWebEngineCore` / `QtWebEngineWidgets` **不能排**。

**最省事的自查方式**：`MangaKit.exe --selftest report.txt`，
`webengine` 那一项就是真起一个 `QWebEngineView` 加载页面并断言 JS 跑通，
结论看末尾是否 `SELFTEST PASS`。这比在 GUI 里试快得多。

> 顺带说明：漫画下载本身**完全不依赖** WebEngine，解析仍走 `httpx` + `lxml`。
> WebEngine 只在你要在应用里看网页（过验证码、看防盗链页面、逛图源索引）时才派上用场，
> 所以它是懒加载的 —— 不点那一页，它就不会占用启动时间和内存。

### 如何确认手上的 exe 是新版？

v0.1.0 那会儿 `__version__` 一直是 `0.1.0`，而你手上那个 exe 可能是几天前打的 ——
**「修了却测不出来」是这类项目最费时间的坑**：用户运行的是 17:35 打的 exe，
而源码 18:06 才加上流式搜索，用户那边跑的还是老逻辑
（`search_done` 回的是列表而非总数，且没有 `search_partial`）—— 见 `使用文档.md`
的「九、目录结构（源码）→ 冒烟脚本分工」一节。

现在版本号已抬到 **0.4.1**，而且 **exe 内嵌了版本资源**（`assets/version_info.txt`，
版本号从 `mangakit/__init__.py` 的 `__version__` 现读），所以确认方式很直接：

1. **看属性（最直接）**：右键 `MangaKit.exe` → 属性 → **详细信息** →
   看 **FileVersion**。是 `0.4.1` 就是最新版；`0.4.0`/`0.3.3`/`0.3.2`/`0.3.1`/`0.3.0`/`0.2.0`/`0.1.0` 或空白就是老的。
   另外 `0.3.1` 起**双击后不应再出现任何 `tk` 小窗口**（那是老版的坏启动画面）；
   `0.3.2` 起**日志里不应再有 `pyi_splash` 警告**（`%APPDATA%\mangakit\logs\mangakit.log`）；
   `0.3.3` 起**漫画名/章节名是 `CON`、`NUL` 这类保留设备名、或路径过长时也能正常下载**。
2. **跑自检**：`MangaKit.exe --selftest report.txt`，末尾 `SELFTEST PASS` 且
   含 `webengine` 项才算完整的新版（老 exe 没有 webengine 这一项）。
3. **跑时序核对**（改代码后自查最有用）：

   ```bash
   python tools\t_exe_ver.py
   ```

   它会 WATCH `dist\MangaKit.exe` 与 `main_window.py` / `bridge.py` / `service.py` / `models.py`，
   逐个打印修改时间，比 exe 新的会标 `<<< 比 exe 新，exe 里没有这次改动！`，
   最后给结论：「exe 落后于源码，必须重新打包才能复现/验证最新修复。」
   或「exe 不比源码旧。」
4. **发布时用 tag 区分**：`tools\t_publish.py` 的 tag 取 `__version__`，
   每次发版前先把 `mangakit/__init__.py` 的 `__version__` 往上加，
   这样 Release 页面上的 tag 与附件名就能区分新旧。

### 站方报「被站点防火墙拦截，状态码 xxx」/「站点服务端异常：数据库链接失败…」

前者是站方 WAF 拦了这个 IP/UA，不是程序坏了 —— 换个网络（手机热点常能过）、
过一会儿再试、或改用其它能用的站点。**本程序不会用伪装爬虫的 UA 去绕风控。**

后者是**站方服务器自己出故障**（数据库连接池打满、内部错误），网页只有一句报错、
没有漫画内容，程序已能区分这种「HTTP 200 但正文是故障回执」的情况，
不会误报成「没有章节」。等一会儿再试，同一个站过几分钟往往自己恢复。

### 图片全部 403 / 下载到的是 HTML

403 多半是缺 Referer 或签名过期：设置里 Referer 改成「章节页」；
kanman 的图片地址带时间戳签名，正确做法是**重新打开该章节页**让程序重新解析一次再下载，
不要拿几小时前复制的旧章节列表反复重试。
下载到 HTML 则是站方风控返回了验证页：降低并发、调大「同域限速」、过一会儿再试。

### 我要的站不在列表里怎么办？

两条路：

1. **自己加**：设置 → 站点管理 → 「添加网站」，贴详情页网址，程序自己探测能不能下。
   详见上文「站点规则 → 方式一」。
2. **从图源仓库里挑**（v0.2.0 新增）：切到导航第 6 项「图源仓库」，
   搜 yck2026 的图源索引，选中一条点「校验并添加」——
   通过的会直接变成你的一条自定义站点规则。适合那些「自己探测不过、
   但别人已经写好图源」的站（这类站通常是靠 JavaScript 现拼图片地址的）。
   校验不通过会写清楚原因（加密源 / 语法不支持 / 引擎跑不通），**不写半成品进配置**。

---

## 数据与隐私

- 所有抓取、解析、下载**都在本机完成，不经过任何第三方服务器**。
- 只向漫画站点发请求，程序不会上传任何数据。
- 数据都在 `%APPDATA%\mangakit\`：配置、数据库、日志
  （该目录不可写时自动改用 exe 旁的 `MangaKitData\`）。
- 卸载：删掉 exe 和 `%APPDATA%\mangakit\` 目录即可，**没有注册表项、没有系统服务**。
- 想换数据目录，设环境变量 `MANGAKIT_HOME`。

---

## 免责声明

- 本项目**仅供个人学习、技术研究与离线备份使用**。请勿用于批量抓取受版权保护的内容或任何商业用途。
- 请**尊重目标站点的版权与服务条款**，遵守各站点的 `robots.txt`。下载的内容版权归原作者与发布平台所有，
  请勿二次分发、传播或用于任何盈利行为。
- 本项目**不破解、不绕过任何付费内容与登录限制**。遇到需要付费或登录才能看的章节，
  程序会直接失败并报错，这是刻意设计。
- 本项目**不做图源解密**：对异次元（yck2026）等平台上标注为**「加密源」**的图源
  （关键字段是 `:_R002_…` 这类不透明块、由阅读器 App 内部解密的），
  程序**直接判定为不支持**，不会尝试逆向或解密。仅支持语法可翻译、且能在真实引擎里
  跑通 `parse_chapters` / `parse_pages` 的明文图源 —— 实测抽 60 条图源，
  **只有 14 条**属于这种可转的明文源，其余 46 条不可转（其中 32 条是 `:_R002_` 加密串）。
- 本项目**不会用伪装爬虫的 UA 绕过站点风控**。被 WAF 拦截时程序如实报错并建议你换站点或稍后再试。
- 使用本软件产生的一切后果由使用者自行承担。站点改版会导致规则失效，请以 `tools\verify.py` 的实跑结果为准。

---

## 许可

漫卷 MangaKit v0.4.1 ｜ 仅供个人学习与离线备份使用。

下载的内容请自行遵守各站点的使用条款。完整使用说明见 [使用文档.md](使用文档.md)。
