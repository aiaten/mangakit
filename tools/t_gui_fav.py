# -*- coding: utf-8 -*-
"""离线 GUI 验证：收藏页「上次章节数」列 + 「最新章节」显示。

不开窗、不联网：造一个临时库塞两本收藏，构造 FavoriteUpdate 直接喂给
FavoritesPage，然后读回表格单元格文本/颜色/tooltip，确认：
  1. 列数从 7 变 8、表头有「上次章节数」；
  2. 没检查过 -> 「—」；
  3. 有基准的 -> 「N 话」，且**检查后仍然是旧值**（这是这一列的意义）；
  4. 倒序站的最新章节显示的是话号最大那一话，不是列表末尾；
  5. 涨/跌/没变 三种 tooltip。

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'; $env:QT_QPA_PLATFORM='offscreen'
    <py> tools\\t_gui_fav.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mangakit.models import FavoriteUpdate  # noqa: E402
import mangakit.store as st  # noqa: E402
from mangakit.store import Store  # noqa: E402

FAILED: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mk_gui_fav_")) / "mangakit.db"
    test_store = Store(tmp)
    st._store = test_store

    app = QApplication.instance() or QApplication([])

    A = "https://songzimanhua.com/book/15293/"
    B = "https://example.com/book/999/"
    # A：有历史基准（上次 1060 话），且最后一次检查时间已有
    test_store.add_favorite({"title": "海贼王", "url": A, "site": "songzimanhua",
                             "author": "尾田"})
    test_store.mark_favorite_checked("songzimanhua", A, 0, 1060, 1150)
    # B：从未检查过
    test_store.add_favorite({"title": "新书", "url": B, "site": "songzimanhua"})

    from mangakit.gui.main_window import FavoritesPage
    from mangakit.service import ComicService
    from mangakit.downloader import manager

    mgr = manager()          # 不 start()，只要一个占位对象
    page = FavoritesPage(ComicService(mgr))

    print("== 1. 表格结构 ==")
    check("列数", page.table.columnCount(), 8)
    heads = [page.table.horizontalHeaderItem(c).text() for c in range(8)]
    print(f"   表头 = {heads}")
    check("第4列表头", heads[4], "上次章节数")
    check("第3列表头", heads[3], "最新章节")
    check("第5列表头", heads[5], "状态")
    tip = page.table.horizontalHeaderItem(4).toolTip()
    check("表头有 tooltip", bool(tip), True)

    def cell(r, c):
        it = page.table.item(r, c)
        return it.text() if it is not None else None

    print("\n== 2. 没检查过时 ==")
    # 行序不保证，按标题找行
    rows = {}
    for i in range(page.table.rowCount()):
        rows[cell(i, 0)] = i
    print(f"   行 = {rows}")
    iB = rows["新书"]
    check("B 上次章节数", cell(iB, 4), "—")
    # 没检查过时状态列显示的是「这站支持不支持检查」，不是「未检查」
    check("B 状态", cell(iB, 5), "可检查")
    it = page.table.item(iB, 4)
    check("B 上次章节数 tooltip", "还没检查过" in it.toolTip(), True)

    print("\n== 3. 有历史基准 ==")
    iA = rows["海贼王"]
    check("A 上次章节数", cell(iA, 4), "1060 话")
    check("A 上次检查", cell(iA, 6) != "从未", True)

    print("\n== 4. 跑一轮检查后，该列仍是旧值（可对比）==")
    # 模拟本轮结果：倒序站，列表末尾是第1话，话号最大是第1162话
    up = FavoriteUpdate("songzimanhua", A, "海贼王",
                        latest_index=1067, latest_no=1162,
                        latest_title="第1162话", total=1068,
                        prev_total=1060, prev_no=1150,
                        new_count=8, status="new", by="count",
                        message="按章节数对比；章节数 1060 → 1068")
    # _rid 只有点过「检查更新」才存在；这里手工设成一致，模拟"正在检查中"
    page._rid = 7
    page.on_update_partial(7, up)
    check("检查后 最新章节", cell(iA, 3), "第1162话")
    check("检查后 状态", cell(iA, 5), "有 8 话更新")
    check("检查后 上次章节数仍是旧值", cell(iA, 4), "1060 话")
    prev_item = page.table.item(iA, 4)
    print(f"   上次章节数 tooltip = {prev_item.toolTip()!r}")
    check("tooltip 含上次值", "上次检查时源站共 1060 话" in prev_item.toolTip(), True)
    check("tooltip 含本次值", "本次 1068 话" in prev_item.toolTip(), True)
    check("tooltip 说明多了几话", "多 8 话" in prev_item.toolTip(), True)
    # Qt 会把 "#c60" 归一化成 "#cc6600"
    check("上次章节数标橙", prev_item.foreground().color().name(), "#cc6600")
    st_item = page.table.item(iA, 5)
    print(f"   状态 tooltip = {st_item.toolTip()!r}")
    check("状态 tooltip 含本次章节数", "本次章节数：1068" in st_item.toolTip(), True)

    print("\n== 5. 倒序站：最新章节不显示成第 1 话 ==")
    # latest_title 为空时也不能退回按位置算成「第 1068 话」
    up2 = FavoriteUpdate("songzimanhua", A, "海贼王", latest_index=1067,
                         latest_no=1162, latest_title="", total=1068,
                         prev_total=1068, prev_no=1162, new_count=0, status="ok",
                         by="count", message="按章节数对比；章节数 1068 → 1068")
    page.on_update_partial(7, up2)
    check("无标题时回退到话号", cell(iA, 3), "第 1162 话")
    check("没变时上次章节数", cell(iA, 4), "1068 话")
    it = page.table.item(iA, 4)
    check("没变时不标橙", it.foreground().color().name(), "#000000")
    check("没变 tooltip", "（没变）" in it.toolTip(), True)

    print("\n== 6. 章节数变少（站方删话）==")
    up3 = FavoriteUpdate("songzimanhua", A, "海贼王", latest_index=100,
                         latest_no=1000, latest_title="第1000话", total=1000,
                         prev_total=1068, prev_no=1162, new_count=0, status="ok",
                         by="count", message="按章节数对比；章节数 1068 → 1000")
    page.on_update_partial(7, up3)
    check("变少不报负", cell(iA, 5), "无更新")
    it = page.table.item(iA, 4)
    check("变少 tooltip", "少 68 话" in it.toolTip(), True)

    print("\n== 7. 检查完 reload() 后仍看得到「上次 → 本次」（关键）==")
    # 这是这一列的意义所在：跑完一轮检查后，库里 last_total 已经被刷成本次值，
    # 如果那一列直接读 last_total 就会等于本次、永远看不出对比。
    # 所以写库时要把旧值挪进 base_total。
    test_store.mark_favorite_checked("songzimanhua", A, 1067, 1068, 1162)
    row = test_store.one("SELECT * FROM favorites WHERE site=? AND url=?", ("songzimanhua", A))
    print(f"   库：base_total={row['base_total']} last_total={row['last_total']} "
          f"last_no={row['last_no']}")
    check("base_total 冻住旧值", row["base_total"], 1060)
    check("last_total 是本次值", row["last_total"], 1068)

    page.reload()
    rows2 = {}
    for i in range(page.table.rowCount()):
        rows2[cell(i, 0)] = i
    iA2 = rows2["海贼王"]
    check("reload 后 上次章节数", cell(iA2, 4), "1060 话")
    t = page.table.item(iA2, 4).toolTip()
    print(f"   reload 后 tooltip = {t!r}")
    check("reload 后 tooltip 有本次值", "本次 1068 话" in t, True)
    check("reload 后 tooltip 有多 8 话", "多 8 话" in t, True)
    check("reload 后标橙", page.table.item(iA2, 4).foreground().color().name(),
          "#cc6600")
    # 章节数判更新已由离线套件覆盖；这里只确认界面显示

    print("\n== 8. 连查两轮：base_total 跟着前进，不是停在第一次 ==")
    test_store.mark_favorite_checked("songzimanhua", A, 1080, 1085, 1175)
    row = test_store.one("SELECT * FROM favorites WHERE site=? AND url=?", ("songzimanhua", A))
    print(f"   第二轮后：base_total={row['base_total']} last_total={row['last_total']}")
    check("base_total 前进到上一轮", row["base_total"], 1068)
    check("last_total 是最新", row["last_total"], 1085)
    page.reload()
    check("第二轮显示", cell(rows2["海贼王"], 4), "1068 话")

    print("\n== 9. 重设基线（从现在开始）不该留旧对比 ==")
    test_store.set_favorite_baseline("songzimanhua", A, 1080, 1085, 1175)
    row = test_store.one("SELECT * FROM favorites WHERE site=? AND url=?", ("songzimanhua", A))
    check("baseline 时 base_total=当前值", row["base_total"], 1085)
    page.reload()
    check("baseline 后无对比（显示当前值）", cell(rows2["海贼王"], 4), "1085 话")

    print("\n" + "=" * 46)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
