"""图片处理：格式识别、webp→png、长图切割、拼接。

Pillow 是可选依赖，缺失时相关功能降级为“原样保存”。
"""

from __future__ import annotations

import io
import os
from typing import Iterable

from .logsetup import get

log = get("image")

try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    Image.MAX_IMAGE_PIXELS = None
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False
    Image = None  # type: ignore

EXT_BY_FORMAT = {
    "JPEG": ".jpg", "JPEG2000": ".jp2", "PNG": ".png", "GIF": ".gif",
    "BMP": ".bmp", "TIFF": ".tiff", "WEBP": ".webp", "ICO": ".ico",
    "AVIF": ".avif", "MPO": ".jpg",
}

MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tiff"), (b"MM\x00*", ".tiff"),
    (b"II\x2a\x00", ".tiff"), (b"MM\x00\x2a", ".tiff"),
)


def sniff_ext(data: bytes) -> str:
    for magic, ext in MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"\x00\x00\x00\x1c" or data[:4] == b"\x00\x00\x00\x20":
        return ".avif"
    if HAVE_PIL:
        try:
            with Image.open(io.BytesIO(data)) as im:
                return EXT_BY_FORMAT.get(im.format or "", "")
        except Exception:
            pass
    return ""


def is_image(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    if sniff_ext(data):
        return True
    # SVG 是文本
    head = data[:512].lstrip()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower())


def dimension(data: bytes) -> tuple[int, int]:
    """读尺寸，失败返回 (0, 0)。"""
    if not HAVE_PIL:
        return (0, 0)
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return (0, 0)


def convert_webp_to_png(data: bytes) -> bytes:
    if not HAVE_PIL:
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGBA")
            else:
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=False, compress_level=6)
            return buf.getvalue()
    except Exception as e:
        log.debug("webp→png 失败: %s", e)
        return data


def finalize(data: bytes, *, convert_webp: bool = True, force_ext: str = "") -> tuple[bytes, str]:
    """返回 (处理后的数据, 扩展名)。"""
    ext = force_ext or sniff_ext(data)
    if ext == ".webp" and convert_webp:
        data = convert_webp_to_png(data)
        ext = ".png"
    if not ext:
        ext = ".jpg"
    return data, ext


def split_long(data: bytes, min_height: int, overlap: int = 0) -> list[bytes]:
    """把超长图按 min_height 切段（漫画站的「一话一张长图」）。"""
    if not HAVE_PIL or min_height <= 0:
        return [data]
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if h <= min_height:
                return [data]
            out: list[bytes] = []
            step = max(1, min_height - overlap)
            y = 0
            im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
            while y < h:
                box = (0, y, w, min(y + min_height, h))
                piece = im.crop(box)
                buf = io.BytesIO()
                ext = "PNG" if im.mode == "RGBA" else "JPEG"
                if ext == "JPEG" and piece.mode == "RGBA":
                    piece = piece.convert("RGB")
                piece.save(buf, format=ext, quality=92)
                out.append(buf.getvalue())
                if y + min_height >= h:
                    break
                y += step
            return out or [data]
    except Exception as e:
        log.debug("切图失败: %s", e)
        return [data]


def make_thumb(data: bytes, width: int = 320) -> bytes:
    if not HAVE_PIL:
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.thumbnail((width, width * 3))
            if im.mode not in ("RGB", "RGBX"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
            return buf.getvalue()
    except Exception:
        return data


def verify(data: bytes) -> bool:
    if not HAVE_PIL:
        return is_image(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:
        return False


def guess_name(index: int, total: int) -> str:
    """0001.jpg 形式；总页数 >9999 时加一位。"""
    w = max(4, len(str(max(total, 1))))
    return f"{index + 1:0{w}d}"


def safe_filename(name: str, maxlen: int = 120) -> str:
    """清理 Windows 非法文件名字符。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if ch in bad else ch for ch in name)
    out = out.strip(" .")
    out = out.replace("\n", " ").replace("\r", " ")
    while "  " in out:
        out = out.replace("  ", " ")
    if len(out) > maxlen:
        out = out[:maxlen].rstrip(" .")
    return out or "untitled"


def page_filename(index: int, total: int, ext: str) -> str:
    return f"{guess_name(index, total)}{ext}"