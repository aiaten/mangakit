## 本版重点

### 1. 收藏「检查更新」改按话号最大判定最新章节

修的是用户实际遇到的问题：**有些站的章节是倒序的，检查完「最新章节」显示成第 1 话。**

- 旧逻辑取的是**列表位置序号**（`max(ch.index)`），而 `index` 是 `parse_chapters()`
  解析完按顺序 `enumerate` 赋的，排序依据是 URL 字典序 —— 倒序站上最后一条就是第 1 话。
- 实测复现：manhuagui_cc《香格里拉》254 话，index 253 = 「第1话 你为何而游戏」。
- 新逻辑用**严格话号正则**取最大：认 `第12话 / 第 12 話 / 第12回 / Chapter 12 /
  12话 / 第12章 / 第3卷`；认不出话号的条目（「2026新年贺图」「序章」「番外篇」）
  不参与比较；若**过半**条目都认不出话号，整站退回按列表位置。

### 2. 新增「上次章节数」列

收藏表从 7 列变 8 列（漫画 / 作者 / 来源 / 最新章节 / **上次章节数** / 状态 / 上次检查 / 网址）。

- 显示上一次检查时源站共多少话，与本次结果并排即可看出有没有更新（涨跌标橙）。
- 关键是**点完检查后这一列仍显示旧值** —— 否则「上次」会立刻等于「本次」，这一列就没意义了。
  实现上把写之前的 `last_total` 挪进 `favorites.base_total`，界面读的是它。
- 更新判定以**章节数差**为主判据，话号做补强（真站话号有跳号：实测 1068 话里话号只到 1162）。

### 3. ★ 修复一起真实数据事故，并加上数据隔离护栏

**事故**：2026-10-14，用户的收藏被清空。

- 根因：`tools/gui_smoke.py` 的界面检查直接调 `store().clear_favorites()`，
  而该脚本没有隔离数据目录 → 打在 `%APPDATA%\mangakit\mangakit.db`（用户真库）。
- **已从字节级打捞恢复全部 6 条收藏**（海贼王 / 猎人 / 全职猎人 / 香格里拉 / 龙珠 /
  comicwang 9286），另补齐了被覆写记录的标题与封面。
- **护栏（运行期单点防护）**：`mangakit/config.py` 新增 `_dev_sandbox()` ——
  只要进程是从 `<仓库>/tools/xxx.py` 启动的，数据目录一律改到 `%TEMP%\mk_dev_xxxx`，
  绝不碰 `%APPDATA%\mangakit`。逃生门 `MANGAKIT_ALLOW_REAL_HOME=1`。
- 为什么不做静态检查：第一版护栏想扫源码里的写接口，结果**放过了 `tools/t_fix_check.py`** ——
  它全文没有写接口，却照样写进真库，因为写操作藏在产品代码里
  （`MangaPage._on_comic` 内部自动 `store().add_favorite()`）。「会不会写」无法静态判定。
- **正常使用不受影响**：`python -m mangakit` / `main.py` / 打包 exe / 显式 `MANGAKIT_HOME`
  四种都照常拿真库，由 `tools/check_dev_guard.py` 逐条验证。
- 最有力的一条证据：把当年肇事的那套测试对真库跑一遍，跑完 `mangakit.db` 的 SHA256
  **逐位不变**（已作为一条断言加进 `gui_smoke.py`，现为 53 条）。

## 回归结果（全部真实运行）

| 检查项 | 结果 |
| --- | --- |
| `selfcheck.py` | 全绿 |
| `tools/verify.py` | **19/19** 站点通过 |
| `tools/gui_smoke.py` | **53 PASS / 0 FAIL** |
| `tools/exe_smoke.py` | **6/6** 站有真实图片落盘 |
| `tools/t_updates2.py` | **47 项全过**（离线） |
| `tools/t_updates.py` | 全过（真机） |
| `tools/t_gui_fav.py` | **9 组断言全过** |
| `tools/t_yck_convert.py` | **29 项全过** |
| `tools/t_gui_new_pages.py` | **56 项全过** |
| `tools/check_isolation.py` | 全过 |
| `tools/check_dev_guard.py` | 全过 |
| `MangaKit.exe --selftest` | **SELFTEST PASS** |

## 其他

- 测试脆弱性修复：`gui_smoke.py` 里「自建站点数量」原本硬编码断言 `自建 1`，
  但用户自己通过图源仓库加过站 → 实测显示「自建 2」，测试在真实配置上必然失败。
  已改成先读当前数量再断言。
- 新增两个离线回归脚本：`tools/t_updates2.py`（47 项）、`tools/t_gui_fav.py`（9 组）。
- `.gitignore` 补上 `/tools/_incident/`（事故取证目录，内含本机用户真实数据库副本，
  **绝不能提交**）与 `/tools/_*.py`、`/tools/_*.txt`、`/tools/_*.json`
  （下划线前缀 = 用完即弃的排查脚本）。

**资产**：`MangaKit.exe`（174.6 MB，单文件，双击即可运行，FileVersion = 0.3.0）。
