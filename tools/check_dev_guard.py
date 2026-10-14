r"""验证 _dev_sandbox() 只拦开发态脚本，不误伤真实运行方式。

**为什么不直接跑一遍 `python -m mangakit`**：那会真的把 GUI 起起来并阻塞住
（第一次写这个检查时就被 180s 超时打断）。所以这里用同样的办法验证语义 ——
在子进程里把 `sys.argv[0]` / `sys.frozen` 摆成真实运行时的样子，只看
`app_dir()` 落在哪里。这正好也是护栏的判断依据，等价且不启动界面。

必须拿到**真库** %APPDATA%\mangakit 的四种真实场景：
  1. `python -m mangakit`      → argv[0] = <仓库>\mangakit\__main__.py
  2. `python main.py`          → argv[0] = <仓库>\main.py
  3. 打包后的 exe              → sys.frozen = True
  4. 用户自己设了 MANGAKIT_HOME → 显式覆盖优先
必须被**隔离**的一种：从 `<仓库>\tools\xxx.py` 起的脚本。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REAL = Path(os.environ.get("APPDATA", "")) / "mangakit"


def probe(*, argv0: str | None = None, frozen: bool = False,
          home: str | None = None) -> str:
    """在子进程里摆出指定的启动形态，返回它算出的 app_dir()。"""
    setup = ["import sys"]
    if frozen:
        setup.append("sys.frozen = True")
    if argv0 is not None:
        setup.append(f"sys.argv = [{argv0!r}]")
    if home is not None:
        setup.append(f"import os; os.environ['MANGAKIT_HOME'] = {home!r}")
    setup.append("import mangakit.config as c")
    setup.append("print('HOME=' + str(c.app_dir()))")

    env = dict(os.environ)
    env.pop("MANGAKIT_HOME", None)
    env.pop("MANGAKIT_ALLOW_REAL_HOME", None)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable, "-c", "\n".join(setup)],
                       capture_output=True, text=True, encoding="utf-8",
                       env=env, cwd=str(ROOT), timeout=120)
    if p.returncode != 0:
        return f"<失败 rc={p.returncode}> {(p.stderr or '').strip()[:300]}"
    for ln in (p.stdout or "").splitlines():
        if ln.startswith("HOME="):
            return ln[5:].strip()
    return "<无输出> " + (p.stdout or "").strip()[:150]


def check(name: str, got: str, want: Path, *, equal: bool = True) -> bool:
    try:
        same = Path(got) == want
    except (OSError, ValueError):
        same = False
    ok = same if equal else not same
    print(f"  [{'ok ' if ok else 'BAD'}] {name}\n         → {got}")
    return ok


def main() -> int:
    print(f"真库 = {REAL}")
    results = [
        check("python -m mangakit（argv[0]=mangakit\\__main__.py）",
              probe(argv0=str(ROOT / "mangakit" / "__main__.py")), REAL),
        check("python main.py（argv[0]=<仓库>\\main.py）",
              probe(argv0=str(ROOT / "main.py")), REAL),
        check("打包后的 exe（sys.frozen=True，argv[0] 假装在 tools/ 下）",
              probe(frozen=True,
                    argv0=str(ROOT / "tools" / "gui_smoke.py")), REAL),
        check("用户显式设 MANGAKIT_HOME", 
              probe(home=str(Path(os.environ["TEMP"]) / "mk_iso_override_test")),
              Path(os.environ["TEMP"]) / "mk_iso_override_test"),
        check("从 tools/ 起的脚本（必须隔离）",
              probe(argv0=str(ROOT / "tools" / "gui_smoke.py")), REAL, equal=False),
    ]
    bad = results.count(False)
    print("=== " + ("开发态护栏行为正确" if not bad else f"{bad} 项不符合预期") + " ===")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
