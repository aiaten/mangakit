# -*- coding: utf-8 -*-
"""内置适配器回归：注册时序 + 章节解析取节点。

背景（为什么单独立一个套件）
----------------------------
这两个 bug 都能让三个内置站（mstation / kuaikan / kzdm）**整体失效**，
而现有的六层测试全都没覆盖到它们：

  1. **注册时序**：`SiteRegistry.__init__` 里 `from . import adapters`，
     而 adapters 模块在导入时反过来调 `registry()`。那一刻模块级 `_registry`
     还是 None（外层正在 `_registry = SiteRegistry()` 的右边求值），于是递归
     建出**第二个**注册表，适配器装进了内层；内层对象被外层覆盖，最终单例
     指向没有适配器的那个 —— 三个站全部报「没有可用适配器」。
     selfcheck.py 之所以一直能过，是因为它第 9 行就 `from mangakit import
     adapters`，抢先把模块导入完了；GUI 主程序（main_window.py）没这句，
     所以线上必现。

  2. **find_all / find_nodes 混用**：`Selector(...).find_all()` 返回的是
     **已提取好的字符串列表**（元素是 str，没有 .get()），要自己取属性必须用
     `find_nodes()` 拿原始 lxml 节点。adapters.py 三处 parse_chapters 照抄
     `for n in ...find_all(): n.get("href")`，`hasattr(n, "get")` 恒为 False，
     href 恒空 —— 章节列表恒为空。

    这三个站没有 rules/*.json 兜底（纯靠 adapters.py），所以一旦挂了就是
    彻底不可用，而 tools/cases.json 的 19 个实网 case 里没有它们任何一个，
    verify.py 19/19 通过完全覆盖不到。

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'
    <py> tools\\t_adapters.py
全部通过时末行打印 === 内置适配器回归全部通过 ===，退出码 0。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据隔离：本套件只解析 HTML 字符串，但 tools/ 下的脚本一律显式隔离，
# 不依赖 config._dev_sandbox() 兜底（见 check_isolation.py）。
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_adp_"))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------ 1. 冷启动注册
# 必须用**全新解释器**验证：本进程（以及 pytest/selfcheck）可能已经 import 过
# adapters，那样就测不出「GUI 主程序没 import adapters」这条真实路径。

_COLD = """
import sys
from mangakit.sites import registry
r = registry()
print("ADAPTERS=" + ",".join(sorted(r._adapters)))
print("SITES=" + ",".join(sorted(r.sites)))
for u in ("https://m.dm5w.com/manga/123/",
          "https://www.kuaikanmanzhen.com/comic/1",
          "https://www.kzdm.com/manhuaziyuan/"):
    s = r.site_for_url(u)
    print("HIT=" + (s.id if s else "None"))
