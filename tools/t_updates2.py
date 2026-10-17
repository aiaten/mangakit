# -*- coding: utf-8 -*-
"""m11417 回归测试：收藏检查更新「按话号最大」+「上次检查章节数」。

覆盖四件事：
  1. 话号解析（models.Chapter.no）：认明确写法，不把「2026新年贺图」当第 2026 话。
  2. latest_chapter()：倒序站取话号最大者；话号认不出来时退回按位置。
  3. service._check_one() 的三种对比口径：首次 / 章节数涨 / 话号涨（总数不变）。
  4. store 迁移 + mark_favorite_checked 写三列，以及老库（只有 last_index）的兼容。

用假适配器喂章节，不联网。跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'
    <py> tools\\t_updates2.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from mangakit.models import Chapter, FavoriteUpdate, latest_chapter  # noqa: E402
import mangakit.store as store_mod  # noqa: E402
from mangakit.store import Store  # noqa: E402
from mangakit.service import ComicService, _prev_baseline  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def mk(*titles: str) -> list[Chapter]:
    return [Chapter(cid=f"c{i}", title=t, url=f"http://x/{i}", index=i)
            for i, t in enumerate(titles)]


# ---------------------------------------------------------------- 1. 话号解析
print("== 1. 话号解析（Chapter.no）==")
PARSE = [
    ("第12话", 12), ("第 12 話", 12), ("Chapter 12", 12), ("chapter12", 12),
    ("12话", 12), ("第12章", 12), ("第3卷", 3), ("第12.5话", 12),
    ("2026新年贺图", -1), ("序章", -1), ("番外篇", -1), ("后记", -1),
    ("", -1), ("第0话", 0),
]
for title, want in PARSE:
    check(f"no({title!r})", Chapter(cid="x", title=title, url="").no, want)


# ------------------------------------------------------- 2. latest_chapter()
print("\n== 2. 选「最新章节」（倒序站不再是第 1 话）==")
# 倒序站：列表第一条 = 最新话
desc = mk("第50话", "第49话", "第48话")
check("倒序站 latest_chapter", latest_chapter(desc).title, "第50话")
check("倒序站按位置会取错", max(desc, key=lambda c: c.index).title, "第48话")
# 正序站
asc = mk("第1话", "第2话", "第3话")
check("正序站 latest_chapter", latest_chapter(asc).title, "第3话")
# 话号认不出来 → 退回按位置
odd = mk("序章", "番外篇", "后记")
check("全认不出→退回按位置", latest_chapter(odd).title, "后记")
# 少数带话号（不过半）→ 也退回按位置，避免拿残缺话号比
mixed = mk("序章", "第2话", "番外篇", "后记")
check("不过半→退回按位置", latest_chapter(mixed).title, "后记")
# 过半带话号 → 用话号
mostly = mk("第1话", "第2话", "番外篇")
check("过半→用话号", latest_chapter(mostly).title, "第2话")
# 跳号：最大话号不是最后一项
gap = mk("第10话", "第99话", "第11话")
check("跳号取99", latest_chapter(gap).title, "第99话")


# ------------------------------------------------ 3. _check_one 对比口径
print("\n== 3. _check_one 三种对比口径 ==")


class FakeSite:
    def __init__(self, sid):
        self.id = sid
        self.enabled = True


class FakeReg:
    """假注册表：adapter_for 返回喂固定章节的适配器。"""

    def __init__(self, chs):
        self.chs = chs
        self.sites = {"fake": FakeSite("fake")}

    def adapter_for(self, site):
        chs = self.chs

        class A:
            async def parse_chapters(self, url):
                return chs

        return A()


URL = "http://example.com/book"


def run_check(chs, prev_total, prev_no) -> FavoriteUpdate:
    reg = FakeReg(chs)
    return asyncio.run(ComicService._check_one(
        reg, "fake", URL, "书", prev_total, prev_no))


desc5 = mk("第5话", "第4话", "第3话", "第2话", "第1话")   # 倒序，最新=第5话
up = run_check(desc5, -1, -1)
check("首次 status", up.status, "first")
check("首次 latest_title", up.latest_title, "第5话")
check("首次 latest_no", up.latest_no, 5)
check("首次 total", up.total, 5)
check("首次 new_count", up.new_count, 0)

# 上次 5 话 → 这次 6 话（倒序插入一条新话在最前）
# ★ 口径：以最大话号为主。话号 5→6 涨了 1，by 记 "no"，不是 "count"。
desc6 = mk("第6话", "第5话", "第4话", "第3话", "第2话", "第1话")
up = run_check(desc6, 5, 5)
check("章节数涨 status", up.status, "new")
check("章节数涨 new_count", up.new_count, 1)
check("章节数涨 by（话号为准）", up.by, "no")
check("章节数涨 latest_title", up.latest_title, "第6话")
check("章节数涨 message 提最大话号", "最大话号" in up.message, True)

# ★ 核心场景：源站跳话/缺话。总数只多 1，但话号从 50 跳到 60，实际多 10 话。
# 旧口径（按章节总数比）会显示"更新 1 话"，读者以为只有 1 话可看。
jumpy_prev = mk(*[f"第{i}话" for i in range(50, 0, -1)])          # 第50..第1话
jumpy_now = mk(*([f"第{i}话" for i in range(60, 51, -1)]
                 + [f"第{i}话" for i in range(50, 0, -1)]))        # 第60..第52 + 第50..第1
check("跳话站 总数", len(jumpy_now), 59)
check("跳话站 最大话号", latest_chapter(jumpy_now).no, 60)
up = run_check(jumpy_now, 50, 50)
check("跳话站 status", up.status, "new")
check("跳话站 new_count（话号差 10，不是总数差 9）", up.new_count, 10)
check("跳话站 by", up.by, "no")
check("跳话站 latest_no", up.latest_no, 60)

# 总数不变但话号涨（删了旧话）：5 话 → 5 话，最新从第5话变第6话
shifted = mk("第6话", "第4话", "第3话", "第2话", "第1话")
up = run_check(shifted, 5, 5)
check("话号涨 status", up.status, "new")
check("话号涨 new_count", up.new_count, 1)
check("话号涨 by", up.by, "no")
check("话号涨 message", "最大话号" in up.message, True)

# 没更新
up = run_check(desc5, 5, 5)
check("无更新 status", up.status, "ok")
check("无更新 new_count", up.new_count, 0)
check("无更新 message", "最新话号 5 → 5" in up.message, True)

# 老库只有位置基线（prev_total 来自 last_index+1），话号 -1
# 话号那侧没基准 → 只能退回按章节数比，by 记 "count"
up = run_check(desc6, 5, -1)
check("老库口径 status", up.status, "new")
check("老库口径 new_count", up.new_count, 1)
check("老库口径 by（退回 count）", up.by, "count")

# ★ 源站新增一话，但标题抠不出话号（只写「番外」）：
# 话号没涨（仍是第 5 话），章节数 5→6。这是话号口径的天然盲区，
# 必须靠章节数差补上，否则用户永远看不到这一话更新。
up = run_check(mk("番外", "第5话", "第4话", "第3话", "第2话", "第1话"), 5, 5)
check("新话无话号 status", up.status, "new")
check("新话无话号 new_count", up.new_count, 1)
check("新话无话号 by（按章节数）", up.by, "count")

# ★ 全站标题都抠不出话号（prev_no/latest_no 都是 -1）→ 纯按章节数比
up = run_check(mk("后记", "番外", "正文"), 2, -1)
check("全无话号 status", up.status, "new")
check("全无话号 new_count", up.new_count, 1)
check("全无话号 by", up.by, "count")

# 站方删话导致总数变少 → 不报负数
up = run_check(mk("第5话", "第4话", "第3话"), 5, 5)
check("变少不报负", up.new_count, 0)
check("变少 status", up.status, "ok")


# ------------------------------------------------- 4. store 迁移与写入
print("\n== 4. store 迁移 + 三列写入 ==")
tmp = Path(tempfile.mkdtemp(prefix="mktest_"))
db = tmp / "mangakit.db"

# 先手工建一个「老库」：favorites 只有 last_check / last_index
con = sqlite3.connect(db)
con.executescript("""
CREATE TABLE favorites (
    site TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
    author TEXT DEFAULT '', cover TEXT DEFAULT '', tags TEXT DEFAULT '[]',
    last_check REAL DEFAULT 0, last_index INTEGER DEFAULT -1,
    PRIMARY KEY (site, url));
