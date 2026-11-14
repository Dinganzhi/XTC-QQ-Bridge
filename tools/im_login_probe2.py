#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位登录帧的正确发送方式。

假设
----
App 抓包里握手之后的所有业务帧都是 `cmd=0x20(32)` + `tag10`，
而 32 不在 @CommandValue 表里，所以它应是传输层包装：

    cmd=32, tag10 = 编码后的 [ 业务命令 + 业务 TLV ]

本脚本把同一份登录载荷用多种编码塞进 tag10，看哪种能换来 `cmd=4`（登录应答）。

观察到的线索
------------
抓包里 tag10 的长度是 54 字节，**不是 16 的倍数**，
所以内层不是"带 PKCS#7 填充的 AES-ECB/CBC"。
更像长度不变的流式处理（XOR 密钥流 / CTR），或是先压缩再加密。

用法
----
    python3 tools/im_login_probe2.py --brp-from-creds
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import ssl
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST, PORT = "im3.okii.com", 8002
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
    v, sh = 0, 0
    while i < len(buf):
        b = buf[i]; i += 1
        v |= (b & 0x7F) << sh
        if not (b & 0x80):
            return v, i
        sh += 7
    return None, i


def tlv(tag: int, val: bytes) -> bytes:
    return enc_varint(tag) + enc_varint(len(val)) + val


