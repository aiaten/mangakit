"""漫画站 decryptData 的 Python 端口（mhzh 系）。

页面里 `var DATA='<base64>'` + `var cid=63235;`，pic.js 的 decryptData
把 DATA 解成 JSON 数组，每项含 url。算法（已验证）：
    raw = base64decode(DATA)
    key = KEYS[cid % 10].encode()          # KEYS 本身是 base64，要再解一层
    out[i] = raw[i] ^ key[i % len(key)]
    json.loads(base64decode(out))
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

# 从 pic.js 的字符串表里 dump 出来的 10 个 key（base64 形式，见 tools/dump_keys.js）
KEYS_B64 = [
    "NC1iWGQ5aU4=", "NC1SWHlqcnk=", "NC1vWXZ3Vnk=", "NC00Wlk1N1U=", "NC1tYkpwVTc=",
    "NC02TU0yRWk=", "NC01NFRpUXI=", "NC1QaDV4eDk=", "NC1iWWdlUFI=", "NC1aOUEzYlc=",
]


def _keys() -> list[bytes]:
    return [base64.b64decode(k) for k in KEYS_B64]


def decrypt_data(data_b64: str, cid: int) -> Any:
    keys = _keys()
    key = keys[cid % len(keys)]
    raw = base64.b64decode(data_b64)
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return json.loads(base64.b64decode(out).decode("utf-8"))


def pages_from_html(html: str) -> list[str]:
    m = re.search(r"(?:var|let|const)\s+DATA\s*=\s*'([^']+)'", html)
    if not m:
        return []
    c = re.search(r"(?:var|let|const)\s+cid\s*=\s*(\d+)", html)
    cid = int(c.group(1)) if c else 0
    try:
        data = decrypt_data(m.group(1), cid)
    except Exception:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("url"):
            out.append(str(item["url"]))
        elif isinstance(item, str):
            out.append(item)
    return out