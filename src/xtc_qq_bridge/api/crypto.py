# -*- coding: utf-8 -*-
"""小天才 HTTP 请求的签名与加密信封。

协议结构（与 imoo-client 一致，见包 docstring 的出处说明）
----------------------------------------------------------
每个请求带一组固定头：

    Base-Request-Param: <BRP，可能被 AES 加密后再 base64>
    Eebbk-Sign:         <MD5(uri + BRP明文 + body明文 + aes_key) 大写十六进制>
    model / Version / Grey / dataCenterCode ...

其中 **BRP（Base-Request-Param）** 是一段字段顺序固定的 JSON 明文：

    {"imFag":"1","appId":"2","program":"watch","timestamp":"...",
     "registId":"...","deviceId":"...","token":"...","accountId":"...",
     "mac":"unkown","requestId":"..."}

签名的关键点：**MD5 对明文 BRP 计算，而不是对加密后的**。字段顺序、字段名、
甚至 `"mac":"unkown"` 这个拼写都必须逐字节一致 —— 服务端是拿同一份明文重算的。

请求体在 v3 下是：gzip(JSON) -> AES-ECB-PKCS7 -> base64。
响应体同样如此（`_SUCCESS_CODES` 之外的字段解密后才能读）。

为什么要自己实现 AES
--------------------
项目现有依赖只有 pyyaml；为了不让"实验性功能"把 cryptography 变成硬依赖，
这里优先用 `cryptography`（若已安装），否则退回纯标准库实现。
纯 Python AES-ECB 只用于小块数据（请求体通常几 KB），性能足够。
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field

__all__ = [
    "HttpAccount",
    "ImooCrypto",
    "build_signed_request",
    "SUCCESS_CODES",
]

# 成功码：okii 域用 "200"，tiancaixing 域用 "000001"，部分接口回 "0"
SUCCESS_CODES = frozenset({"200", "0", "000001"})


@dataclass
class HttpAccount:
    """一个已登录账号的身份与加密材料。

    字段全部来自本地配置，**不要打印到日志**（`__repr__` 已做脱敏）。
    """

    bind_number: str = ""       # 手表绑定号 -> deviceId
    chip_id: str = ""           # -> token
    account_id: str = ""
    regist_id: int = 0
    inner_model: str = ""       # -> model 头，如 XTC_Z10
    soft_version: str = ""      # -> Version: W_<soft_version>
    encrypt_key: str = ""       # "keyId:aesKey:eebbkKey"（v3）或空（v0 明文）
    grey: str = "0"
    data_center: str = "CN_BJ"

    def __repr__(self) -> str:  # pragma: no cover - 仅用于排错时人工查看
        """脱敏表示：只暴露"有没有配"，不暴露值。"""
        def mask(v: str) -> str:
            s = str(v or "")
            if not s:
                return "未配置"
            return f"已配置({len(s)}位)"

        return (f"HttpAccount(deviceId={mask(self.bind_number)}, "
                f"token={mask(self.chip_id)}, accountId={mask(self.account_id)}, "
                f"registId={self.regist_id or '未配置'}, "
                f"model={self.inner_model or '未配置'}, "
                f"encryptKey={mask(self.encrypt_key)})")


@dataclass
class ImooCrypto:
    """按 encrypt_key 决定加密版本：v3 = AES-ECB，v0 = 明文。"""

    encrypt_key: str = ""
    version: int = field(init=False, default=0)
    key_id: str = field(init=False, default="")
    aes_key: str = field(init=False, default="")
    eebbk_key: str = field(init=False, default="")

    def __post_init__(self) -> None:
        key = str(self.encrypt_key or "")
        if not key:
            return
        parts = key.split(":")
        # v3 形如 "keyId:0123456789abcdef:eebbkKey"（aesKey 必须 16 字节）
        if len(parts) == 3 and parts[0] and len(parts[1]) == 16 and parts[2]:
            self.version = 3
            self.key_id, self.aes_key, self.eebbk_key = parts
        else:
            # 结构不认识：退回明文，而不是抛异常把整条链路打断
            self.version = 0

    # ---------------------------------------------------------------- 编解码
    def encrypt_header(self, text: str) -> str:
        """BRP 头：v3 时 AES-ECB 后 base64，v0 原样。"""
        if self.version == 0:
            return text
        return base64.b64encode(_aes_ecb_encrypt(text.encode("utf-8"),
                                                 self.aes_key.encode("utf-8"))).decode("ascii")

    def encrypt_body(self, text: str) -> str:
        """请求体：gzip -> AES-ECB -> base64。"""
        if self.version == 0:
            return text
        packed = gzip.compress(text.encode("utf-8"))
        return base64.b64encode(_aes_ecb_encrypt(packed,
                                                 self.aes_key.encode("utf-8"))).decode("ascii")

    def decrypt_body(self, text: str) -> str:
        """响应体：base64 -> AES-ECB -> gunzip。解密失败时原样返回（由调用方判断）。"""
        if self.version == 0:
            return text
        raw = base64.b64decode(str(text or "").replace('"', "").strip())
        return gzip.decompress(_aes_ecb_decrypt(raw,
                                                 self.aes_key.encode("utf-8"))).decode("utf-8")

    def extra_headers(self) -> list[tuple[str, str]]:
        """v3 需要额外的加密协商头；v0 没有。"""
        if self.version == 0:
            return []
        out: list[tuple[str, str]] = []
        if self.key_id:
            out.append(("Eebbk-Key-Id", self.key_id))
        if self.eebbk_key:
            out.append(("Eebbk-Key", self.eebbk_key))
        out.append(("encrypted", "encrypted"))
        return out


# ---------------------------------------------------------------- AES-ECB
def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    return _aes_ecb(data, key, encrypt=True)


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    return _aes_ecb(data, key, encrypt=False)


def _aes_ecb(data: bytes, key: bytes, *, encrypt: bool) -> bytes:
    """AES-ECB + PKCS7。优先 cryptography，缺失时用内置纯 Python 实现。"""
    if len(key) not in (16, 24, 32):
        raise ValueError("AES 密钥长度必须是 16/24/32 字节")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        return _aes_ecb_pure(data, key, encrypt=encrypt)

    if encrypt:
        padder = PKCS7(algorithms.AES.block_size).padder()
        buf = padder.update(data) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(buf) + enc.finalize()
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    buf = dec.update(data) + dec.finalize()
    unpadder = PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(buf) + unpadder.finalize()


def _aes_ecb_pure(data: bytes, key: bytes, *, encrypt: bool) -> bytes:
    """纯标准库 AES-ECB（无 cryptography 时的兜底）。

    只支持 128/192/256 位密钥的标准实现；数据量很小（请求体通常 < 32 KB），
    纯 Python 也能在毫秒级完成。
    """
    if encrypt:
        pad = 16 - (len(data) % 16)
        data = data + bytes([pad]) * pad
    if len(data) % 16:
        raise ValueError("AES 数据长度必须是 16 的倍数")
    rk = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), 16):
        block = data[off:off + 16]
        out += _encrypt_block(block, rk) if encrypt else _decrypt_block(block, rk)
    if not encrypt:
        pad = out[-1]
        if 1 <= pad <= 16:
            out = out[:-pad]
    return bytes(out)


# --- 纯 Python AES 基础表（FIPS-197）---
_SBOX: list[int] = []
_INV_SBOX: list[int] = []


def _init_tables() -> None:
    if _SBOX:
        return
    p = q = 1
    sbox = [0] * 256
    while True:
        # p 乘 3，q 除 3
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) \
            ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _SBOX.extend(sbox)
    _INV_SBOX.extend(inv)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _expand_key(key: bytes) -> list[list[int]]:
    _init_tables()
    # 必须先按字节数校验：len//4 会把 17 字节悄悄当成 16 字节用，
    # 结果是"用错的密钥加密"，而不是报错 —— 这类静默错误最难排查。
    if len(key) not in (16, 24, 32):
        raise ValueError("AES 密钥长度必须是 16/24/32 字节")
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(nr + 1)]


def _encrypt_block(block: bytes, rk: list[list[int]]) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[0])]
    nr = len(rk) - 1
    for rnd in range(1, nr + 1):
        s = [_SBOX[b] for b in s]
        # ShiftRows（列主序）
        s = [s[0], s[5], s[10], s[15],
             s[4], s[9], s[14], s[3],
             s[8], s[13], s[2], s[7],
             s[12], s[1], s[6], s[11]]
        if rnd != nr:
            ns = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                ns += [
                    _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3],
                    a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3],
                    a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3),
                    _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2),
                ]
            s = ns
        s = [b ^ k for b, k in zip(s, rk[rnd])]
    return bytes(s)


def _decrypt_block(block: bytes, rk: list[list[int]]) -> bytes:
    nr = len(rk) - 1
    s = [b ^ k for b, k in zip(block, rk[nr])]
    for rnd in range(nr - 1, -1, -1):
        s = [s[0], s[13], s[10], s[7],
             s[4], s[1], s[14], s[11],
             s[8], s[5], s[2], s[15],
             s[12], s[9], s[6], s[3]]
        s = [_INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        if rnd:
            ns = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                ns += [
                    _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                    _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                    _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                    _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14),
                ]
            s = ns
    return bytes(s)


# ---------------------------------------------------------------- 签名信封
def _json_escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def build_base_request_param(acct: HttpAccount, now: float | None = None) -> str:
    """构造 BRP 明文。

    **字段顺序固定**：服务端按同一份明文重算签名，顺序变了就验不过。
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
    rid = uuid.uuid4().hex
    if not acct.bind_number:
        # 未绑定设备时（部分只读接口）不带 deviceId/token
        return (f'{{"imFag":"1","appId":"2","program":"watch","timestamp":"{ts}",'
                f'"registId":"{acct.regist_id}","accountId":"{_json_escape(acct.account_id)}",'
                f'"mac":"unkown","requestId":"{rid}"}}')
    return (f'{{"imFag":"1","appId":"2","program":"watch","timestamp":"{ts}",'
            f'"registId":"{acct.regist_id}","deviceId":"{_json_escape(acct.bind_number)}",'
            f'"token":"{_json_escape(acct.chip_id)}","accountId":"{_json_escape(acct.account_id)}",'
            f'"mac":"unkown","requestId":"{rid}"}}')