def tlv_int(tag: int, v: int) -> bytes:
    return tlv(tag, v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big"))


def frame(cmd: int, payload: bytes) -> bytes:
    return bytes([0xA0, cmd]) + enc_varint(len(payload)) + payload


def parse_tlvs(payload: bytes):
    out, i = [], 0
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


def read_all(tls, limit=16384, idle=2.0):
    """读到超时为止，尽量拿全。"""
    buf = b""
    tls.settimeout(idle)
    try:
        while len(buf) < limit:
            c = tls.recv(65536)
            if not c:
                break
            buf += c
    except Exception:
        pass
    return buf


def aes_ecb(data: bytes, key: bytes) -> bytes:
    pad = 16 - len(data) % 16
    d = data + bytes([pad]) * pad
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(d) + e.finalize()


def aes_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    pad = 16 - len(data) % 16
    d = data + bytes([pad]) * pad
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(d) + e.finalize()


def aes_ctr(data: bytes, key: bytes, nonce: bytes) -> bytes:
    e = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return e.update(data) + e.finalize()


def xor_key(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


class Sess:
    def __init__(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.tls = ctx.wrap_socket(
            socket.create_connection((HOST, PORT), timeout=10), server_hostname=HOST)
        self.rid = 5000

    def handshake(self):
        self.rid += 1
        self.tls.sendall(frame(22, tlv_int(1, self.rid)))
        buf = read_all(self.tls, 4096, 3)
        pem = mod = exp = None
        for s in (3, 2):
            for tag, val in parse_tlvs(buf[s:]):
                if tag == 10 and pem is None:
                    pem = val.decode("utf-8", "replace")
                elif tag == 11 and mod is None:
                    mod = val
                elif tag == 12 and exp is None:
                    exp = int.from_bytes(val, "big")
        if pem:
            b = pem.replace("-----BEGIN PUBLIC KEY-----", "").replace(
                "-----END PUBLIC KEY-----", "").strip()
            pub = serialization.load_der_public_key(base64.b64decode("".join(b.split())))
        else:
            pub = rsa.RSAPublicNumbers(exp, int.from_bytes(mod, "big")).public_key()
        self.rid += 1
        enc = pub.encrypt(AES_KEY, padding.PKCS1v15())
        self.tls.sendall(frame(33, tlv_int(1, self.rid) + tlv(11, enc) + tlv(10, b"\x01")))
        return read_all(self.tls, 256, 3)

    def send(self, data: bytes):
        self.tls.sendall(data)
        return read_all(self.tls, 16384, 3)


def build_login_tlvs(brp: str, rid: int) -> bytes:
    now = int(time.time())
    return b"".join([
        tlv_int(1, rid),
        tlv_int(10, 0),
        tlv(11, b""),
        tlv_int(12, 1),
        tlv(17, b""),
        tlv(18, b"1.0.0"),
        tlv_int(19, 0),
        tlv_int(20, 1),
        tlv_int(21, now),
        tlv(24, brp.encode()),
        tlv(26, b""),
    ])


def summarize(buf: bytes, label: str):
    cmds = []
    i = 0
    while i < len(buf):
        if buf[i] != 0xA0:
            i += 1
            continue
        if i + 1 >= len(buf):
            break
        cmd = buf[i + 1]
        ln, j = dec_varint(buf, i + 2)
        if ln is None or j + ln > len(buf):
            break
        cmds.append((cmd, buf[j:j + ln]))
        i = j + ln
    if not cmds:
        print(f"  {label:34} -> {len(buf)}B 非帧: {buf[:32].hex()}")
        return
    parts = []
    for cmd, pl in cmds:
        tl = parse_tlvs(pl)
        code = None
        for t, v in tl:
            if t == 2 and 0 < len(v) <= 4:
                code = int.from_bytes(v, "big")
        parts.append(f"cmd={cmd}({cmd:#04x}) len={len(pl)}" + (f" code={code}" if code else ""))
    print(f"  {label:34} -> {len(buf):5d}B  " + " | ".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brp")
    ap.add_argument("--brp-from-creds", action="store_true")
    args = ap.parse_args()

    brp = args.brp
    if not brp and args.brp_from_creds:
        c = json.loads((Path.home() / ".config" / "xtc-bridge" /
                        "imoo-credentials.json").read_text("utf-8"))
        brp = json.dumps({
            "accountId": c.get("mobile_id", ""), "appId": "0",
            "deviceId": c.get("bind_number", ""), "imFlag": "1",
            "mac": c.get("bind_number", ""), "machineId": c.get("bind_number", ""),
            "program": "android", "registId": 0,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "token": c.get("chip_id", ""),
        }, separators=(",", ":"), ensure_ascii=False)
    if not brp:
        print("[!] 需要 --brp 或 --brp-from-creds")
        return 1
    print(f"[*] BRP {len(brp)} 字节\n")

    s = Sess()
    hs = s.handshake()
    print(f"[+] 握手应答 {hs.hex()}\n")

    rid = 5100
    inner = build_login_tlvs(brp, rid)
    # 业务层可能的封装：命令号 + TLV
    biz = enc_varint(3) + inner
    biz_nocmd = inner

    cases = [
        ("cmd3 直发（对照，已知不行）", frame(3, inner)),
        ("cmd32 tag10=明文[T,TLV]", frame(32, tlv(10, biz))),
        ("cmd32 tag10=明文[TLV]", frame(32, tlv(10, biz_nocmd))),
        ("cmd32 tag10=ECB[T,TLV]", frame(32, tlv(10, aes_ecb(biz, AES_KEY)))),
        ("cmd32 tag10=CBC0[T,TLV]", frame(32, tlv(10, aes_cbc(biz, AES_KEY, b"\x00" * 16)))),
        ("cmd32 tag10=CTR0[T,TLV]", frame(32, tlv(10, aes_ctr(biz, AES_KEY, b"\x00" * 16)))),
        ("cmd32 tag10=XOR[AESKEY]", frame(32, tlv(10, xor_key(biz, AES_KEY)))),
        ("cmd32 tag10=ECB[TLV]", frame(32, tlv(10, aes_ecb(biz_nocmd, AES_KEY)))),
    ]

    print("=== 各种发送方式 ===")
    for name, data in cases:
        resp = s.send(data)
        summarize(resp, name)
        s.rid += 1
        inner = build_login_tlvs(brp, rid + 100)
        biz = enc_varint(3) + inner
        biz_nocmd = inner
        time.sleep(0.3)

    s.tls.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
