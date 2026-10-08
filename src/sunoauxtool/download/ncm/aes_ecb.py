"""纯标准库 AES-128 加密（ECB 模式 + PKCS#7 填充）。

NCM 容器只用到 AES-128-ECB 单块组操作，按 FIPS-197 独立实现：
SubBytes / ShiftRows / MixColumns / AddRoundKey 及其逆。
正确性由 FIPS-197 附录 C.1 官方测试向量锁定（见 tests/test_download_ncm.py）。
"""

from __future__ import annotations

# S-box（FIPS-197 图 7）：由 GF(2^8) 乘法逆元 + 仿射变换生成的固定表
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
# 逆 S-box：SBOX 的逆映射
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

# 轮常量（Rcon）
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]

# GF(2^8) 乘法辅助：xtime(x) = x * 2 mod (x^8 + x^4 + x^3 + x + 1)


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法（俄罗斯农民乘法 + 约减多项式 0x11B）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p


def _key_expansion(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展：11 个 4x4 轮密钥（每轮 16 字节，列优先）。"""
    if len(key) != 16:
        raise ValueError("AES-128 密钥必须为 16 字节")
    words = [list(key[i * 4 : i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [_SBOX[b] for b in temp]  # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([a ^ b for a, b in zip(words[i - 4], temp)])
    return [words[4 * r : 4 * r + 4] for r in range(11)]


def _xor_state(state: list[list[int]], rk: list[list[int]]) -> None:
    for c in range(4):
        for r in range(4):
            state[r][c] ^= rk[c][r]


def _encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    """加密单个 16 字节块（列优先状态布局）。"""
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _xor_state(state, round_keys[0])

    for rnd in range(1, 11):
        # SubBytes
        for r in range(4):
            for c in range(4):
                state[r][c] = _SBOX[state[r][c]]
        # ShiftRows
        for r in range(1, 4):
            state[r] = state[r][r:] + state[r][:r]
        # MixColumns（末轮跳过）
        if rnd < 10:
            for c in range(4):
                a0, a1, a2, a3 = state[0][c], state[1][c], state[2][c], state[3][c]
                state[0][c] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
                state[1][c] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
                state[2][c] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
                state[3][c] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
        _xor_state(state, round_keys[rnd])

    return bytes(state[r][c] for c in range(4) for r in range(4))


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    """解密单个 16 字节块（InvShiftRows / InvSubBytes / InvMixColumns）。"""
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _xor_state(state, round_keys[10])

    for rnd in range(9, -1, -1):
        # InvShiftRows
        for r in range(1, 4):
            state[r] = state[r][-r:] + state[r][:-r]
        # InvSubBytes
        for r in range(4):
            for c in range(4):
                state[r][c] = _INV_SBOX[state[r][c]]
        _xor_state(state, round_keys[rnd])
        # InvMixColumns（跳过 round 0 之后那次——即 rnd==0 时已无 MixColumns）
        if rnd > 0:
            for c in range(4):
                a0, a1, a2, a3 = state[0][c], state[1][c], state[2][c], state[3][c]
                state[0][c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
                state[1][c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
                state[2][c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
                state[3][c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)

    return bytes(state[r][c] for c in range(4) for r in range(4))


def pkcs7_pad(data: bytes) -> bytes:
    """PKCS#7 填充到 16 字节倍数。"""
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    """PKCS#7 去填充（非法填充抛 ValueError）。"""
    if not data or len(data) % 16:
        raise ValueError("PKCS#7: 长度不是块大小整数倍")
    n = data[-1]
    if not 1 <= n <= 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("PKCS#7: 填充非法")
    return data[:-n]


def aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 解密（data 须为 16 字节倍数，不去填充）。"""
    if len(data) % 16:
        raise ValueError("ECB 输入须为 16 字节倍数")
    rk = _key_expansion(key)
    return b"".join(
        _decrypt_block(data[i : i + 16], rk) for i in range(0, len(data), 16)
    )


def aes128_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB 加密（data 须为 16 字节倍数，不填充）。"""
    if len(data) % 16:
        raise ValueError("ECB 输入须为 16 字节倍数")
    rk = _key_expansion(key)
    return b"".join(
        _encrypt_block(data[i : i + 16], rk) for i in range(0, len(data), 16)
    )