def eebbk_sign(uri: str, raw_brp: str, body: str, aes_key: str) -> str:
    """Eebbk-Sign = MD5(uri + BRP明文 + body明文 + aes_key)，大写十六进制。"""
    h = hashlib.md5()
    h.update(str(uri).encode("utf-8"))
    h.update(str(raw_brp).encode("utf-8"))
    h.update(str(body).encode("utf-8"))
    h.update(str(aes_key).encode("utf-8"))
    return h.hexdigest().upper()


def build_signed_request(acct: HttpAccount, method: str, url: str,
                         body: str = "") -> tuple[dict[str, str], str]:
    """返回 (请求头, 要发送的 body 字符串)。"""
    crypto = ImooCrypto(acct.encrypt_key)
    raw_brp = build_base_request_param(acct)
    is_get = method.upper() == "GET"

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Base-Request-Param": crypto.encrypt_header(raw_brp),
        "Eebbk-Sign": eebbk_sign(url, raw_brp, "" if is_get else body, crypto.aes_key),
        "Accept-Language": "zh-CN",
        "User-Agent": "okhttp/3.12.0",
        "model": acct.inner_model,
        "dataCenterCode": acct.data_center or "CN_BJ",
    }
    if not is_get:
        extra = crypto.extra_headers()
        for k, v in extra:
            headers[k] = v
        if extra:
            headers["content-encoding"] = "gzip"
            headers["Accept-Encoding"] = "gzip"
    if acct.soft_version:
        headers["Version"] = f"W_{acct.soft_version}"
    if acct.grey:
        headers["Grey"] = acct.grey

    out_body = "" if is_get else crypto.encrypt_body(body)
    return headers, out_body


def dumps_compact(obj) -> str:
    """与 App 一致的紧凑 JSON（无空格），签名对这段文本计算。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