""")
con.execute("INSERT INTO favorites(site,url,title,last_check,last_index) VALUES(?,?,?,?,?)",
            ("fake", URL, "书", 0.0, 4))
con.commit()
con.close()

st = Store(db)
cols = {r["name"] for r in st.query("PRAGMA table_info(favorites)")}
check("老库补出 last_total", "last_total" in cols, True)
check("老库补出 last_no", "last_no" in cols, True)
check("老库补出 base_no", "base_no" in cols, True)
check("老库补出 base_total", "base_total" in cols, True)
# 迁移可重复执行（每次开 Store 都会跑一遍）
Store(db)
check("迁移可重复执行", True, True)

row = st.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake", URL))
check("老库 last_total 默认 -1", row["last_total"], -1)
check("老库 last_no 默认 -1", row["last_no"], -1)
check("老库 base_no 默认 -1", row["base_no"], -1)
check("老库 _prev_baseline 推算章节数", _prev_baseline(row), (5, -1))

# ★ 首次写入话号：base_no 应被冻成"写之前的 last_no"，即 -1（老库从没记过话号）。
# 界面这时显示「—」是对的：还没有基准可对比，下一次检查开始才有。
st.mark_favorite_checked("fake", URL, 0, 6, 6)
row = st.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake", URL))
check("写后 last_index", row["last_index"], 0)
check("写后 last_total", row["last_total"], 6)
check("写后 last_no", row["last_no"], 6)
check("写后 base_no 冻住旧值(-1)", row["base_no"], -1)
check("写后 _prev_baseline", _prev_baseline(row), (6, 6))

# ★ 第二次写入：base_no 必须冻成上一次的 last_no(6)，否则界面上「上次章节数」
# 会永远等于本次值，那一列等于白加。
st.mark_favorite_checked("fake", URL, 0, 8, 8)
row = st.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake", URL))
check("第二次写后 last_no", row["last_no"], 8)
check("第二次写后 base_no 冻住 6", row["base_no"], 6)
check("第二次写后 base_total 冻住 6", row["base_total"], 6)

# 失败检查：只动时间，不清基线
st.mark_favorite_checked("fake", URL, None)
row = st.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake", URL))
check("失败不清 last_total", row["last_total"], 8)
check("失败不清 last_no", row["last_no"], 8)
check("失败不清 base_no", row["base_no"], 6)

# set_favorite_baseline 也要写三列 + base_no（点「从现在开始」重设基线时，
# 那一次没有"上一次"，base_no 应当就是当前话号，界面显示「N 话」而不是旧值）
st.set_favorite_baseline("fake", URL, 0, 9, 9)
row = st.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake", URL))
check("baseline 写 total", row["last_total"], 9)
check("baseline 写 no", row["last_no"], 9)
check("baseline 写 base_no=当前", row["base_no"], 9)

# 从未检查过的行
# 注意签名是 add_favorite(f, url="", site="", title="")，第三个位置参数是 site 不是 title，
# 别照直觉写 add_favorite("fake2", url, "书2") —— 那会把 site 写成 "书2"、url 空着。
st.add_favorite("书2", "http://example.com/b2", "fake2", tags=[])
row = st.one("SELECT * FROM favorites WHERE site=? AND url=?", ("fake2", "http://example.com/b2"))
check("新收藏行存在", row is not None, True)
check("新收藏 _prev_baseline", _prev_baseline(row), (-1, -1))
check("新收藏 base_no 默认 -1", row["base_no"], -1)
check("新收藏 last_no 默认 -1", row["last_no"], -1)


print("\n" + ("=" * 46))
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
