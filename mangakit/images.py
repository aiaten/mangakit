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


# Windows 默认 MAX_PATH = 260（按字符算）。这里留 20 字余量，因为同一目录下
# 还会多出「.part 子目录 + 页码文件名 + .tmp 后缀」这几层。不处理的话，长标题
# 漫画在别人机器上会以 winerror=3 / FileNotFoundError 的形式失败，而本机路径
# 短的话根本复现不了——属于只在分发后才暴露的那类问题。
MAX_PATH_BUDGET = 240

# Windows 保留设备名。把它们当目录/文件名时，mkdir 看起来会成功，但只要
# 往里写文件就 FileNotFoundError（实测 winerror=206/3）。漫画标题里出现
# "NUL"、"CON"、"COM1" 这类短词是可能的，必须加前缀躲开。
_RESERVED_DEVICE = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
)


def safe_filename(name: str, maxlen: int = 120) -> str:
    """清理 Windows 非法文件名字符、保留设备名、首尾空格与点。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if ch in bad else ch for ch in name)
    out = out.strip(" .")
    out = out.replace("\n", " ").replace("\r", " ")
    while "  " in out:
        out = out.replace("  ", " ")
    if len(out) > maxlen:
        out = out[:maxlen].rstrip(" .")
    out = out or "untitled"
    # 判断要按「第一段」来：CON.txt 也会被当成设备名（Windows 的规则是
    # 只看主名），所以取第一个 '.' 之前的部分。
    if out.split(".")[0].upper() in _RESERVED_DEVICE:
        out = "_" + out
    return out


def safe_path(base: Path, *names: str, tail: int = 40) -> Path:
    """在 base 下拼若干层目录，超长时逐层截短，保证不撞 MAX_PATH。

    tail = 最后一层目录之下还要占掉的字符数（.part 子目录、页码文件名、
    .tmp 后缀等）。名字等长分配额度：短名浪费的空额会被长名用不上，
    但结果一定不超预算，这比「刚好塞满」重要。
    """
    cleaned = [safe_filename(n) for n in names]
    if cleaned:
        room = MAX_PATH_BUDGET - len(str(base)) - tail - len(cleaned)
        alloc = max(8, room // len(cleaned))
        cleaned = [
            (c[:alloc].rstrip(" .") or "untitled") if len(c) > alloc else c
            for c in cleaned
        ]
    p = base
    for c in cleaned:
        p = p / c
    return p


def page_filename(index: int, total: int, ext: str) -> str:
    return f"{guess_name(index, total)}{ext}"