"""导出器：把章节目录打包成 CBZ / ZIP / PDF / EPUB / 长条图。

依赖都是可选的：img2pdf 缺失时 PDF 走 Pillow 兜底。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

from .config import config
from .images import HAVE_PIL
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


def _zip_like(folder: Path, ext: str, compress: int) -> Path:
    files = page_files(folder)
    if not files:
        raise ValueError(f"{folder.name} 里没有图片")
    target = folder.with_suffix(ext)
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
    out = out or folder.with_suffix(".pdf")
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


def to_epub(folder: Path, title: str | None = None) -> Path:
    """单章节 EPUB（多章合并用 merge_epub）。"""
    files = page_files(folder)
    if not files:
        raise ValueError(f"{folder.name} 里没有图片")
    title = title or folder.name
    out = folder.with_suffix(".epub")
    items = "\n".join(
        f'<item id="p{i}" href="images/{i:05d}.jpg" media-type="image/jpeg"/>'
        for i in range(1, len(files) + 1)
    )
    manifest = "".join(
        f'<item id="p{i}" href="pages/{i:05d}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, len(files) + 1)
    )
    spine = "".join(f'<itemref idref="p{i}"/>' for i in range(1, len(files) + 1))
    body = "".join(
        f'<div class="page"><img src="images/{i:05d}.jpg" alt="第{i}页"/></div>'
        for i in range(1, len(files) + 1)
    )
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:mangakit:{abs(hash(str(folder)))}</dc:identifier>
<dc:title>{_esc(title)}</dc:title><dc:language>zh-CN</dc:language>
<meta property="rendition:layout">pre-paginated</meta>
</metadata>
<manifest>{manifest}<item id="css" href="style.css" media-type="text/css"/></manifest>
<spine toc="ncx">{spine}</spine></package>'''
    ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="uid"/></head>
<docTitle><text>{_esc(title)}</text></docTitle>
<navMap>{''.join(f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>第{i}页</text></navLabel><content src="pages/{i:05d}.xhtml"/></navPoint>' for i in range(1, len(files) + 1))}</navMap>
</ncx>'''
    css = "body{margin:0} .page{margin:0;padding:0;text-align:center} img{max-width:100%;height:auto}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", css)
        for i, f in enumerate(files, 1):
            z.writestr(f"OEBPS/images/{i:05d}.jpg", f.read_bytes())
            z.writestr(
                f"OEBPS/pages/{i:05d}.xhtml",
                f'<?xml version="1.0" encoding="utf-8"?>'
                f'<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" '
                f'type="text/css" href="../style.css"/></head><body>{body}</body></html>'
                if i == 1 else
                f'<?xml version="1.0" encoding="utf-8"?>'
                f'<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" '
                f'type="text/css" href="../style.css"/></head><body>'
                f'<div class="page"><img src="../images/{i:05d}.jpg" alt="第{i}页"/></div></body></html>',
            )
    return out


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


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


def merge_epub(epubs: list[Path], out: Path) -> Path:
    """把多个单章 EPUB 合成一本。"""
    if len(epubs) == 1:
        epubs[0].replace(out)
        return out
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="mangakit_epub_"))
    for i, e in enumerate(epubs):
        with zipfile.ZipFile(e) as z:
            z.extractall(work / f"c{i}")
    images: list[str] = []
    spine_items: list[str] = []
    for i, _ in enumerate(epubs):
        d = work / f"c{i}" / "OEBPS"
        if d.is_dir():
            for p in sorted((d / "images").glob("*.jpg")):
                images.append(p.read_bytes())
            for p in sorted((d / "pages").glob("*.xhtml")):
                spine_items.append(f'<item id="s{i}_{p.stem}" href="pages/{i}_{p.stem}.xhtml" media-type="application/xhtml+xml"/>')
    total = len(images)
    spine_items = [
        f'<item id="s{i}_{i}" href="pages/{i}_{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(total)
    ]
    manifest = "".join(spine_items) + '<item id="css" href="style.css" media-type="text/css"/>'
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:mangakit:merged</dc:identifier>
<dc:title>合集</dc:title><dc:language>zh-CN</dc:language></metadata>
<manifest>{manifest}</manifest><spine>{''.join(f'<itemref idref="s{i}_{i}"/>' for i in range(total))}</spine></package>'''
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/style.css", "body{margin:0} img{max-width:100%}")
        for i, data in enumerate(images):
            z.writestr(f"OEBPS/images/{i:05d}.jpg", data)
            z.writestr(f"OEBPS/pages/{i}_{i}.xhtml",
                       f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f'<head><link rel="stylesheet" type="text/css" href="../style.css"/></head>'
                       f'<body><div class="page"><img src="../images/{i:05d}.jpg"/></div></body></html>')
    shutil.rmtree(work, ignore_errors=True)
    return out