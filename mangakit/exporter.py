"""导出器：把章节目录打包成 CBZ / ZIP / PDF / EPUB / 长条图。

依赖都是可选的：img2pdf 缺失时 PDF 走 Pillow 兜底。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import zipfile
from pathlib import Path

from .config import config
from .images import HAVE_PIL, sniff_ext
from .logsetup import get

log = get("export")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")


def page_files(folder: Path) -> list[Path]:
    """按自然序号排序的页面文件。"""
    if not folder.is_dir():
        return []
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS]

    def key(p: Path) -> tuple:
        m = re.match(r"(\d+)", p.stem)
        return (int(m.group(1)) if m else 10**9, p.stem)

    return sorted(files, key=key)


def sibling(folder: Path, ext: str) -> Path:
    """章节目录同级的输出路径：`第12.5话` → `第12.5话.cbz`。

    ★ 不能用 `folder.with_suffix(ext)`：with_suffix 会**替换**最后一段后缀，
    而章节名里带点很常见（`第12.5话`、`Ch.12.5`、`第3卷.下`）。
    `Path("第12.5话").with_suffix(".cbz")` 得到的是 `第12.cbz` ——
    于是「第12.5话」和「第12.6话」会打包到**同一个文件**上互相覆盖，
    用户看到的是「下载了两话，只剩一话」。改成追加而不是替换。
    """
    return folder.parent / f"{folder.name}{ext}"


def _zip_like(folder: Path, ext: str, compress: int) -> Path:
    files = page_files(folder)
    if not files:
        raise ValueError(f"{folder.name} 里没有图片")
    target = sibling(folder, ext)
    with zipfile.ZipFile(target, "w", compress) as z:
        for i, f in enumerate(files, 1):
            # CBZ 内部按顺序命名，避免本地文件改名后顺序错乱
            z.writestr(f"{i:05d}{f.suffix.lower()}", f.read_bytes())
    return target


def pack_cbz(folder: Path, mode: str = "cbz") -> Path:
    """按模式打包一个章节目录，返回生成的文件路径。"""
    mode = (mode or "cbz").lower()
    if mode in ("cbz", "zip"):
        return _zip_like(folder, ".cbz" if mode == "cbz" else ".zip",
                         zipfile.ZIP_STORED if mode == "cbz" else zipfile.ZIP_DEFLATED)
    if mode == "pdf":
        return to_pdf(folder)
    if mode == "epub":
        return to_epub(folder)
    raise ValueError(f"未知打包模式：{mode}")


def to_cbz(folder: Path) -> Path:
    return _zip_like(folder, ".cbz", zipfile.ZIP_STORED)


def to_pdf(folder: Path, out: Path | None = None) -> Path:
    """页面按序合成 PDF（自动横竖适配）。"""
    files = page_files(folder)
    if not files:
        raise ValueError(f"{folder.name} 里没有图片")
    out = out or sibling(folder, ".pdf")
    try:
        import img2pdf

        out.write_bytes(img2pdf.convert([str(f) for f in files]))
        return out
    except Exception as e:
        log.debug("img2pdf 不可用（%s），改用 Pillow", e)
    if not HAVE_PIL:
        raise RuntimeError("需要 img2pdf 或 Pillow 才能导出 PDF")
    from PIL import Image

    imgs = []
    for f in files:
        try:
            im = Image.open(f)
            im.load()
            if im.mode in ("RGBA", "P", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            imgs.append(im)
        except Exception as e:
            log.warning("跳过坏图 %s: %s", f.name, e)
    if not imgs:
        raise ValueError("没有可用图片")
    imgs[0].save(out, save_all=True, append_images=imgs[1:], resolution=150.0)
    for im in imgs:
        im.close()
    return out


MEDIA_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif", ".tif": "image/tiff", ".tiff": "image/tiff",
}

_XHTML_HEAD = ('<?xml version="1.0" encoding="utf-8"?>'
               '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
               '<meta charset="utf-8"/><title>{title}</title>'
               '<link rel="stylesheet" type="text/css" href="../style.css"/></head>')
_CSS = ("@page{margin:0}body{margin:0;padding:0}"
        ".page{margin:0;padding:0;text-align:center;page-break-after:always}"
        "img{max-width:100%;max-height:100%;height:auto}")


def _epub_id(folder: Path) -> str:
    """稳定的书号：同一目录每次导出得到同一个 id。

    不能用 `hash(str(folder))`：Python 的字符串 hash 每个进程都加了随机盐
    （PYTHONHASHSEED），同一个章节每次导出会得到不同的 dc:identifier，
    有些阅读器/书架据此判定为「新书」而重复收录。
    """
    import hashlib
    return hashlib.sha1(str(folder).encode("utf-8")).hexdigest()[:16]


def to_epub(folder: Path, title: str | None = None) -> Path:
    """单章节 EPUB（多章合并用 merge_epub）。"""
    files = page_files(folder)
    if not files:
        raise ValueError(f"{folder.name} 里没有图片")
    title = title or folder.name
    out = sibling(folder, ".epub")

    # 每个页面的真实扩展名决定 zip 内文件名与 media-type。以前固定写 .jpg：
    # PNG 的字节被命名成 .jpg 且声明 image/jpeg，严格的阅读器（Calibre 校验、
    # Apple Books）会报「文件损坏」或整章不显示。
    exts = [f.suffix.lower() for f in files]
    exts = [e if e in MEDIA_BY_EXT else ".jpg" for e in exts]

    img_items = "".join(
        f'<item id="i{n}" href="images/{n:05d}{e}" media-type="{MEDIA_BY_EXT[e]}"/>'
        for n, e in enumerate(exts, 1)
    )
    page_items = "".join(
        f'<item id="p{n}" href="pages/{n:05d}.xhtml" media-type="application/xhtml+xml"/>'
        for n in range(1, len(files) + 1)
    )
    spine = "".join(f'<itemref idref="p{n}"/>' for n in range(1, len(files) + 1))
    navpoints = "".join(
        f'<navPoint id="n{n}" playOrder="{n}"><navLabel><text>第{n}页</text></navLabel>'
        f'<content src="pages/{n:05d}.xhtml"/></navPoint>'
        for n in range(1, len(files) + 1)
    )
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:mangakit:{_epub_id(folder)}</dc:identifier>
<dc:title>{_esc(title)}</dc:title><dc:language>zh-CN</dc:language>
<meta property="rendition:layout">pre-paginated</meta>
<meta property="rendition:orientation">portrait</meta>
<meta property="rendition:spread">none</meta>
</metadata>
<manifest>{img_items}{page_items}<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="css" href="style.css" media-type="text/css"/></manifest>
<spine toc="ncx">{spine}</spine></package>'''
    ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="urn:mangakit:{_epub_id(folder)}"/></head>
<docTitle><text>{_esc(title)}</text></docTitle>
<navMap>{navpoints}</navMap>
</ncx>'''
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", _CSS)
        for n, (f, e) in enumerate(zip(files, exts), 1):
            z.writestr(f"OEBPS/images/{n:05d}{e}", f.read_bytes())
            # ★ 一页一个 xhtml，只放本页那张图。
            # 旧代码把「全部页面」的 img 拼进了第 1 页的 body（body 变量被复用到
            # 每一页），于是第 1 页显示全书所有图片、第 2..N 页又各显示一张 ——
            # 阅读时表现为「第 1 页后面所有图重复了一遍」。
            z.writestr(
                f"OEBPS/pages/{n:05d}.xhtml",
                _XHTML_HEAD.format(title=_esc(f"第{n}页"))
                + f'<body><div class="page"><img src="../images/{n:05d}{e}" alt="第{n}页"/></div></body></html>',
            )
    return out


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def join_long_strip(folder: Path, out: Path | None = None) -> Path:
    """把多页竖向拼成一张长图（可选）。"""
    files = page_files(folder)
    if not files:
        raise ValueError(f"{folder.name} 里没有图片")
    if not HAVE_PIL:
        raise RuntimeError("需要 Pillow")
    from PIL import Image
    imgs = []
    for f in files:
        im = Image.open(f)
        im.load()
        imgs.append(im.convert("RGB") if im.mode != "RGB" else im)
    w = max(i.width for i in imgs)
    h = sum(i.height for i in imgs)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        y += im.height
        im.close()
    out = out or folder.with_name(folder.name + "_long.jpg")
    canvas.save(out, quality=90)
    canvas.close()
    return out


def merge_epub(epubs: list[Path], out: Path, title: str = "合集") -> Path:
    """把多个单章 EPUB 合成一本（按传入顺序排列章节）。

    修掉的几个问题：
    - 原来只 glob `*.jpg`，PNG/WebP 页被**静默丢掉**（合并出来的书缺页）；
    - 原来用系统临时目录解包，本机 temp 不可写时直接 PermissionError；
      改成在 out 旁边建临时目录，导出完删掉；
    - 只有一个输入时原来用 `replace()` —— 那会把用户原来那本**移走**，
      等于合并一次就毁掉源文件；改成复制。
    """
    if not epubs:
        raise ValueError("没有可合并的 EPUB")
    import shutil

    out.parent.mkdir(parents=True, exist_ok=True)
    if len(epubs) == 1:
        shutil.copyfile(epubs[0], out)
        return out

    work = Path(tempfile.mkdtemp(prefix=".mk_epub_", dir=str(out.parent)))
    try:
        # 按「章 → 页」收集，保持传入顺序，不依赖 zip 内名字排序
        pages: list[tuple[bytes, str]] = []   # (图片字节, 扩展名)
        for i, e in enumerate(epubs):
            d = work / f"c{i}"
            with zipfile.ZipFile(e) as z:
                z.extractall(d)
            src = d / "OEBPS" / "images"
            if not src.is_dir():
                continue
            for p in sorted(src.iterdir(), key=lambda q: (len(q.stem), q.stem)):
                if not p.is_file():
                    continue
                data = p.read_bytes()
                ext = p.suffix.lower()
                if ext not in MEDIA_BY_EXT:
                    ext = sniff_ext(data) or ".jpg"
                pages.append((data, ext))
        if not pages:
            raise ValueError("这些 EPUB 里没有图片")

        img_items = "".join(
            f'<item id="i{n}" href="images/{n:05d}{e}" media-type="{MEDIA_BY_EXT[e]}"/>'
            for n, (_, e) in enumerate(pages, 1)
        )
        page_items = "".join(
            f'<item id="p{n}" href="pages/{n:05d}.xhtml" media-type="application/xhtml+xml"/>'
            for n in range(1, len(pages) + 1)
        )
        spine = "".join(f'<itemref idref="p{n}"/>' for n in range(1, len(pages) + 1))
        navpoints = "".join(
            f'<navPoint id="n{n}" playOrder="{n}"><navLabel><text>第{n}页</text></navLabel>'
            f'<content src="pages/{n:05d}.xhtml"/></navPoint>'
            for n in range(1, len(pages) + 1)
        )
        uid = hashlib.sha1(str(out).encode("utf-8")).hexdigest()[:16]
        opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:mangakit:{uid}</dc:identifier>
<dc:title>{_esc(title)}</dc:title><dc:language>zh-CN</dc:language>
<meta property="rendition:layout">pre-paginated</meta>
<meta property="rendition:orientation">portrait</meta>
<meta property="rendition:spread">none</meta>
</metadata>
<manifest>{img_items}{page_items}<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/><item id="css" href="style.css" media-type="text/css"/></manifest>
<spine toc="ncx">{spine}</spine></package>'''
        ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="urn:mangakit:{uid}"/></head>
<docTitle><text>{_esc(title)}</text></docTitle>
<navMap>{navpoints}</navMap>
</ncx>'''
        tmp_out = out.with_name(out.name + ".tmp")
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml",
                       '<?xml version="1.0"?><container version="1.0" '
                       'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                       'media-type="application/oebps-package+xml"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf", opf)
            z.writestr("OEBPS/toc.ncx", ncx)
            z.writestr("OEBPS/style.css", _CSS)
            for n, (data, e) in enumerate(pages, 1):
                z.writestr(f"OEBPS/images/{n:05d}{e}", data)
                z.writestr(
                    f"OEBPS/pages/{n:05d}.xhtml",
                    _XHTML_HEAD.format(title=_esc(f"第{n}页"))
                    + f'<body><div class="page"><img src="../images/{n:05d}{e}" alt="第{n}页"/></div></body></html>',
                )
        os.replace(tmp_out, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out
