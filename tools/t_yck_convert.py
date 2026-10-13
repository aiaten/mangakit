"""异次元源转换器（mangakit.ycksource）自检脚本。

覆盖五组断言：
  1. 离线：三个本地真实样本（944 看漫画 / 2 来漫画 / 3 爱米推漫画）的转换结论，
     其中 944/3 用 @js: 必须判「不可转 + 中文原因」，2 是加密块必须判「加密源」。
  2. 离线：选择器语法样本，断言转出来的 css / xpath / attr 字符串正确。
  3. 联网：fetch_index() 抓第 0 页，断言条数 > 50、sid > 0、site_url 以 http 开头。
  4. 联网：挑可转的源跑 check_source(sid, deep=True)，通过的规则写到
     mangakit/.tmp/yck/converted/<id>.json。
  5. 末尾打印 ALL YCK TESTS PASSED 或 N 项失败；失败时退出码非 0。

用法（必须在 mangakit 根目录的 PYTHONPATH 下跑）：
    $env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
    $env:PYTHONPATH='...\\mangakit'
    python tools\\t_yck_convert.py [--no-net]
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mangakit import ycksource as Y  # noqa: E402

TMP = ROOT / ".tmp" / "yck"
SURVEY = ROOT / ".tmp" / "survey" / "json_raw"
OUTDIR = TMP / "converted"

FAILURES: list[str] = []
PASSES = 0


def check(cond: bool, what: str) -> bool:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  [PASS] {what}")
    else:
        FAILURES.append(what)
        print(f"  [FAIL] {what}")
    return cond


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def load_source(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  (跳过 {path.name}：{type(exc).__name__}: {exc})")
        return None
    if isinstance(obj, list):
        obj = obj[0] if obj else None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# 1. 离线：三个本地真实样本
# ---------------------------------------------------------------------------


def test_local_samples() -> None:
    print("\n=== 1. 离线：本地三个真实样本 ===")
    cases = {
        944: ("NO", "看漫画（ruleBookContent 是 @js: 解码脚本）"),
        2: ("NO", "来漫画（ruleChapterList/ruleBookContent 都是 :_H006_ 加密块）"),
        3: ("NO", "爱米推漫画（ruleChapterList 是 @js:）"),
    }
    for sid, (want, desc) in cases.items():
        path = TMP / f"yck_{sid}.json"
        if not path.exists():
            check(False, f"本地样本 {path.name} 不存在")
            continue
        raw = load_source(path)
        if raw is None:
            check(False, f"本地样本 {path.name} 不是有效的异次元源对象")
            continue
        raw.setdefault("_yck_id", sid)
        rule, why = Y.convert(raw)
        got = "OK" if rule else "NO"
        print(f"  [{sid}] {got}  {why[:150]}")
        check(got == want, f"#{sid}（{desc}）应判 {want}，实际 {got}")
        if got == "NO":
            check(has_cjk(why), f"#{sid} 的不可用原因是中文")

    # 944 / 3 必须点明是脚本规则，2 必须点明是加密源
    raw944 = load_source(TMP / "yck_944.json")
    if raw944:
        rule, why = Y.convert(raw944)
        check(rule is None, "#944 用 @js: 必须判不可转（不许硬编出规则）")
        check("@js" in why or "脚本" in why, "#944 的原因要说明是脚本规则")
    raw2 = load_source(TMP / "yck_2.json")
    if raw2:
        rule, why = Y.convert(raw2)
        check(rule is None, "#2 加密源必须判不可转")
        check("加密" in why, "#2 的原因要说明是加密源")


# ---------------------------------------------------------------------------
# 2. 离线：选择器语法样本
# ---------------------------------------------------------------------------


def test_selector_samples() -> None:
    print("\n=== 2. 离线：选择器语法样本 ===")

    def conv(expr: str) -> dict:
        spec = Y.convert_selector(expr, field="样本")
        return {k: v for k, v in spec.items() if k != "_notes"}

    samples: list[tuple[str, dict]] = [
        ("class.a@tag.b@text", {"css": ".a b", "text": True}),
        ("id.y@href", {"css": "#y", "attr": "href", "url": False}),
        ("-.z li a", {"css": ".z li a", "text": True}),
        ("tag.img@data-src|tag.img@src", {"css": "img", "attr": ["data-src", "src"], "url": False}),
        ("a@href|img@src", {"css": "a, img", "attr": ["href", "src"], "url": False}),
    ]
    for expr, want in samples:
        try:
            got = conv(expr)
        except Exception as exc:  # noqa: BLE001
            check(False, f"{expr!r} 转换抛异常：{type(exc).__name__}: {exc}")
            continue
        print(f"  {expr!r:52} -> {got}")
        check(got == want, f"{expr!r} 应转成 {want}，实际 {got}")

    # class.x.2@text：末尾数字是 0 基下标（第 3 个）。cssselect 不支持无标签的
    # :nth-of-type()，所以只能整条翻成 XPath，语义是「第 3 个匹配」。
    try:
        got = conv("class.x.2@text")
        print(f"  {'class.x.2@text':52} -> {got}")
        ok = got.get("text") is True and (
            got.get("xpath", "").endswith("[3]")
            or "nth-of-type(3)" in got.get("css", "")
        )
        check(ok, "class.x.2@text 的 .2 要落到第 3 个（XPath [...][3] 或 :nth-of-type(3)）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"class.x.2@text 转换抛异常：{type(exc).__name__}: {exc}")

    # 明确拒绝的类型
    rejects = [
        ("@js:result.replace('a','b')", "脚本规则"),
        ("@css:document.write('x')", None),
        ("class.a@text#(^.*?【.*?】).*$#$1", None),  # 捕获组替换 → 其实是可映射的
        ("$.data[*].image", "JSONPath"),
        ("color.#ffffff@text", "color."),
        ("!0", None),
    ]
    # @js: 必须拒绝
    try:
        Y.convert_selector(rejects[0][0], field="样本")
        check(False, "@js: 脚本规则必须被拒绝")
    except Y.UnsupportedSelector as exc:
        print(f"  {rejects[0][0]!r} -> 拒绝：{exc}")
        check("@js" in str(exc) or "脚本" in str(exc), "@js: 的拒绝原因是中文且说明是脚本")
    # JSONPath 必须拒绝
    try:
        Y.convert_selector("$.data[*].image", field="样本")
        check(False, "JSONPath 必须被拒绝")
    except Y.UnsupportedSelector as exc:
        print(f"  {'$.data[*].image'!r} -> 拒绝：{exc}")
        check("JSON" in str(exc), "JSONPath 的拒绝原因要说明是 JSON 接口")
    # color. 方言必须拒绝
    try:
        Y.convert_selector("color.#ffffff@text", field="样本")
        check(False, "color. 方言必须被拒绝")
    except Y.UnsupportedSelector as exc:
        print(f"  {'color.#ffffff@text'!r} -> 拒绝：{exc}")
        check("color" in str(exc), "color. 的拒绝原因要说明是方言能力")
    # !N 负索引必须拒绝（不猜语义）
    try:
        Y.convert_selector(".pure-g!0@.comics-chapters", field="样本")
        check(False, "!0 负索引必须被拒绝（不猜语义）")
    except Y.UnsupportedSelector as exc:
        print(f"  {'.pure-g!0@.comics-chapters'!r} -> 拒绝：{exc}")
        check("!0" in str(exc) or "负索引" in str(exc), "!N 的拒绝原因要说明是未文档化的负索引")
    # 捕获组替换应映射成 re + group
    try:
        got = conv("tag.p.0@text#(^.*?【.*?】).*$#$1")
        print(f"  {'tag.p.0@text#(^.*?【.*?】).*$#$1':52} -> {got}")
        check(got.get("re") == "(^.*?【.*?】).*$" and got.get("group") == 1,
              "正则+$1 的替换要映射成引擎的 re + group")
    except Exception as exc:  # noqa: BLE001
        check(False, f"捕获组替换应当可映射，却抛出 {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 3. 联网：索引页
# ---------------------------------------------------------------------------


def test_index() -> list:
    print("\n=== 3. 联网：yck2026 索引页第 0 页 ===")
    try:
        rows = Y.fetch_index(page=0)
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] fetch_index 失败（网络问题不算转换器 bug）：{type(exc).__name__}: {exc}")
        return []
    print(f"  拿到 {len(rows)} 条")
    for r in rows[:5]:
        print(f"    sid={r.sid:<5} {r.site_name:<14} {r.site_url:<32} "
              f"发={r.can_find} 搜={r.can_search} 下载={r.downloads} {r.updated}")
    check(len(rows) > 50, f"第 0 页条数 > 50（实际 {len(rows)}）")
    check(all(r.sid > 0 for r in rows), "每条 sid > 0")
    check(all(r.site_url.startswith("http") for r in rows), "每条 site_url 以 http 开头")
    if rows:
        bad = [r for r in rows if not r.site_name]
        check(not bad, f"每条都能解析出站点名（{len(bad)} 条为空）")
    return rows


# ---------------------------------------------------------------------------
# 4. 联网：真跑校验
# ---------------------------------------------------------------------------


def test_check_sources(rows: list) -> list[tuple[str, int, int]]:
    print("\n=== 4. 联网：check_source 真跑（probe 引擎验收） ===")
    OUTDIR.mkdir(parents=True, exist_ok=True)

    # 优先挑：索引里带「搜」徽章、且本地 survey 里能转、ruleBookContent 不含 @js: 的源。
    preferred = [938, 934, 667, 35, 22, 110, 730, 69, 68, 855, 30]
    if rows:
        indexed = {r.sid for r in rows}
        preferred = [s for s in preferred if s in indexed] + [
            s for s in preferred if s not in indexed
        ]

    good: list[tuple[str, int, int]] = []
    tried = 0
    for sid in preferred:
        if len(good) >= 3:
            break
        tried += 1
        try:
            res = Y.check_source(sid, deep=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{sid}] 异常（check_source 不该抛异常）：{type(exc).__name__}: {exc}")
            FAILURES.append(f"check_source({sid}) 抛出了异常")
            continue
        print(f"  [{sid}] ok={res.ok} status={res.status} {res.message[:130]}")
        if res.ok and res.rule:
            path = OUTDIR / f"{res.rule.get('id')}.json"
            path.write_text(
                json.dumps(res.rule, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            good.append((str(res.rule.get("id")), res.chapters, res.images))
            print(f"         -> 已写入 {path.name}（章节 {res.chapters}、图片 {res.images}）")
            check(res.chapters > 0, f"#{sid} 校验通过且章节数 > 0")
            check(res.images > 0, f"#{sid} 校验通过且首话图片数 > 0")
        else:
            for line in res.log[-3:]:
                print(f"         · {line[:150]}")
    if not good:
        print("  [WARN] 没有源通过联网校验 —— 如实报告为 0，不伪造成功")
    if tried == 0:
        print("  [SKIP] 没有可试的候选（索引抓取失败）")
    return good


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    no_net = "--no-net" in argv
    print("MangaKit 异次元源转换器自检")
    print(f"模块：{Y.__file__}")

    test_local_samples()
    test_selector_samples()

    good: list[tuple[str, int, int]] = []
    if no_net:
        print("\n=== 3/4. 联网测试已跳过（--no-net） ===")
    else:
        try:
            rows = test_index()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            rows = []
        try:
            good = test_check_sources(rows)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            FAILURES.append("联网校验阶段异常中断")

    print("\n" + "=" * 62)
    print(f"离线+联网断言：通过 {PASSES} 项，失败 {len(FAILURES)} 项")
    if good:
        print("真实可用的源：")
        for sid, chs, imgs in good:
            print(f"  {sid}：章节 {chs} 话、首话 {imgs} 张图")
    else:
        print("真实可用的源：0 个" + ("（联网测试被跳过）" if no_net else ""))
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  -", f)
        print(f"{len(FAILURES)} 项失败")
        return 1
    print("ALL YCK TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
