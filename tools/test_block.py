"""detect_block 判定回归：WAF 页必拦、正常页（哪怕挂 Cloudflare 统计）不拦。"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mangakit.http_client as H  # noqa: E402


def mk(code, body):
    return httpx.Response(
        code,
        headers={"content-type": "text/html; charset=utf-8"},
        content=body.encode("utf-8"),
        request=httpx.Request("GET", "http://x"),
    )


CASES = [
    ("702 WAF拦截页", mk(702, "<html>访问代码: 702 访问编号: abc 触发防火墙规则(417) 🛡</html>"), True),
    ("200正常页带CF统计脚本", mk(200, '<html><script src="/cdn-cgi/rum.js"></script>cloudflare 漫画 第1话</html>'), False),
    ("200正常漫画页", mk(200, "<html><body>第1话</body></html>"), False),
    ("404正常", mk(404, "<html>404 Not Found</html>"), False),
    ("CF挑战页200", mk(200, "<html><title>Just a moment...</title>Checking your browser</html>"), True),
    ("502网关错误", mk(502, "<html>502 Bad Gateway</html>"), False),
    ("200正常页含captcha字样", mk(200, "<html>本章无验证码 captcha-free 第2话</html>"), False),
    # 站方数据库打满：200 + 51 字节人话，不拦的话解析层只会报「没有章节」
    ("200站方数据库故障页", mk(200, "数据库链接失败：SQLSTATE[HY000] [1040] Too many connections"), True),
    ("200站方InternalServerError", mk(200, "Internal Server Error"), True),
    # 正常页正文里带这些词，不能误伤（长 HTML + 真正页面才放行）
    ("200正常页提到数据库", mk(200, "<html><body>作者说数据库梗 第1话</body></html>"), False),
    ("200正常页提到SQLState字样", mk(200, "<html><body>SQLState 教程 第2话</body></html>"), False),
]

ok = True
for name, r, want_block in CASES:
    got = H.detect_block(r)
    blocked = got is not None
    good = blocked == want_block
    ok = ok and good
    print(f"{'OK ' if good else 'BAD'} {name:24} blocked={blocked} -> {got}")

# 站方故障的提示必须说清「是站方的问题、过会再试」，而不是让人去改规则
r = mk(200, "数据库链接失败：SQLSTATE[HY000] [1040] Too many connections")
msg = H.detect_block(r) or ""
told_right = ("站点服务端异常" in msg and "站方" in msg and "过一会儿" in msg)
ok = ok and told_right
print(f"{'OK ' if told_right else 'BAD'} 站方故障提示说清责任方 -> {msg[:60]}")

# 头值非 ASCII 兜底
u = H._ascii_header_value("https://mhquan.com/search?q=海贼王")
good = u == "https://mhquan.com/search?q=%E6%B5%B7%E8%B4%BC%E7%8E%8B"
ok = ok and good
print(f"{'OK ' if good else 'BAD'} Referer中文编码 -> {u}")
print("ALL DETECT TESTS PASSED" if ok else "DETECT TESTS FAILED")