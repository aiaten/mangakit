# -*- coding: utf-8 -*-
"""拷貝漫畫（copy4000 系）适配器离线回归：AES + 三段解密 + 搜索 + 限流识别。

为什么单独立一个套件
--------------------
这个站的**全部正文**（章节列表、章节图片）都是 AES 密文，而且密钥（ccz/cct）
是每次从详情页/章节页里抠出来的；解密只要错一个字节，症状就是「章节列表
为空」或「图片全丢」，而这两种症状跟「站点限流」「站点改版」长得一模一样。
所以这里把每一环都钉死：

  1. 纯 Python AES 实现对不对（用 node crypto 生成的固定向量对拍）
  2. 「16 字符 iv + hex 密文」这种非标准拼接格式能不能解
  3. 详情页能不能抠出 ccz / dnt / 标题 / 作者 / 状态 / 标签
  4. 章节列表解密后能不能正确切出条目（用真实抓下来的密文）
  5. 章节图片解密后顺序是否原样
  6. 站内搜索走 JSON API，字段映射对不对
  7. **限流识别**：站点被限流时不报错、安静返回空章节，我们要抛 ParseError
     而不是返回空列表（空列表在界面上等于「这本漫画没有章节」，会误导用户）

跑法：
    $env:PYTHONPATH='D:\\AI\\工作区1\\mangakit'
    <py> tools\\t_copy4000.py
全部通过时末行打印 === 拷貝漫畫适配器回归全部通过 ===，退出码 0。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据隔离：本套件全用固定 HTML/密文，不打真网，但仍显式隔离（见 check_isolation.py）。
os.environ.setdefault("MANGAKIT_HOME", tempfile.mkdtemp(prefix="mk_c4k_"))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ 夹具
# AES 向量由 node crypto 生成（key='0123456789abcdef', iv='fedcba9876543210'），
# 覆盖空串 / 单字符 / UTF-8 多字节 / 恰好一块 / 跨块 / JSON 六种边界。
AES_KEY = "0123456789abcdef"
AES_IV = "fedcba9876543210"
AES_VECTORS: list[tuple[str, str]] = [
    ("", "3c492cedd954cc383f3d3ee93e728484"),
    ("a", "30aef88f86574fe3493174858fa4e2d5"),
    ("中文测试", "5c31c15a0d2c62652056db8eb23240c9"),
    ("x" * 16, "433ce36074acbc2cb9a6ee7474fdfe9b78303a1b39fab2af0c13606f936c1bf1"),
    ("x" * 17, "433ce36074acbc2cb9a6ee7474fdfe9b256caeb7f4b00d4902b7d99cb0114d50"),
    ('{"a":1}', "9d5688b6f68336e76bd62aaf309ecfbb"),
]

# 真实抓下来的章节列表密文（火影忍者特典 huoyingrenzhetedian，7 条）。
CHAP_BLOB = (
    "s0ZrcCLjHWxbBIvf7c30b76b5e2bd4e0186df6005b92f7ef4d1df7605c84bb6cebd679b22c71265377da3d"
    "20fbbb7f72966b186873f8bbed512799ceed16881a72b57f8fb326185721222ce70c29980b2c070e04c0ae"
    "3564d7dad4b841c051f16b4721f1c2e2614d9537b8dd9cc87d0d0be3ec1c2a66548395b87e24790462cf96"
    "5d158ac60fb78d2c73f7c2b06581888b07b0c6fe52fdd94dd18da1902907be59f92a91da7bd34381ac2c80"
    "b08a876a58699a6e9684b658e21a81b3726f0ed397e9c874144efb83bdcdfb39075bacf78e590af1768e35"
    "f062f45663dbcdc10d7fc6536b0ee561a523affec66e6cef13fd287a237b199f490728b07d888ce7a4a324"
    "85d7793ec37f3aeeedc33b8f073467995cffd2c5e599c15e63efefb596ce7e67a326fa0bf3837897418a6d"
    "392fd2459ecd90c40e7c421f0535ca76c963e363f9a6cffb6f24c78d7097967adfea21f57dfd3d7cfaf8eb"
    "e7a676c77b5cfef2f6fda62aa263996119c23256a3fa86c5440e9f663d0d42fca308ec7eae721f4dbeb4fc"
    "bc03a2e1fec47756543beda616f706ec6e8eb4e26fd0622e598bdd94148332ee4f73936c70ae5fc3abe116"
    "df747f2668385fe2d852a6ac842495ad2f04e8c50660228e361af5ea9c14f12fe65acd32099d2fa36db81f"
    "0ecf5489ee10e4da2409f74002b4b7db7b480b2fd74da982280491103d905cf2ce2dfbc3aaa7fbe3edf0c4"
    "cec12d0e50ab651fce8d29270ffb9d0e9ae02152985be349aebc28dc27f620b76883c2424a65149c1967ea"
    "4ef41ac4dc3a02a32ea09d102c381271f01f190545e805790569e9365e5a366d878d357d102002598b6d2d"
    "4725f28dbfd4c23f86dfe498be1617c366cfabb6b5f371a0e75f1734a0c5e426c7a5f838be4e5558fc4bfa"
    "e108d9f7be40e9e6d94316eed9d368576a530b5f98697d19a66ce094084e0b45ec4119d272a1fd2ae2e969"
    "a292349ad960d92137173b24f42bec668e7307b12440d987c3c0ecdcad2feca847ed023a53a745896093c5"
    "ea4b017059b41db0303338f06d3adf65ca38cf70c775ffb13a483915ff356f9713a93eb3fa4a48a062b785"
    "8d26800b69fb5a21ea6c51bb87a7bc8507ae423c012b839fdc33bffd8e21233515f0c0a28816f2d08dd7b9"
    "61b4a02e9faefe0a4e204c548dd9a35e0febc1a16eb506b08b864d3819649f8ebabba96e4af9da44b57741"
    "05a4ef5108f74415a9595b6d26826543f8e89825246caf7b687ecb2a8de9c5482cf7cc6d65c56145b3ec4b"
    "2a9ff276d79201ed087dd84b5081e54e201f4241ab2b000da06027f4c534cdcc41bf1211e26538907cc0b5"
    "4ad31174fbbaa653cde0022ca5cc7793c3ed687dbb9b86b933f4cc6700e8385c94f3b857feb68eb3064013"
    "174b9863f225ffbc2ebfb1458ea926500de754cd940686a5ab6fadd72d07e3fb286aa89bf167d33e79ae8d"
    "4c83a1633d52a0e5cfdedffb21f7291e6b26d3813c401919cc42b6b31f894573bceeff73af60e85feb8857"
    "f5437fa5d69da47ab83b1af683e4c2ed0c687b31dc37a93f041e86e60b12b6c2e6b708dcc38d8b9b982867"
    "612df725cd94abaddb45ae54b10b6f683caef53fad93292d7c63738b42d2e033e8d45eb7ee2f919c2a4fa4"
    "ee793177970454282eddf5340e17b1108634605e3055624cc0f28c0184530f86f68f837e03f1ef2e75237b"
    "ffb5bb14857f8a28c26afa8c3dee9f1f1b27e6595a7eb8287dc276cba797fcb14e76529c37f78a8bca5c93"
    "503acc96f4fd53cc9f73befc77c9912da63d5875dea52b704a24179d634edb344f9de2eb1be2978c21c4ae"
    "9681325ef2b13431e37da279451a848fb9a34631e6468ce89aa4c54fe610"
)

# 解出空章节列表的密文（模拟站点限流时的安静返回）。
EMPTY_BLOB = (
    "Zq8vN3rT5yU1wX2ac0d295467168643e053785f6e305b649c19c3fb449e07e2aeb6d44be6c066a3406b409"
    "7c64d06e97e0e6132b4e9ae6b7376b02fc28bb2e0991bc9c86f5bfa81028368c6e39961b1d70fdead8e0ea"
    "75799c45354c5457b9fd739ce2fd68cf2cc844ec96c951b139eb37336123bf5d4f6ec2746c70e1b41ee549"
    "20ed5dbe06406dc7a3056b6e0e4bd67357937ed476add599833fc956c340a20e742efda8373040"
)

# 章节图片密文：明文是 3 张图的 JSON 数组，用站点密钥 + iv '3f1t9qj8IwhP6RpZ' 加密。
PAGES_BLOB = (
    "3f1t9qj8IwhP6RpZ9ae1c5170e68c5d53a529441b4f8e8bc1712512722427c472f3388a2b10054afecfc58"
    "d480ed914b68c63b26dd2c73634f95c128a6c4222a4a079c4113c27c22487c3462a5c5f64547323d24219e"
    "c7711988e872e30700b2cf7747d6ed16829904b1d1ca8839d315e4acd53f345cc0b5a132472de36e6f33f4"
    "0040213fc287e18b4053650ef3a3deb75fc4aa5eba2c1f7d8ca0ea7f1c715405ea12b363c9c21fda19f029"
    "2d20ba72e25f19ede3eb270066ecf74a442098b32a3cd043e5356ee08e7bd2cbb283f3ae012d91889a8e68"
    "d0"
)

# 真实站点密钥（页面内联 var ccz = '...' / var cct = '...'，两个变量同值）。
SITE_KEY = "0p0zzpvv.nmn.oop"

DETAIL_HTML = """<!DOCTYPE html><html lang="zh-hant"><head>
<title>海贼王-海贼王漫畫-第1195话-連載中-冒險漫画,热血漫画-在线阅读 - 拷貝漫畫</title>
</head><body>
<div class="comicParticulars-left-img loadingIcon">
  <img class="lazyload" data-src="https://sh.mangafunb.fun/h/haizeiwang/cover/1651422982.jpg.328x422.jpg">
