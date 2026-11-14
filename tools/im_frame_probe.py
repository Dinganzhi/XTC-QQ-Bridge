#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""握手后试探后续帧格式与 AES 加密方式。

已知
----
握手已自建成功（cmd22 取公钥 -> 自己生成 AES 密钥 -> cmd33 上报 -> t2=200）。
现在要确定两件事：

  1. 握手之后的业务帧是直接发 cmd（7=心跳/3=登录/9=消息），
     还是要包在 cmd=32 的数据帧里、把真内容放进 tag10。
     （客户端抓包里只见到 cmd=0x20=32，但 32 不在 @CommandValue 表里，
       所以它很可能是传输层包装）

  2. tag10 里那层 AES 用的什么模式（ECB / CBC）与填充。

做法
----
对每种组合发一帧，看服务端回什么。回 t2=200 就是对了。

用法
----
    python3 tools/im_frame_probe.py
"""
from __future__ import annotations

import base64
import socket
import ssl
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST = "im3.okii.com"
PORT = 8002
AES_KEY = bytes.fromhex("0123456789abcdef0123456789abcdef")


def enc_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def dec_varint(buf: bytes, i: int):
    v = 0
    sh = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << sh
        if not (b & 0x80):
            return v, i
        sh += 7
    return None, i


def tlv(tag: int, val: bytes) -> bytes:
    return enc_varint(tag) + enc_varint(len(val)) + val


def frame(cmd: int, payload: bytes) -> bytes:
    return bytes([0xA0, cmd]) + enc_varint(len(payload)) + payload


def parse_tlvs(payload: bytes):
    out = []
    i = 0
    while i < len(payload):
        tag, i = dec_varint(payload, i)
        if tag is None:
            break
        ln, i = dec_varint(payload, i)
        if ln is None or i + ln > len(payload):
            break
        out.append((tag, payload[i:i + ln]))
        i += ln
    return out


def resp_code(buf: bytes):
    """从响应里取 t2（状态码）。"""
    for start in (3, 2, 0):
        if len(buf) <= start:
            continue
        tlvs = parse_tlvs(buf[start:])
        for tag, val in tlvs:
            if tag == 2 and 0 < len(val) <= 4:
                return int.from_bytes(val, "big")
    return None


def aes_ecb_enc(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    e = Cipher(algorithms.AES(AES_KEY), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()


def aes_cbc_enc(data: bytes, iv: bytes) -> bytes:
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    e = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
    return e.update(data) + e.finalize()


class Session:
    def __init__(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((HOST, PORT), timeout=8)
        self.tls = ctx.wrap_socket(raw, server_hostname=HOST)
        self.tls.settimeout(6)
        self.rid = 2000

    def _read(self, limit=4096):
        buf = b""
        try:
            while len(buf) < limit:
                c = self.tls.recv(65536)
                if not c:
                    break
                buf += c
        except Exception:
            pass
        return buf

    def handshake(self):
        self.rid += 1
        self.tls.sendall(frame(22, tlv(1, self.rid.to_bytes(2, "big"))))
        buf = self._read()
        pem = modulus = exponent = None
        for start in (3, 2):
            for tag, val in parse_tlvs(buf[start:]):
                if tag == 10 and not pem:
                    pem = val.decode("utf-8", "replace")
                elif tag == 11 and not modulus:
                    modulus = val
                elif tag == 12 and not exponent:
                    exponent = int.from_bytes(val, "big")
        if pem:
            b = pem.replace("-----BEGIN PUBLIC KEY-----", "").replace(
                "-----END PUBLIC KEY-----", "").strip()
            pub = serialization.load_der_public_key(base64.b64decode("".join(b.split())))
        else:
            pub = rsa.RSAPublicNumbers(exponent, int.from_bytes(modulus, "big")).public_key()
        enc = pub.encrypt(AES_KEY, padding.PKCS1v15())
        self.rid += 1
        self.tls.sendall(frame(33, tlv(1, self.rid.to_bytes(2, "big")) + tlv(11, enc) + tlv(10, b"\x01")))
        resp = self._read(256)
        return resp_code(resp)

    def send(self, data: bytes):
        self.tls.sendall(data)
        return self._read(1024)


def main() -> int:
    s = Session()
    code = s.handshake()
    print(f"[握手] t2={code}")
    if code != 200:
        print("握手没成功，后面没意义")
        return 1
    print()

    rid = 3001
    ridb = rid.to_bytes(2, "big")
    inner_hb = tlv(1, ridb)          # 心跳实体本体：只有 RID

    cases = [
        ("cmd7 裸心跳",            frame(7, inner_hb)),
        ("cmd7 + tag10=ECB(心跳)", frame(7, tlv(1, ridb) + tlv(10, aes_ecb_enc(inner_hb)))),
        ("cmd32 + tag10=ECB(心跳)", frame(32, tlv(10, aes_ecb_enc(inner_hb)))),
        ("cmd32 + tag10=CBC(心跳)", frame(32, tlv(10, aes_cbc_enc(inner_hb, b"\x00" * 16)))),
        ("cmd32 裸心跳",           frame(32, inner_hb)),
    ]
    for name, data in cases:
        resp = s.send(data)
        c = resp_code(resp)
        print(f"  {name:26} -> t2={c}  响应 {len(resp)}B {resp[:24].hex()}")

    s.tls.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
