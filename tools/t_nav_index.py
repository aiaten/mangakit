# -*- coding: utf-8 -*-
"""离线回归：主窗口导航行号 / 堆栈索引 / 页面常量三者必须始终一致。

背景：以前 `_nav()` 里写的是 `if i == 3: self.favs.reload()`、`if i == 5:
self.sources.ensure_loaded()`，`_goto()` 里写的是 `_goto(1)` / `_goto(2)` /
`_goto(6)`。这些裸数字一旦有人往导航里插一页、或把两页换个位置，就会**静默**
错位：点「收藏」却触发了图源仓库的联网、点「下载」跳到设置页——不报错、不崩、
测试也未必失败，只是行为莫名其妙。

现在定义集中在两处：
  - `main_window.PG_SEARCH … PG_BROWSER = range(7)`（模块级常量）
  - `MainWindow.__init__` 里的 `pages = ((导航文字, 页面), …)` 元组

本脚本不启动真 GUI，只做静态+轻量对象检查：
  ① 常量个数与顺序正确；
  ② `_nav` / `_goto` 里不再出现裸数字索引；
  ③ `pages` 元组的顺序与常量一一对应（用正则从源码里抠出来比对）；
  ④ 每个常量都能在 `pages` 里找到对应页面属性名。

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'; $env:PYTHONIOENCODING='utf-8'
    <py> tools\\t_nav_index.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_nav_"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = ROOT / "mangakit" / "gui" / "main_window.py"
TEXT = SRC.read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def strip_comments(code: str) -> str:
    """去掉 # 注释（本文件里注释里会提到 i == 3 这类字样，不能算违规）。"""
    out = []
    for line in code.splitlines():
        # 粗略处理：字符串里的 # 极少见，这里够用
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


CODE = strip_comments(TEXT)

# ------------------------------------------------------------------ ① 常量
print("== 1. 页面索引常量 ==")
import importlib
mw = importlib.import_module("mangakit.gui.main_window")

names = ["PG_SEARCH", "PG_MANGA", "PG_TASKS", "PG_FAVS",
         "PG_SETTINGS", "PG_SOURCES", "PG_BROWSER"]
vals = [getattr(mw, n, None) for n in names]
check("七个常量都存在", [v is not None for v in vals], [True] * 7)
check("取值为 0..6", vals, list(range(7)))

# ------------------------------------------------------------------ ② 无裸数字
print("\n== 2. _nav / _goto 里不得再有裸数字索引 ==")
# 抓 _nav 与 _goto 的函数体（到下一个同缩进 def 为止）
def func_body(name: str) -> str:
    """抠出某个方法的函数体。

    签名可能带返回类型标注（`def _goto(self, i: int) -> None:`），所以不能
    用 `\\):` 收尾，要一直吃到行尾的冒号。
    """
    pat = re.compile(rf"^( *)(?:async )?def {name}\(.*?:$", re.M)
    m = pat.search(CODE)
    if not m:
        return ""
    indent = len(m.group(1))
    rest = CODE[m.end():]
    lines = rest.splitlines()
    body = []
    for ln in lines:
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        body.append(ln)
    return "\n".join(body)


nav_body = func_body("_nav")
goto_body = func_body("_goto")
check("_nav 函数体非空", bool(nav_body.strip()), True)
check("_goto 函数体非空", bool(goto_body.strip()), True)

# _nav 里形如 `if i == 3:` / `elif i == 5:` 的数字比较（if 和 elif 都要查）
nav_bad = re.findall(r"\b(?:el)?if\s+i\s*==\s*(\d+)", nav_body)
check("_nav 里没有 if/elif i == <数字>", nav_bad, [])
nav_good = re.findall(r"\b(?:el)?if\s+i\s*==\s*(PG_\w+)", nav_body)
check("_nav 用的是 PG_ 常量", nav_good, ["PG_FAVS", "PG_SOURCES"])

# 全文搜索 _goto(<数字>)
goto_bad = re.findall(r"_goto\((\d+)\)", CODE)
check("全文没有 _goto(<数字>)", goto_bad, [])

# ------------------------------------------------------------------ ③ pages 顺序
print("\n== 3. pages 元组顺序与常量一一对应 ==")
mp = re.search(r"pages\s*=\s*\((.*?)\)\s*\n", CODE, re.S)
check("找到 pages 元组", mp is not None, True)
if mp:
    # 抠出 ("文字", self.xxx) 对
    pairs = re.findall(r'\("([^"]+)",\s*self\.(\w+)\)', mp.group(1))
    check("pages 有 7 项", len(pairs), 7)
    expect_pages = ["search", "manga", "tasks", "favs",
                    "settings", "sources", "browser"]
    check("页面属性顺序", [p[1] for p in pairs], expect_pages)
    expect_labels = ["搜索 / 打开", "漫画详情", "下载任务", "收藏",
                     "设置", "图源仓库", "内置浏览器"]
    check("导航文字顺序", [p[0] for p in pairs], expect_labels)

# ------------------------------------------------------------------ ④ 常量 ↔ 页面
print("\n== 4. 每个 PG_ 常量都有对应页面 ==")
const_to_page = {
    "PG_SEARCH": "search", "PG_MANGA": "manga", "PG_TASKS": "tasks",
    "PG_FAVS": "favs", "PG_SETTINGS": "settings", "PG_SOURCES": "sources",
    "PG_BROWSER": "browser",
}
if mp:
    props = [p[1] for p in pairs]
    for c, page in const_to_page.items():
        idx = getattr(mw, c, None)
        ok = (idx is not None and 0 <= idx < len(props) and props[idx] == page)
        print(f"  [{'OK ' if ok else 'FAIL'}] {c} -> [{idx}] {props[idx] if idx is not None and idx < len(props) else '?'} "
              f"(期望 {page})")
        if not ok:
            FAILED.append(c)

# ------------------------------------------------------------------ ⑤ 静态一致性
print("\n== 5. 导航栏与堆栈用的是同一份 pages（不各写一遍）==")
check("导航栏从 pages 建", bool(re.search(r"for name, page in pages:", CODE)), True)
check("堆栈从 pages 加", bool(re.search(r"for _name, page in pages:", CODE)), True)

print("\n" + ("=" * 46))
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
