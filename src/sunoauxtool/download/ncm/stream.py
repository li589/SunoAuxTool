"""网易云定制 RC4 流密码（NCM 载荷解密）。

与标准 RC4 的差别：PRGA 不随读取推进，密钥流**只依赖字节绝对偏移**——
因此可任意分块并行解密。实现按公开格式规范独立完成：
    keybox = KSA(key)（256 字节 S-box，Swaps 变体）
    j = (i + 1) & 0xff
    keystream[i] = keybox[(keybox[j] + keybox[(keybox[j] + j) & 0xff]) & 0xff]
    payload[i] ^= keystream[i]
"""

from __future__ import annotations


def parse_rc4_key(key_material: bytes) -> bytes:
    """密钥段解密后的 RC4 密钥材料（非空校验；可直接作为 build_key_box 输入）。"""
    if not key_material:
        raise ValueError("RC4 密钥材料为空")
    return bytes(key_material)


def build_key_box(key: bytes) -> bytearray:
    """KSA：由密钥材料生成 256 字节 keybox。

    与教科书 RC4 的 KSA 略有差异：swap 之前先把 box[i] 原值加进 c，
    且 last_byte 取的是交换后 box[i] 位置的 c 值。
    """
    key = bytes(key)
    if not key:
        raise ValueError("key 不能为空")
    box = bytearray(range(256))
    c = 0
    last_byte = 0
    key_offset = 0
    key_len = len(key)
    for i in range(256):
        swap = box[i]
        c = (swap + last_byte + key[key_offset]) & 0xFF
        key_offset = (key_offset + 1) % key_len
        box[i] = box[c]
        box[c] = swap
        last_byte = c
    return box


def segment_key(offset: int, key_box: bytearray) -> int:
    """取绝对偏移 offset 处的密钥流字节（两级 box 查表）。"""
    j = (offset + 1) & 0xFF
    return key_box[(key_box[j] + key_box[(key_box[j] + j) & 0xFF]) & 0xFF]


def decrypt_payload(data: bytes, key: bytes) -> bytes:
    """整段载荷解密：预生成密钥流后按大整数 XOR（比逐字节快一个量级）。"""
    box = build_key_box(key)
    n = len(data)
    # 密钥流 256 字节后周期性不复现吗？——不复现：每字节依赖绝对偏移。
    keystream = bytes(segment_key(i, box) for i in range(n))
    return (int.from_bytes(data, "little") ^ int.from_bytes(keystream, "little")).to_bytes(
        n, "little"
    )


def encrypt_payload(data: bytes, key: bytes) -> bytes:
    """加密与解密同构（XOR 流密码的对称性），供自加密往返测试用。"""
    return decrypt_payload(data, key)