</div>
<div class="col-9 comicParticulars-title-right"><ul>
  <li><h6 title="海贼王">海贼王</h6></li>
  <li><span>別名：</span><p class="comicParticulars-right-txt">海盗王,OP,航海王</p></li>
  <li><span>作者：</span><span class="comicParticulars-right-txt">
      <a href="/author/weitianrongyilang/comics" target="_blank">尾田栄一郎</a></span></li>
  <li><span>熱度：</span><p class="comicParticulars-right-txt">
      <span class="flameIcon allIcon"></span>1307.0W</p></li>
  <li><span class="comicParticulars-sigezi">最後更新：</span>
      <span class="comicParticulars-right-txt">2026-10-15</span></li>
  <li><span>狀態：</span><span class="comicParticulars-right-txt">連載中</span></li>
  <li><span>題材：</span><span class="comicParticulars-left-theme-all comicParticulars-tag">
      <a href="/comics?theme=maoxian" target="_blank">#冒險</a>
      <a href="/comics?theme=rexue" target="_blank">#热血</a></span></li>
  <li><a href="/comic/haizeiwang/chapter/4bd05882-c7bc-11e8-881a-024352452ce0"
         class="btn comicParticulars-botton">開始閱讀</a></li>
</ul></div>
<div class="container comicParticulars-synopsis">
  <div class="theBoxModel-comic-detail-content"></div>
  <span id="dnt" style="display:none;" value="9"></span>
  <p class="intro" style="flex: 1;margin-right:40px;">拥有财富、名声、权力，这世界上的一切的男人。</p>
