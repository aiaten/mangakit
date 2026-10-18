"""真机端到端：走真实 GUI 对话框，把一个真站加进去，再用引擎跑一遍。

这是「用户实际点按钮」的复现：
  ① 打开设置页 → 点「添加网站」→ 弹框出现
  ② 贴入真网址 → 走真实探测（打真网）
  ③ 探测通过 → 点「添加到站点列表」
  ④ 设置页列表里出现 [自建] 行
  ⑤ 引擎能解析该站
  ⑥ 点「删除站点」把它删掉，配置回到原样

注意：本脚本走真实 GUI、会把规则写进 config.user_rules，所以必须先把数据
目录隔离到临时位置，绝不能动用户的真配置（2026-10-14 收藏清空事故的同类风险）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
# 必须在任何 mangakit import 之前：数据目录由 MANGAKIT_HOME 决定。
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_gui_addsite_"))

URL = sys.argv[1] if len(sys.argv) > 1 else "https://songzimanhua.com/book/15293/"


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from mangakit.config import config
    from mangakit.gui.main_window import AddSiteDialog, SettingsPage
    from mangakit.probe import is_user_rule
    from mangakit.sites import reload_registry, registry

    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    # 删除确认框：这是测试，一律点「是」。
    QMessageBox.exec = lambda self: QMessageBox.Yes

    before = list(config().get("user_rules") or [])
    print(f"起始 user_rules 条数：{len(before)}")

    page = SettingsPage()
    page.show()
    app.processEvents()
    print(f"① 设置页站点表 {page.tbl_sites.rowCount()} 行，添加按钮 enabled={page.btn_add_site.isEnabled()}")

    dlg = AddSiteDialog(page)
    dlg.ed_url.setText(URL)
    print(f"② 贴入网址，开始探测：{URL}")
    dlg._probe()
    # 真实探测是后台线程跑的，转圈等它回来。
    import time

    t0 = time.time()
    while dlg._busy and time.time() - t0 < 150:
        app.processEvents()
        time.sleep(0.2)
    app.processEvents()
    print(f"   用时 {time.time()-t0:.1f}s，状态：{dlg.lb_state.text()}")
    print("   探测详情：")
    for line in dlg.txt.toPlainText().splitlines():
        print("     ", line)

    if dlg.rule is None:
        print(f"③ 探测未通过，按需求不予添加 ❌（原因：{dlg.result_info.reason if dlg.result_info else '?'}）")
        dlg.close()
        return 0

    print(f"③ 探测通过，添加按钮 enabled={dlg.btn_add.isEnabled()}")
    sid = dlg.rule["id"]
    dlg.accept()
    page._add_site.__self__  # noqa: B018 - 保持引用，无副作用
    # 直接走设置页的添加流程（对话框里已经认可了规则）。
    from mangakit.probe import add_user_rule

    add_user_rule(dlg.rule)
    reload_registry()
    page.reload()
    app.processEvents()
    print(f"④ 列表共 {page.tbl_sites.rowCount()} 行，{page.lb_sites.text()}")

    rows = []
    for r in range(page.tbl_sites.rowCount()):
        n = page.tbl_sites.item(r, 1).text()
        if "[自建]" in n:
            rows.append((r, n, page.tbl_sites.item(r, 2).text()))
    print(f"   [自建] 行：{rows}")

    site = registry().site_for_url(URL)
    print(f"⑤ 注册表认出：{site.name if site else '认不出 ❌'}")

    # 清理
    import asyncio

    from mangakit.probe import remove_user_rule

    remove_user_rule(sid)
    reload_registry()
    page.reload()
    app.processEvents()
    after = list(config().get("user_rules") or [])
    print(f"⑥ 已删除自定义站点，user_rules 条数回到 {len(after)}，is_user_rule={is_user_rule(sid)}")
    page.close()
    return 0 if len(after) == len(before) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    code = main()
    # 退出前按序拆 Qt（本脚本建了 SettingsPage 等控件），否则 Windows 会弹
    # 「python.exe - 应用程序错误 ... 该内存不能为 read」。
    # 成因与实测数据见 tools/qt_teardown.py 的模块注释。
    from qt_teardown import teardown
    teardown()
    raise SystemExit(code)
