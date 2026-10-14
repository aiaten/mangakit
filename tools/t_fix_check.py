"""验证本轮修复：详情页 status 真的显示 + check_updates 有守卫。

用法：python tools\t_fix_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 隔离护栏：本文件全文没调任何写接口，但驱动 MangaPage._on_comic 时产品代码
# 内部会 store().add_favorite() —— 2026-10-14 的事故里这条记录就落进了用户真库
# （tools\_incident\）。写操作藏在产品代码里，静态判断不了，必须显式隔离。
# config._dev_sandbox() 也会兜底，这里写明是为了不再依赖兜底。
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_fix_"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication([])

    from mangakit.gui.main_window import MangaPage, FavoritesPage
    from mangakit.service import ComicService
    from mangakit.downloader import manager
    from mangakit.models import Comic, Chapter

    mgr = manager(); mgr.start()
    svc = ComicService(mgr)

    # --- MangaPage 状态标签 ---
    mp = MangaPage(svc)
    check("MangaPage 有 lb_status", hasattr(mp, "lb_status"))
    check("初始状态为空", mp.lb_status.text() == "")

    mp.load("https://example.invalid/x")
    check("load 后状态可见=解析中", mp.lb_status.text() == "解析中…",
          f"实际={mp.lb_status.text()!r}")

    # 直接喂一条 comic_loaded / chapters_loaded 看文案
    c = Comic(title="测试", url="https://example.invalid/x", site="s", site_name="站")
    mp._rid_c = 7
    mp._on_comic(7, c)
    check("收到漫画后状态更新", mp.lb_status.text() == "已找到漫画，正在读取章节…",
          f"实际={mp.lb_status.text()!r}")

    chs = [Chapter("第1话", "https://example.invalid/1", "s", "c1"),
           Chapter("第2话", "https://example.invalid/2", "s", "c2")]
    mp._rid_ch = 8
    mp._on_chapters(8, chs)
    check("收到章节后显示话数", mp.lb_status.text() == "已载入 2 话",
          f"实际={mp.lb_status.text()!r}")

    # 空章节要有专门文案
    mp._rid_ch = 9
    mp._on_chapters(9, [])
    check("空章节有明确提示", "没有解析到任何章节" in mp.lb_status.text(),
          f"实际={mp.lb_status.text()!r}")

    # 失败也要有回执（桩掉模态框）
    from PySide6.QtWidgets import QMessageBox
    orig = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    try:
        mp._rid_c = 10
        mp._on_failed(10, "网络超时")
        check("失败写状态栏", "解析失败" in mp.lb_status.text() and "网络超时" in mp.lb_status.text(),
              f"实际={mp.lb_status.text()!r}")
        check("失败后展开按钮置灰", mp.btn_expand.isEnabled() is False)
    finally:
        QMessageBox.warning = orig

    # --- check_updates 走 _guard ---
    import inspect
    src = inspect.getsource(ComicService.check_updates)
    check("check_updates 走 _guard", "_guard" in src, src.splitlines()[0].strip())

    # --- 全站搜索：无论成功还是炸了，search_done 都必须发出来 ---
    # 这是搜索按钮复位的唯一依据。没发出去 = 按钮永远灰着 = 「点了没反应」。
    import asyncio
    import mangakit.service as _svc_mod
    from mangakit.gui.bridge import bridge as _bridge_fn

    br = _bridge_fn()
    seen: list = []
    br.search_done.connect(lambda rid, n: seen.append(("done", rid, n)))

    # ① 正常路径（用桩注册表，不打真网）
    class _FakeSite:
        id = "fake"; name = "假站"; can_search = True

    class _FakeReg:
        def visible_sites(self): return [_FakeSite()]
        def adapter_for(self, s):
            class _A:
                async def search(self, kw): return []
            return _A()

    orig_reg = _svc_mod.registry
    try:
        _svc_mod.registry = lambda: _FakeReg()
        asyncio.run(svc._search_all_stream(101, "x"))
        check("正常搜索也发 search_done",
              ("done", 101, 0) in seen, f"seen={seen}")
    finally:
        _svc_mod.registry = orig_reg

    # ② 注册表直接炸掉（规则文件损坏这类）—— 仍然必须收尾
    seen.clear()
    try:
        _svc_mod.registry = lambda: (_ for _ in ()).throw(RuntimeError("规则坏了"))
        asyncio.run(svc._search_all_stream(102, "x"))
        check("注册表炸了也发 search_done（否则按钮永远灰着）",
              ("done", 102, 0) in seen, f"seen={seen}")
    finally:
        _svc_mod.registry = orig_reg

    # --- FavoritesPage 汇总能显示 message ---
    # 必须传 svc（真实签名）。以前这里写无参 FavoritesPage()，正好和
    # 「__init__ 收不到 svc、self.svc 不存在」的 bug 互相掩护。
    from mangakit.downloader import manager as _mgr_factory
    fp = FavoritesPage(ComicService(_mgr_factory()))
    fp._rid = 11
    fp._checking = True
    fp._check_total = 3
    fp.on_update_done(11, {"total": 3, "new": 0, "error": 1, "checked": 0,
                           "message": "RuntimeError: boom"})
    check("汇总含错误原因", "RuntimeError: boom" in fp.lb_count.text(),
          f"实际={fp.lb_count.text()!r}")
    check("完成后按钮复原", fp.btn_check.text() == "检查更新" and not fp._checking)

    # --- 检查中进度文案不被 reload() 盖掉 ---
    fp._checking = True
    fp._check_total = 3
    fp._updates.clear()
    fp.reload()
    check("检查中显示进度", "正在检查" in fp.lb_count.text(),
          f"实际={fp.lb_count.text()!r}")
    check("检查中按钮置灰", not fp.btn_check.isEnabled()
          and "检查中" in fp.btn_check.text())
    fp._checking = False
    fp.reload()

    mgr.stop()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 -> {FAILS}")
        return 1
    print("ALL FIX CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
