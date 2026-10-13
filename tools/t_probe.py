"""直接跑探测，不走 GUI。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from mangakit.probe import probe_sync

URLS = sys.argv[1:] or [
    "https://www.dingmanhua.com/comic/5758",     # 已知可下（内置站，探测应也能认）
    "https://www.shenqimanhua.com/comic/xxx",    # 占位
]

for u in URLS:
    r = probe_sync(u, deep=True, timeout=60)
    print("=" * 70)
    print(u)
    print("-" * 70)
    print(r.summary())
    print(f"status={r.status}  addable={r.addable}")
    if r.reason:
        print("原因:", r.reason)
    for line in r.log:
        print("  ·", line)
    if r.rule:
        import json
        print("规则:", json.dumps(r.rule, ensure_ascii=False)[:600])
