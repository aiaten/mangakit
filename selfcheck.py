"""冒烟测试：导入所有模块 + 关键纯函数。"""
import asyncio, sys, tempfile, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_home_"))

import mangakit
from mangakit import logsetup, models, images, sites, adapters, exporter
from mangakit.config import config
from mangakit.store import store
from mangakit.http_client import normalize_url as nu
from mangakit.downloader import DownloadManager
from mangakit.sites import Selector, registry
from lxml import html as LH

logsetup.setup("WARNING")
print("version:", mangakit.__version__)

# --- 配置
c = config()
c.set("download_dir", r"D:\漫画测试")
assert c.get("download_dir").endswith("漫画测试")
c.update({"concurrency": 8})
assert c.get("concurrency") == 8
print("config ok")

# --- store
s = store()
s.kv_set("t", {"a": 1})
assert s.kv_get("t") == {"a": 1}
s.upsert_task(models.Task(tid="t1", comic="C", chapter="第1话", folder="f", status="queued"))
assert len(s.all_tasks()) == 1
s.add_favorite({"site": "dm5w.com", "url": "http://x.com/1", "title": "测试漫画", "tags": ["热血"]})
assert s.favorite_exists("dm5w.com", "http://x.com/1")
assert len(s.favorites()) == 1
s.kv_set("x", 1); s.kv_set("x", 2)
assert s.kv_get("x") == 2
print("store ok")

# --- images
im = images
assert im.sniff_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 20) == ".png"
assert im.sniff_ext(b"\xff\xd8\xff\xe0" + b"0" * 20) == ".jpg"
assert im.sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
assert im.safe_filename('a<b>c:"d/e\\f|g?h*i') == "a_b_c__d_e_f_g_h_i"
assert im.guess_name(0, 120) == "0001"
assert im.page_filename(11, 50, ".jpg") == "0012.jpg"
assert im.is_image(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
assert not im.is_image(b"hello world this is not an image at all")
if im.HAVE_PIL:
    from PIL import Image
    buf = __import__("io").BytesIO()
    Image.new("RGB", (100, 4000), (200, 30, 30)).save(buf, format="JPEG")
    data = buf.getvalue()
    assert im.dimension(data) == (100, 4000)
    assert im.verify(data)
    png, ext = im.finalize(data, convert_webp=True)
    assert ext == ".jpg"
    pieces = im.split_long(data, 1600)
    assert len(pieces) >= 2, pieces
    print("images ok (HAVE_PIL), split ->", len(pieces), "pieces")
else:
    print("images ok (no PIL)")

# --- Selector
doc = LH.fromstring("""
<div class="list">
  <a class="item" href="/c/1"><img data-src="//img.cdn/1.jpg"><span>漫画一</span></a>
  <a class="item" href="/c/2"><img data-src="//img.cdn/2.jpg"><span>漫画二</span></a>
</div>
""")
hrefs = Selector({"css": "a.item", "attr": "href", "all": True}).find_all(doc, "https://s.com/book/")
assert hrefs == ["https://s.com/c/1", "https://s.com/c/2"], hrefs
srcs = Selector({"css": "a.item img", "attr": "data-src", "all": True}).find_all(doc, "https://s.com/")
assert srcs == ["https://img.cdn/1.jpg", "https://img.cdn/2.jpg"], srcs
titles = Selector({"css": "a.item span", "text": True, "all": True}).find_all(doc, "https://s.com")
assert titles == ["漫画一", "漫画二"], titles
# xpath
assert Selector({"xpath": "//a[@class='item']/@href", "all": True}).find_all(doc, "https://s.com/") == ["/c/1", "/c/2"]
# regex
html = LH.fromstring('<p>x</p><img src="/p1.png"><img src="/p2.png">')
assert Selector({"regex": r'<img[^>]+src="([^"]+)"', "all": True}).find_all(html) == ["/p1.png", "/p2.png"]
# json path
data = {"data": {"images": ["a.jpg", "b.jpg"], "sub": {"x": [{"u": "1"}, {"u": "2"}]}}}
assert sites._json_path(data, "data.images") == ["a.jpg", "b.jpg"]
assert sites._json_path(data, "$.data.sub.x[*].u") == ["1", "2"]
assert sites._json_path(data, "data.sub.x[1].u") == ["2"]
print("selector ok")

# --- registry
reg = registry()
print("sites:", [s.id for s in reg.all_sites()])
assert reg.site_for_url("https://m.dm5w.com/manga/123/").id == "mstation"
assert reg.site_for_url("https://www.kuaikanmanzhen.com/comic/123").id == "kuaikan"
assert reg.site_for_url("https://www.kzdm.com/manhuaziyuan/").id == "kzdm"
assert reg.site_for_url("https://example.com/") is None
site, ad = reg.for_url("https://m.dm5w.com/manga/123/")
assert isinstance(ad, adapters.MStationAdapter)
print("registry ok")

# --- exporter
tmp = Path(tempfile.mkdtemp(prefix="mk_exp_"))
if im.HAVE_PIL:
    from PIL import Image
    for i in range(1, 4):
        Image.new("RGB", (60, 90), (i * 40, 10, 10)).save(tmp / f"{i:04d}.jpg")
cbz = exporter.to_cbz(tmp)
assert cbz.exists() and cbz.stat().st_size > 0
import zipfile
with zipfile.ZipFile(cbz) as z:
    assert z.namelist() == ["00001.jpg", "00002.jpg", "00003.jpg"], z.namelist()
pdf = exporter.to_pdf(tmp)
assert pdf.exists() and pdf.stat().st_size > 0
epub = exporter.to_epub(tmp, "测试")
assert epub.exists()
with zipfile.ZipFile(epub) as z:
    assert z.namelist()[0] == "mimetype"
    assert "OEBPS/content.opf" in z.namelist()
long_img = exporter.join_long_strip(tmp)
assert long_img.exists()
print("exporter ok:", cbz.name, pdf.name, epub.name, long_img.name)

# --- downloader wiring
m = DownloadManager()
m.start()
t = models.Task(tid="t9", comic="C", chapter="第1话", folder="f", url="https://m.dm5w.com/manga/1/1.html",
                site="mstation", chapter_id="1")
assert abs(t.progress - 0.0) < 1e-6
assert t.to_dict()["status"] == "pending"
assert set(("tid", "comic", "chapter", "url", "site", "chapter_id")).issubset(t.to_dict())
s.upsert_task(t)
m.pause("t9"); m.resume("t9"); m.cancel("t9")
print("downloader ok")

# --- normalize_url
assert nu("https://a.com/x/y?q=1", "../z") == "https://a.com/z"
assert nu("https://a.com/x/y", "p.jpg#frag") == "https://a.com/x/p.jpg"
assert nu("https://a.com", "//cdn.i/b.png") == "https://cdn.i/b.png"
print("normalize ok")

print("\nALL SMOKE TESTS PASSED")