print("FORURL=" + str(type(r.for_url("https://m.dm5w.com/manga/123/")[1]).__name__))
"""

_NO_SIDE_EFFECT = """
import mangakit.adapters
import mangakit.sites as S
print("REGISTRY_IS_NONE=" + str(S._registry is None))
"""


def _run(code: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         env=env, timeout=180)
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def test_cold_start() -> None:
    print("== 1. 冷启动：只 import mangakit.sites 也要有内置适配器 ==")
    rc, txt = _run(_COLD)
    lines = dict(ln.split("=", 1) for ln in txt.splitlines() if "=" in ln)
    check("子进程正常退出", rc == 0, f"rc={rc} {txt.strip()[:200]}")
    want = "kuaikan,kzdm,mstation"
    check("三个内置适配器都已注册", lines.get("ADAPTERS") == want,
          f"got={lines.get('ADAPTERS')!r} want={want!r}")
    sites = (lines.get("SITES") or "").split(",")
    for sid in ("mstation", "kuaikan", "kzdm"):
        check(f"站点 {sid} 存在", sid in sites)
    hits = [ln.split("=", 1)[1] for ln in txt.splitlines() if ln.startswith("HIT=")]
    check("site_for_url 命中 mstation/kuaikan/kzdm",
          hits == ["mstation", "kuaikan", "kzdm"], f"got={hits}")
    check("for_url 返回内置适配器实例（不是 RuleAdapter）",
          lines.get("FORURL") == "MStationAdapter", f"got={lines.get('FORURL')!r}")

    print("== 2. import mangakit.adapters 不应有注册副作用 ==")
    rc, txt = _run(_NO_SIDE_EFFECT)
    lines = dict(ln.split("=", 1) for ln in txt.splitlines() if "=" in ln)
    check("子进程正常退出", rc == 0, f"rc={rc} {txt.strip()[:200]}")
    check("import adapters 后仍无全局注册表（不再 import 时自注册）",
          lines.get("REGISTRY_IS_NONE") == "True",
          f"got={lines.get('REGISTRY_IS_NONE')!r}")


# ------------------------------------------------------------ 3. reload 不丢
def test_reload() -> None:
    print("== 3. reload_registry() 之后适配器仍在 ==")
    from mangakit.sites import registry, reload_registry
    r1 = registry()
    n1 = len(r1._adapters)
    r2 = reload_registry()
    check("reload 返回了新实例", r2 is not r1)
    check("新实例仍有 3 个内置适配器", sorted(r2._adapters) == ["kuaikan", "kzdm", "mstation"],
          f"got={sorted(r2._adapters)}")
    check("reload 前后适配器数量一致", len(r2._adapters) == n1, f"{n1} -> {len(r2._adapters)}")
    for sid in ("mstation", "kuaikan", "kzdm"):
        check(f"reload 后 {sid} 站点仍可用",
              r2.site_for_url({"mstation": "https://m.dm5w.com/manga/1/",
                               "kuaikan": "https://www.kuaikanmanzhen.com/comic/1",
                               "kzdm": "https://www.kzdm.com/x/"}[sid]) is not None)


# ------------------------------------------------- 4-6. parse_chapters 取节点
MSTATION_HTML = """
<html><body><div id="chapterlist">
  <ul>
    <li><a href="/manga/9/100.html">第3话</a></li>
    <li><a href="/manga/9/98.html">第1话</a></li>
    <li><a href="/manga/9/99.html">第2话</a></li>
    <li><a href="/manga/9/100.html">第3话（重复链接）</a></li>
  </ul>
</div></body></html>
"""

KUAIKAN_HTML = """
<html><body><div class="chapter-list">
  <a href="/comic/7/9001/1">第1话 起点</a>
  <a href="/comic/7/9002/1">第2话 继续</a>
</div></body></html>
"""

KZDM_HTML = """
<html><body><div id="chapterlist">
  <a href="/book/55/1001">第1话</a>
  <a href="/book/55/1002">第2话</a>
