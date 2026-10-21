"""纯 Python 的 AES-128-CBC 解密（零第三方依赖）。

为什么自己写：拷贝漫画（copy4000 / copy5000 系）把章节列表和章节图片都放在
一个 AES-128-CBC 密文里，但本项目只依赖 httpx + lxml，不想为了两个字段引入
cryptography（打包体积 + 一堆二进制轮子）。纯 Python 实现解 30KB 密文大约
几十毫秒，对下载器来说完全够用。

密文格式（页面里直接给出的字符串，不是标准 base64）：
    iv  = 字符串前 16 个字符的 UTF-8 字节
    ct  = 剩下部分的 hex 解码
    key = 页面里 `var cct = '...'` 的 UTF-8 字节（正好 16 字节，即 AES-128）
    填充 = PKCS#7

只实现解密方向：AES 的 S 盒 / 逆 S 盒 / 密钥扩展 / 逆密码 / CBC 串联。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 常量表

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

# 逆 S 盒由 S 盒反推，避免再抄一张 256 字节的表（抄错了很难查）。
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 上的乘法（AES 用的不可约多项式 0x11B）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


# 逆 MixColumns 的固定系数
_M9 = [_gmul(i, 9) for i in range(256)]
_M11 = [_gmul(i, 11) for i in range(256)]
_M13 = [_gmul(i, 13) for i in range(256)]
_M14 = [_gmul(i, 14) for i in range(256)]


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展：返回 44 个 4 字节字。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 的密钥必须是 16 字节，收到 {len(key)} 字节")
    w: list[list[int]] = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = w[i - 1][:]
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                  # RotWord
            temp = [_SBOX[b] for b in temp]             # SubWord
            temp[0] ^= _RCON[i // 4 - 1]                # Rcon
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return w


def _add_round_key(state: list[int], w: list[list[int]], rnd: int) -> None:
    # 每轮 4 个字拼成 16 字节，直接按字节异或（原地改）
    for c in range(4):
        word = w[rnd * 4 + c]
        base = c * 4
        state[base] ^= word[0]
        state[base + 1] ^= word[1]
        state[base + 2] ^= word[2]
        state[base + 3] ^= word[3]


def _inv_shift_rows(s: list[int]) -> None:
    # 第 r 行循环右移 r 位。状态是列优先：行 r、列 c 的下标 = r + 4c
    # 第 1 行右移 1
    s[1], s[5], s[9], s[13] = s[13], s[1], s[5], s[9]
    # 第 2 行右移 2
    s[2], s[6], s[10], s[14] = s[10], s[14], s[2], s[6]
    # 第 3 行右移 3（= 左移 1）
    s[3], s[7], s[11], s[15] = s[7], s[11], s[15], s[3]


def _inv_sub_bytes(s: list[int]) -> None:
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]


def _inv_mix_columns(s: list[int]) -> None:
    for c in range(4):
        b = c * 4
        a0, a1, a2, a3 = s[b], s[b + 1], s[b + 2], s[b + 3]
        s[b] = _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3]
        s[b + 1] = _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3]
        s[b + 2] = _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3]
        s[b + 3] = _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]


def _decrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    s = list(block)
    _add_round_key(s, w, 10)
    for rnd in range(9, 0, -1):
        _inv_shift_rows(s)
        _inv_sub_bytes(s)
        _add_round_key(s, w, rnd)
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    _inv_sub_bytes(s)
    _add_round_key(s, w, 0)
    return bytes(s)


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC 解密 + 去 PKCS#7 填充。"""
    if len(iv) != 16:
        raise ValueError(f"iv 必须是 16 字节，收到 {len(iv)} 字节")
    if len(ciphertext) % 16:
        raise ValueError(f"密文长度必须是 16 的倍数，收到 {len(ciphertext)} 字节")
    w = _expand_key(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(ciphertext), 16):
        blk = ciphertext[i:i + 16]
        plain = _decrypt_block(blk, w)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = blk
    return _unpad(bytes(out))


def decrypt_page_blob(blob: str, key_text: str) -> str:
    """解拷贝漫画那种「iv 明文 + hex 密文」拼接串，返回明文文本。

    blob 形如 '3f1t9qj8IwhP6RpZ' + '<36800 个 hex 字符>'，
    key_text 是页面里的 cct 变量（16 个 ASCII 字符）。
    """
    blob = (blob or "").strip()
    if len(blob) < 32:
        raise ValueError(f"密文串太短（{len(blob)} 字符）")
    iv = blob[:16].encode("utf-8")
    ct = bytes.fromhex(blob[16:])
    key = (key_text or "").encode("utf-8")
    return cbc_decrypt(key, iv, ct).decode("utf-8")
