# -*- coding: utf-8 -*-
"""导出器回归测试：CBZ / ZIP / PDF / EPUB / 长条图 / 多章合并 EPUB。

覆盖的都是「只有真的打开文件才看得见」的问题：

  1. 输出文件名不能被 `Path.with_suffix` 吃掉章节号
     （`第12.5话` 曾被打包成 `第12.cbz`，第 12.5 / 12.6 话互相覆盖）。
  2. EPUB 每页 xhtml 只放**自己那一张**图
     （曾把全书图片拼进第 1 页，阅读时第 1 页之后整本重复）。
  3. EPUB 图片扩展名与 media-type 必须跟真实字节一致
     （PNG 字节曾被命名 .jpg 且声明 image/jpeg，严格阅读器报损坏）。
  4. EPUB 的 dc:identifier 必须**跨进程稳定**
     （曾用内置 hash()，每个进程随机盐不同 → 同一章每次导出换一个书号）。
  5. merge_epub 不能丢 PNG 页、不能把源文件移走、不能依赖系统 temp 可写。
  6. EPUB 结构自检：mimetype 必须是第一个条目且不压缩、manifest 里的每个
     href 都要真实存在、spine 引用的 id 都要在 manifest 里。

跑法（在仓库根目录）：
    $env:APPDATA='D:\\AI\\工作区1\\mangakit\\.tmp\\appdata_roaming'
    $env:USERPROFILE='D:\\AI\\工作区1\\mangakit\\.tmp\\home'
    $env:PYTHONIOENCODING='utf-8'
    .tmp\\venv\\Scripts\\python.exe tools\\t_export.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from mangakit import exporter  # noqa: E402
from mangakit.images import HAVE_PIL  # noqa: E402

FAILED: list[str] = []
PASSED = 0

# 所有临时产物都放仓库 .tmp 下：本机系统 temp 不可写（PermissionError）。
TMPROOT = ROOT / ".tmp"
TMPROOT.mkdir(exist_ok=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [OK ] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def eq(name: str, got, want) -> None:
    check(name, got == want, f"got={got!r} want={want!r}")


def mkpages(d: Path, specs: list[tuple[int, int, str, tuple[int, int, int]]]) -> None:
    """按 specs 造页面：[(宽, 高, 扩展名, RGB)]。"""
    from PIL import Image
    for i, (w, h, ext, color) in enumerate(specs, 1):
        Image.new("RGB", (w, h), color).save(d / f"{i:04d}{ext}")


def epub_open(path: Path) -> dict:
    """解包 EPUB 成 {名字: 字节}，顺便做结构断言。"""
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        names = z.namelist()
        data = {n: z.read(n) for n in names}
    check(f"{path.name}: mimetype 是第一个条目", names[0] == "mimetype", f"got {names[0]!r}")
    check(f"{path.name}: mimetype 不压缩",
          infos[0].compress_type == zipfile.ZIP_STORED, f"got {infos[0].compress_type}")
    eq(f"{path.name}: mimetype 内容", data.get("mimetype"), b"application/epub+zip")
    check(f"{path.name}: 有 container.xml", "META-INF/container.xml" in data)
    check(f"{path.name}: 有 content.opf", "OEBPS/content.opf" in data)
    return data


def hrefs_exist(data: dict, opf_name: str = "OEBPS/content.opf") -> None:
    """manifest 里每个 href 都要在包里真实存在。"""
    import re
    opf = data[opf_name].decode("utf-8")
    base = opf_name.rsplit("/", 1)[0] + "/"
    hrefs = re.findall(r'<item[^>]*href="([^"]+)"', opf)
    missing = [h for h in hrefs if base + h not in data]
    check(f"manifest 引用的文件都存在（{len(hrefs)} 项）", not missing, f"缺 {missing[:5]}")
    ids = re.findall(r'<item[^>]*id="([^"]+)"', opf)
    spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
    bad = [s for s in spine if s not in ids]
    check(f"spine 引用的 id 都在 manifest 里（{len(spine)} 项）", not bad, f"缺 {bad[:5]}")


def main() -> int:
    if not HAVE_PIL:
        print("SKIP: 没有 Pillow，无法造测试图片")
        return 0

    work = Path(tempfile.mkdtemp(prefix="t_export_", dir=str(TMPROOT)))
    print(f"工作目录：{work}")
    try:
        # ---------------------------------------------------------------- 1
        print("\n== 1. 章节名带点时输出文件名不能丢号 ==")
        for name in ("第12.5话", "Ch.12.5", "第3卷.下", "第1话"):
            d = work / name
            d.mkdir()
            mkpages(d, [(40, 60, ".jpg", (10, 20, 30))])
            cbz = exporter.pack_cbz(d, "cbz")
            eq(f"{name} → cbz 名", cbz.name, f"{name}.cbz")
            check(f"{name} 的 cbz 落在同级目录", cbz.parent == work)
            check(f"{name} 的源目录仍在", d.is_dir())
            pdf = exporter.pack_cbz(d, "pdf")
            eq(f"{name} → pdf 名", pdf.name, f"{name}.pdf")
            epub = exporter.pack_cbz(d, "epub")
            eq(f"{name} → epub 名", epub.name, f"{name}.epub")
        # 12.5 与 12.6 不能互相覆盖
        a, b = work / "第12.5话", work / "第12.6话"
        for d, color in ((a, (255, 0, 0)), (b, (0, 255, 0))):
            d.mkdir(exist_ok=True)
            mkpages(d, [(40, 60, ".jpg", color)])
        za, zb = exporter.pack_cbz(a, "cbz"), exporter.pack_cbz(b, "cbz")
        check("第12.5话 / 第12.6话 是两个不同文件", za != zb, f"{za} vs {zb}")
        with zipfile.ZipFile(za) as x, zipfile.ZipFile(zb) as y:
            check("两话内容不同（没有互相覆盖）",
                  x.read("00001.jpg") != y.read("00001.jpg"))

        # ---------------------------------------------------------------- 2
        print("\n== 2. EPUB 每页只放自己的图 ==")
        d = work / "多页"
        d.mkdir()
        mkpages(d, [(40, 60, ".jpg", (i * 30, 0, 0)) for i in range(1, 5)])
        epub = exporter.to_epub(d, "多页测试")
        data = epub_open(epub)
        hrefs_exist(data)
        import re
        for n in range(1, 5):
            page = data[f"OEBPS/pages/{n:05d}.xhtml"].decode("utf-8")
            imgs = re.findall(r'<img[^>]*src="([^"]+)"', page)
            eq(f"第{n}页 xhtml 只含 1 张图", len(imgs), 1)
            check(f"第{n}页引用的是第{n}张",
                  imgs and imgs[0].endswith(f"{n:05d}.jpg"), f"got {imgs}")
        total_imgs = [k for k in data if "/images/" in k]
        eq("图片条目数 = 页数", len(total_imgs), 4)

        # ---------------------------------------------------------------- 3
        print("\n== 3. EPUB 扩展名 / media-type 跟真实字节一致 ==")
        d = work / "混合格式"
        d.mkdir()
        mkpages(d, [(40, 60, ".png", (1, 2, 3)),
                    (40, 60, ".jpg", (4, 5, 6)),
                    (40, 60, ".png", (7, 8, 9))])
        epub = exporter.to_epub(d, "混合")
        data = epub_open(epub)
        hrefs_exist(data)
        opf = data["OEBPS/content.opf"].decode("utf-8")
        check("PNG 声明为 image/png", 'media-type="image/png"' in opf)
        check("JPEG 声明为 image/jpeg", 'media-type="image/jpeg"' in opf)
        png_entries = [k for k in data if k.endswith(".png")]
        jpg_entries = [k for k in data if k.endswith(".jpg")]
        eq("png 条目数", len(png_entries), 2)
        eq("jpg 条目数", len(jpg_entries), 1)
        check("png 条目里确实是 PNG 字节",
              data[png_entries[0]][:8] == b"\x89PNG\r\n\x1a\n",
              f"got {data[png_entries[0]][:8]!r}")
        check("jpg 条目里确实是 JPEG 字节",
              data[jpg_entries[0]][:3] == b"\xff\xd8\xff",
              f"got {data[jpg_entries[0]][:3]!r}")
        # 页面 xhtml 的 src 要指向带正确扩展名的文件
        page1 = data["OEBPS/pages/00001.xhtml"].decode("utf-8")
        check("第1页 src 指向 .png", ".png" in page1, page1[:200])

        # ---------------------------------------------------------------- 4
        print("\n== 4. dc:identifier 跨进程稳定 ==")
        code = (
            "import sys;sys.path.insert(0,r'%s');"
            "from pathlib import Path;from mangakit import exporter;"
            "import zipfile,re;"
            "p=exporter.to_epub(Path(r'%s'),'x');"
            "z=zipfile.ZipFile(p);"
            "print(re.search(r'<dc:identifier[^>]*>([^<]+)<',z.read('OEBPS/content.opf').decode()).group(1))"
        ) % (str(ROOT), str(d))
        ids = []
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
            r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=env, timeout=120)
            if r.returncode != 0:
                check(f"子进程（HASHSEED={seed}）跑通", False, r.stderr[-400:])
                continue
            ids.append(r.stdout.strip().splitlines()[-1])
        eq("三个不同 HASHSEED 得到同一个书号", len(set(ids)), 1)
        check("书号不是空串", bool(ids and ids[0]))

        # ---------------------------------------------------------------- 5
        print("\n== 5. merge_epub ==")
        src = []
        for i, exts in enumerate([(".jpg",), (".png",), (".jpg", ".png")], 1):
            d = work / f"章{i}"
            d.mkdir()
            mkpages(d, [(40, 60, e, (i * 20, 0, 0)) for e in exts])
            src.append(exporter.to_epub(d, f"第{i}话"))
        merged = exporter.merge_epub(src, work / "合集.epub", "测试合集")
        mdata = epub_open(merged)
        hrefs_exist(mdata)
        n_imgs = len([k for k in mdata if "/images/" in k])
        eq("合并后图片总数 = 1+1+2", n_imgs, 4)
        check("PNG 页没有被丢掉",
              any(k.endswith(".png") for k in mdata if "/images/" in k),
              str([k for k in mdata if "/images/" in k]))
        check("源 EPUB 都还在（没被移走）", all(p.exists() for p in src),
              str([p.name for p in src if not p.exists()]))
        check("合集里没有残留临时目录",
              not [p for p in work.iterdir() if p.name.startswith(".mk_epub_")],
              str([p.name for p in work.iterdir() if p.name.startswith(".mk_epub_")]))
        # 单个输入不能被移走
        single_src = src[0]
        before = single_src.read_bytes()
        single_out = exporter.merge_epub([single_src], work / "单本.epub")
        check("单个输入合并后源文件仍在", single_src.exists())
        eq("单个输入合并后源文件内容不变", single_src.read_bytes(), before)
        eq("单个输入合并输出大小一致", single_out.stat().st_size, len(before))

        # ---------------------------------------------------------------- 6
        print("\n== 6. CBZ / ZIP / 长条图 基本正确性 ==")
        d = work / "基本"
        d.mkdir()
        mkpages(d, [(40, 60, ".jpg", (9, 9, 9)), (40, 80, ".jpg", (8, 8, 8))])
        cbz = exporter.pack_cbz(d, "cbz")
        with zipfile.ZipFile(cbz) as z:
            eq("CBZ 内部按页序命名", z.namelist(), ["00001.jpg", "00002.jpg"])
            eq("CBZ 用 STORED（不压缩）",
               {i.compress_type for i in z.infolist()}, {zipfile.ZIP_STORED})
        zipped = exporter.pack_cbz(d, "zip")
        with zipfile.ZipFile(zipped) as z:
            eq("ZIP 用 DEFLATED", {i.compress_type for i in z.infolist()},
               {zipfile.ZIP_DEFLATED})
        long_img = exporter.join_long_strip(d)
        from PIL import Image
        with Image.open(long_img) as im:
            eq("长条图高度 = 各页高度之和", im.size, (40, 140))
        check("长条图文件名带 _long", "_long" in long_img.name, long_img.name)

        # ---------------------------------------------------------------- 7
        print("\n== 7. 边界情况 ==")
        empty = work / "空目录"
        empty.mkdir()
        for mode in ("cbz", "zip", "pdf", "epub"):
            try:
                exporter.pack_cbz(empty, mode)
                check(f"空目录 {mode} 应报错", False, "没有抛异常")
            except ValueError:
                check(f"空目录 {mode} 抛 ValueError", True)
        try:
            exporter.pack_cbz(d, "rar")
            check("未知模式应报错", False, "没有抛异常")
        except ValueError:
            check("未知模式抛 ValueError", True)
        one = work / "单页"
        one.mkdir()
        mkpages(one, [(40, 60, ".jpg", (1, 1, 1))])
        e1 = exporter.to_epub(one, "单页")
        d1 = epub_open(e1)
        hrefs_exist(d1)
        eq("单页 EPUB 只有 1 个 xhtml",
           len([k for k in d1 if k.endswith(".xhtml")]), 1)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILED:
        print(f"=== 失败 {len(FAILED)} 项 / 通过 {PASSED} 项 ===")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"=== 全部通过：{PASSED} 项 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