</div></body></html>
"""


def _mk_adapter(cls_name: str, html: str):
    """造一个「fetch 被桩掉」的适配器实例；站点缺失时返回 None。

    返回 None 而不是抛异常：注册时序 bug 会让 mstation/kuaikan/kzdm 三个站
    整体不存在，那时下面每个用例都该报 FAIL 说清「站没了」，而不是让整个
    套件崩在 KeyError 上——崩掉的测试读不出「到底哪条契约破了」。
    """
    from mangakit.sites import registry
    from mangakit import adapters
    reg = registry()
    sid = {"MStationAdapter": "mstation",
           "KuaikanAdapter": "kuaikan",
           "KZAdapter": "kzdm"}[cls_name]
    site = reg.sites.get(sid)
    if site is None:
        return None
    ad = getattr(adapters, cls_name)(site, None)

    async def fake_fetch(url: str, **kw) -> str:
        return html

    ad.fetch = fake_fetch  # 实例属性遮蔽基类方法，不打真网
    return ad


def test_parse_chapters() -> None:
    print("== 4. MStationAdapter.parse_chapters 能取到 href ==")
    ad = _mk_adapter("MStationAdapter", MSTATION_HTML)
    if ad is None:
        check("mstation 站点存在（注册时序）", False, "站点缺失，跳过解析断言")
        return
    chs = asyncio.run(ad.parse_chapters("https://m.dm5w.com/manga/9/"))
    check("解析到 3 话（重复链接已去重）", len(chs) == 3, f"got={len(chs)}")
    check("href 被拼成绝对地址",
          chs and chs[0].url.startswith("https://m.dm5w.com/manga/9/"),
          f"got={chs[0].url if chs else None!r}")
    check("标题取到 <a> 文本", [c.title for c in chs] == ["第1话", "第2话", "第3话"],
          f"got={[c.title for c in chs]}")
    check("按话号排序且 index 连续", [c.index for c in chs] == [0, 1, 2],
          f"got={[c.index for c in chs]}")

    print("== 5. KuaikanAdapter.parse_chapters 能取到 href ==")
    ad = _mk_adapter("KuaikanAdapter", KUAIKAN_HTML)
    if ad is None:
        check("kuaikan 站点存在（注册时序）", False, "站点缺失，跳过解析断言")
        return
    chs = asyncio.run(ad.parse_chapters("https://www.kuaikanmanzhen.com/comic/7"))
    check("解析到 2 话", len(chs) == 2, f"got={len(chs)}")
    check("href 被拼成绝对地址",
          chs and chs[0].url == "https://www.kuaikanmanzhen.com/comic/7/9001/1",
          f"got={chs[0].url if chs else None!r}")

    print("== 6. KZAdapter HTML 兜底路径能取到 href ==")
    ad = _mk_adapter("KZAdapter", KZDM_HTML)
    if ad is None:
        check("kzdm 站点存在（注册时序）", False, "站点缺失，跳过解析断言")
        return

    async def boom(path: str):
        raise RuntimeError("API 挂了（模拟 app.kzdm.com 不可用）")

    ad._api = boom  # 强制走 HTML 兜底分支
    chs = asyncio.run(ad.parse_chapters("https://www.kzdm.com/book/55"))
    check("API 失败后仍从 HTML 解析到 2 话", len(chs) == 2, f"got={len(chs)}")
    check("href 被拼成绝对地址",
          chs and chs[0].url == "https://www.kzdm.com/book/55/1001",
          f"got={chs[0].url if chs else None!r}")


# --------------------------------------------------------- 7. find_nodes 语义
def test_selector_contract() -> None:
    print("== 7. Selector 契约：find_all 出字符串、find_nodes 出节点 ==")
    from lxml import html as LH
    from mangakit.sites import Selector
    doc = LH.fromstring(MSTATION_HTML)
    spec = {"css": "#chapterlist a"}
    nodes = Selector(spec).find_nodes(doc)
    check("find_nodes 返回原始节点", nodes and hasattr(nodes[0], "get"),
          f"type={type(nodes[0]).__name__ if nodes else None}")
    check("find_nodes 能取到 href", bool(nodes and nodes[0].get("href")),
          f"got={nodes[0].get('href') if nodes else None!r}")
    vals = Selector(spec).find_all(doc, "https://m.dm5w.com/manga/9/")
    check("find_all 返回字符串（不是节点）", vals and isinstance(vals[0], str),
          f"type={type(vals[0]).__name__ if vals else None}")
    check("find_all 元素没有 .get 方法（所以不能拿来取属性）",
          bool(vals) and not hasattr(vals[0], "get"))


# --------------------------------------------------- 8. 站点启用状态要落盘
def test_enabled_persist() -> None:
    print("== 8. 设置页取消勾选站点后重启仍保持（enabled 落盘）==")
    from mangakit.config import config
    from mangakit.sites import registry, reload_registry

    reg = registry()
    sid = "mstation"
    check(f"{sid} 初始为启用", reg.sites[sid].enabled is True)

    reg.set_enabled(sid, False)
    check("内存里已禁用", reg.sites[sid].enabled is False)
    saved = (config().get("sites") or {}).get(sid) or {}
    check("已写进 config.sites", saved.get("enabled") is False, f"got={saved!r}")

    # 模拟重启：丢弃注册表重新构建，看禁用状态有没有被重新读回来
    reg2 = reload_registry()
    check("重建注册表后仍为禁用（重启不丢）", reg2.sites[sid].enabled is False,
          f"got={reg2.sites[sid].enabled!r}")
    check("禁用站不再参与 site_for_url 匹配",
          reg2.site_for_url("https://m.dm5w.com/manga/1/") is None)

    # 批量接口：一次写盘、状态全部生效
    states = {s.id: True for s in reg2.all_sites()}
    states[sid] = False
    reg2.set_enabled_many(states)
    check("批量设置后 mstation 仍禁用", reg2.sites[sid].enabled is False)
    others = [s.id for s in reg2.all_sites() if s.id != sid and not s.enabled]
    check("批量设置后其它站都是启用", others == [], f"got={others}")

    reg3 = reload_registry()
    check("批量设置同样落盘", reg3.sites[sid].enabled is False,
          f"got={reg3.sites[sid].enabled!r}")

    # 复原，别把状态留给后面的用例
    reg3.set_enabled(sid, True)
    check("复原为启用", reload_registry().sites[sid].enabled is True)


def main() -> int:
    print(f"仓库根：{ROOT}")
    test_cold_start()
    test_reload()
    test_parse_chapters()
    test_selector_contract()
    test_enabled_persist()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        print("=== 内置适配器回归有失败项 ===")
        return 1
    print("=== 内置适配器回归全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