</div>
<script>var ccz = '0p0zzpvv.nmn.oop';</script>
</body></html>
"""

CHAPTER_HTML = """<!DOCTYPE html><html><head><title>海贼王 第1卷</title></head><body>
<div class="imageData"></div>
<script>
  var cct = '0p0zzpvv.nmn.oop';
  var contentKey = '%s';
</script></body></html>""" % PAGES_BLOB

# contentKey 为空（匿名 / 限流）的章节页。
CHAPTER_HTML_EMPTY = CHAPTER_HTML.replace(PAGES_BLOB, "")

SEARCH_JSON = {
    "code": 200, "message": "请求成功",
    "results": {
        "total": 2, "limit": 30, "offset": 0,
        "list": [
            {"name": "海贼王", "path_word": "haizeiwang",
             "cover": "https://sh.mangafunb.fun/h/haizeiwang/cover/1.jpg.328x422.jpg",
             "author": [{"name": "尾田栄一郎", "path_word": "weitianrongyilang"}],
             "alias": "海盗王,OP", "popular": 13020076},
            {"name": "海賊王 艾斯", "path_word": "haizeiwangaisi",
             "cover": "https://sh.mangafunb.fun/h/haizeiwangaisi/cover/2.jpg.328x422.jpg",
             "author": [{"name": "Boichi", "path_word": "boichi"}],
             "alias": "", "popular": 100},
        ],
    },
}


class FakeResp:
    """够适配器用的假响应：只调 .json() / .raise_for_status()。"""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class FakeHttp:
    """桩掉 get()（章节列表）与 get_json()（搜索），并记下请求过的 URL。"""

    def __init__(self, chap_payload: Any = None, search_payload: Any = None) -> None:
        self.chap_payload = chap_payload
        self.search_payload = search_payload if search_payload is not None else SEARCH_JSON
        self.calls: list[tuple[str, str]] = []

    async def get(self, url: str, **kw: Any) -> FakeResp:
        self.calls.append(("get", url))
        return FakeResp(self.chap_payload)

    async def get_json(self, url: str, **kw: Any) -> Any:
        self.calls.append(("get_json", url))
        return self.search_payload

    async def download(self, url: str, **kw: Any) -> tuple[bytes, str]:
        self.calls.append(("download", url))
        return b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg"


def _mk(detail_html: str = DETAIL_HTML,
        chapter_html: str = CHAPTER_HTML,
        chap_payload: Any = None,
        search_payload: Any = None):
    """造一个 fetch / http 都被桩掉的 Copy4000Adapter。"""
    from mangakit import adapters
    from mangakit.sites import registry

    reg = registry()
    site = reg.sites.get("copy4000")
    if site is None:
        return None, None
    http = FakeHttp(chap_payload, search_payload)
    ad = adapters.Copy4000Adapter(site, http)      # type: ignore[arg-type]

    async def fake_fetch(url: str, **kw: Any) -> str:
        return chapter_html if "/chapter/" in url else detail_html

    ad.fetch = fake_fetch                            # 实例属性遮蔽基类方法
    adapters._COPY_KEYS.clear()                      # 密钥缓存别跨用例串
    return ad, http


# ------------------------------------------------------------------ 用例
def test_aes() -> None:
    print("== 1. 纯 Python AES-128-CBC 与 node crypto 对拍 ==")
    from mangakit.aes import cbc_decrypt

    key = AES_KEY.encode()
    iv = AES_IV.encode()
    for plain, hexct in AES_VECTORS:
        got = cbc_decrypt(key, iv, bytes.fromhex(hexct)).decode("utf-8")
        check(f"解密 {plain[:12]!r}", got == plain, f"got={got!r}")

    print("== 2. 非法入参必须报错，不能静默解出垃圾 ==")
    for name, fn in (
        ("key 不是 16 字节",
         lambda: cbc_decrypt(b"short", iv, bytes.fromhex(AES_VECTORS[0][1]))),
        ("iv 不是 16 字节",
         lambda: cbc_decrypt(key, b"short", bytes.fromhex(AES_VECTORS[0][1]))),
        ("密文不是 16 的倍数",
         lambda: cbc_decrypt(key, iv, b"123456789012345")),
    ):
        try:
            fn()
        except ValueError:
            check(name + " 抛 ValueError", True)
        except Exception as exc:                     # noqa: BLE001
            check(name + " 抛 ValueError", False, f"got {type(exc).__name__}: {exc}")
        else:
            check(name + " 抛 ValueError", False, "没有抛异常")


def test_blob_format() -> None:
    print("== 3. 「16 字符 iv + hex 密文」拼接格式 ==")
    from mangakit.aes import decrypt_page_blob

    items = json.loads(decrypt_page_blob(PAGES_BLOB, SITE_KEY))
    check("解出 3 条图片", isinstance(items, list) and len(items) == 3,
          f"got={str(items)[:120]}")
    check("每条是 {url: ...}",
          all(isinstance(i, dict) and "url" in i for i in items),
          f"got={str(items[:1])[:120]}")
    check("图片顺序原样保留",
          items[0]["url"].endswith("1001.jpg.c1500x.jpg")
          and items[2]["url"].endswith("1003.jpg.c1500x.jpg"),
          f"got={[i['url'][-20:] for i in items]}")

    for name, blob in (("空串", ""), ("太短", "abc"),
                       ("hex 坏字符", "3f1t9qj8IwhP6RpZ" + "zz" * 16)):
        try:
            decrypt_page_blob(blob, SITE_KEY)
        except ValueError:
            check(name + " 抛 ValueError", True)
        except Exception as exc:                     # noqa: BLE001
            check(name + " 抛 ValueError", False, f"got {type(exc).__name__}: {exc}")
        else:
            check(name + " 抛 ValueError", False, "没有抛异常")


def test_registration() -> None:
    print("== 4. 注册：适配器与站点都在，且能按域名匹配 ==")
    from mangakit.sites import registry

    reg = registry()
    check("copy4000 适配器已注册", "copy4000" in reg._adapters,
          f"got={sorted(reg._adapters)}")
    site = reg.sites.get("copy4000")
    check("copy4000 站点存在", site is not None)
    if site is None:
        return
    check("站名是 拷貝漫畫", site.name == "拷貝漫畫", f"got={site.name!r}")
    check("默认启用", site.enabled is True)
    check("算「支持站内搜索」（搜索页下拉里不该标不支持）", site.can_search is True)
    check("域名含 copy4000.com / copy5000.com",
          "copy4000.com" in site.domains and "copy5000.com" in site.domains,
          f"got={site.domains}")
    for u in ("https://www.copy4000.com/comic/haizeiwang",
              "https://copy4000.com/comic/x",
              "https://www.copy5000.com/comic/x"):
        s = reg.site_for_url(u)
        check(f"site_for_url({u}) -> copy4000", s is not None and s.id == "copy4000",
              f"got={s.id if s else None}")
    ad = reg.adapter_for(site)
    check("adapter_for 返回 Copy4000Adapter",
          type(ad).__name__ == "Copy4000Adapter", f"got={type(ad).__name__}")


def test_parse_comic() -> None:
    print("== 5. parse_comic：详情页字段 ==")
    ad, _ = _mk()
    if ad is None:
        check("copy4000 站点存在", False, "站点缺失，跳过")
        return
    c = asyncio.run(ad.parse_comic("https://www.copy4000.com/comic/haizeiwang"))
    check("标题取 h6 的 title 属性（不是被 urljoin 拼坏的地址）",
          c.title == "海贼王", f"got={c.title!r}")
    check("作者取 /author/ 链接文本", c.author == "尾田栄一郎", f"got={c.author!r}")
    check("封面是绝对地址",
          c.cover.startswith("https://sh.mangafunb.fun/"), f"got={c.cover!r}")
    check("状态 連載中 -> ongoing", c.status == "ongoing", f"got={c.status!r}")
    check("标签去掉 # 前缀", c.tags == ["冒險", "热血"], f"got={c.tags}")
    check("简介取 p.intro", c.description.startswith("拥有财富"),
          f"got={c.description[:20]!r}")
    check("cid 是 path_word", c.cid == "haizeiwang", f"got={c.cid!r}")
    check("site / site_name 已填",
          c.site == "copy4000" and c.site_name == "拷貝漫畫",
          f"got={c.site}/{c.site_name}")

    print("== 6. 已完结的站内文案也要认 ==")
    for text, want in (("已完結", "finished"), ("連載中", "ongoing"), ("", "unknown")):
        check(f"狀態 {text or '(空)'} -> {want}",
              ad._status(text) == want, f"got={ad._status(text)!r}")


def test_parse_chapters() -> None:
    print("== 7. parse_chapters：真实密文解出 7 话，标题/链接/顺序都对 ==")
    ad, http = _mk(chap_payload={"code": 200, "results": CHAP_BLOB})
    if ad is None:
        check("copy4000 站点存在", False, "站点缺失，跳过")
        return
    chs = asyncio.run(ad.parse_chapters(
        "https://www.copy4000.com/comic/huoyingrenzhetedian"))
    check("解出 7 话", len(chs) == 7, f"got={len(chs)}")
    if not chs:
        return
    check("标题取 name 字段", chs[0].title == "皆之书", f"got={chs[0].title!r}")
    check("链接是 /comic/<pw>/chapter/<uuid>",
          chs[0].url.startswith(
              "https://www.copy4000.com/comic/huoyingrenzhetedian/chapter/"),
          f"got={chs[0].url!r}")
    check("cid 是章节 uuid 且出现在链接里",
          bool(chs[0].cid) and chs[0].cid in chs[0].url, f"got={chs[0].cid!r}")
    check("index 从 0 连续", [c.index for c in chs] == list(range(7)),
          f"got={[c.index for c in chs]}")
    check("7 条 uuid 互不重复", len({c.cid for c in chs}) == 7)

    print("== 8. 章节接口的请求姿势与密钥缓存 ==")
    check("请求了 /comicdetail/<pw>/chapters",
          any("/comicdetail/huoyingrenzhetedian/chapters" in u for _, u in http.calls),
          f"got={[u for _, u in http.calls]}")
    from mangakit import adapters
    check("密钥已缓存（同一 host 不重复抓详情页）",
          "www.copy4000.com" in adapters._COPY_KEYS,
          f"got={sorted(adapters._COPY_KEYS)}")
    n_before = len(http.calls)
    asyncio.run(ad.parse_chapters("https://www.copy4000.com/comic/huoyingrenzhetedian"))
    check("第二次解析命中缓存：只多一次章节接口请求",
          len(http.calls) - n_before == 1, f"got={len(http.calls) - n_before} 次")

    print("== 9. 限流识别：安静返回空章节 + 详情页有開始閱讀 -> 抛 ParseError ==")
    ad2, _ = _mk(chap_payload={"code": 200, "results": EMPTY_BLOB})
    try:
        asyncio.run(ad2.parse_chapters("https://www.copy4000.com/comic/haizeiwang"))
    except Exception as exc:                         # noqa: BLE001
        msg = str(exc)
        check("空章节 + 有阅读链接 -> ParseError 且提示限流",
              type(exc).__name__ == "ParseError" and "限流" in msg, f"got={msg!r}")
    else:
        check("空章节 + 有阅读链接 -> ParseError 且提示限流", False, "没有抛异常")

    print("== 10. 接口返回非 200 时给中文原因 ==")
    ad3, _ = _mk(chap_payload={"code": 210, "message": "請到官網更新最新APP"})
    try:
        asyncio.run(ad3.parse_chapters("https://www.copy4000.com/comic/haizeiwang"))
    except Exception as exc:                         # noqa: BLE001
        msg = str(exc)
        check("210 -> ParseError 且带上站点原话",
              type(exc).__name__ == "ParseError" and "210" in msg
              and "請到官網更新最新APP" in msg, f"got={msg!r}")
    else:
        check("210 -> ParseError 且带上站点原话", False, "没有抛异常")

    print("== 11. 接口返回空密文也要报错 ==")
    ad4, _ = _mk(chap_payload={"code": 200, "results": ""})
    try:
        asyncio.run(ad4.parse_chapters("https://www.copy4000.com/comic/haizeiwang"))
    except Exception as exc:                         # noqa: BLE001
        check("空密文 -> ParseError", type(exc).__name__ == "ParseError",
              f"got {type(exc).__name__}: {exc}")
    else:
        check("空密文 -> ParseError", False, "没有抛异常")


def test_parse_pages() -> None:
    print("== 12. parse_pages：解出 3 张图，绝对地址、顺序不变 ==")
    ad, _ = _mk()
    if ad is None:
        check("copy4000 站点存在", False, "站点缺失，跳过")
        return
    urls = asyncio.run(ad.parse_pages(
        "https://www.copy4000.com/comic/demo/chapter/4bd05882-c7bc-11e8-881a-024352452ce0"))
    check("解出 3 张", len(urls) == 3, f"got={len(urls)}")
    check("都是绝对地址", all(u.startswith("https://") for u in urls), f"got={urls}")
    check("顺序是 1001 -> 1002 -> 1003（不能重排）",
          [next((n for n in ("1001", "1002", "1003") if n in u), "?")
           for u in urls] == ["1001", "1002", "1003"],
          f"got={[u.rsplit('/', 1)[-1] for u in urls]}")

    print("== 13. contentKey 为空（匿名/限流）时要说人话 ==")
    ad2, _ = _mk(chapter_html=CHAPTER_HTML_EMPTY)
    try:
        asyncio.run(ad2.parse_pages("https://www.copy4000.com/comic/demo/chapter/x"))
    except Exception as exc:                         # noqa: BLE001
        check("空 contentKey -> ParseError", type(exc).__name__ == "ParseError",
              f"got {type(exc).__name__}: {exc}")
    else:
        check("空 contentKey -> ParseError", False, "没有抛异常")

    print("== 14. 章节页没有 cct 时报错而不是崩 ==")
    ad3, _ = _mk(chapter_html="<html><body>没有脚本</body></html>")
    try:
        asyncio.run(ad3.parse_pages("https://www.copy4000.com/comic/demo/chapter/x"))
    except Exception as exc:                         # noqa: BLE001
        check("缺 cct -> ParseError", type(exc).__name__ == "ParseError",
              f"got {type(exc).__name__}: {exc}")
    else:
        check("缺 cct -> ParseError", False, "没有抛异常")


def test_search() -> None:
    print("== 15. 站内搜索：JSON 字段映射 ==")
    ad, http = _mk()
    if ad is None:
        check("copy4000 站点存在", False, "站点缺失，跳过")
        return
    res = asyncio.run(ad.search("海贼王"))
    check("返回 2 条", len(res) == 2, f"got={len(res)}")
    if res:
        check("标题取 name", res[0].title == "海贼王", f"got={res[0].title!r}")
        check("链接由 path_word 拼出",
              res[0].url == "https://www.copy4000.com/comic/haizeiwang",
              f"got={res[0].url!r}")
        check("作者数组拼成字符串", res[0].author == "尾田栄一郎",
              f"got={res[0].author!r}")
        check("封面透传", res[0].cover.startswith("https://sh.mangafunb.fun/"),
              f"got={res[0].cover!r}")
        check("site / site_name 已填", res[0].site == "copy4000",
              f"got={res[0].site!r}")
        check("cid 是 path_word", res[0].cid == "haizeiwang", f"got={res[0].cid!r}")
    check("搜索走 searchcl 接口",
          any("/api/kb/web/searchcl/comics" in u for _, u in http.calls),
          f"got={[u for _, u in http.calls]}")
    check("total=2 < limit=30，只请求一次", len(http.calls) == 1,
          f"got={len(http.calls)}")

    print("== 16. 空关键词不发请求 ==")
    ad2, http2 = _mk()
    out = asyncio.run(ad2.search("   "))
    check("空关键词返回空列表", out == [], f"got={out}")
    check("空关键词一次请求都没发", http2.calls == [], f"got={http2.calls}")

    print("== 17. 空结果页也不能炸 ==")
    ad3, _ = _mk(search_payload={"code": 200, "results": {"total": 0, "list": []}})
    check("空结果返回 []", asyncio.run(ad3.search("不存在")) == [])


def main() -> int:
    print(f"仓库根：{ROOT}")
    test_aes()
    test_blob_format()
    test_registration()
    test_parse_comic()
    test_parse_chapters()
    test_parse_pages()
    test_search()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{FAILED}")
        print("=== 拷貝漫畫适配器回归有失败项 ===")
        return 1
    print("=== 拷貝漫畫适配器回归全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
