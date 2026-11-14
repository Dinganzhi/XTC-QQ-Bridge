#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用「16 字节密钥循环异或」加密登录帧，验证能否登录成功。

依据
----
同一个请求用两把不同 AES 密钥握手，服务端响应不同 -> 内层用我们的密钥加密。
把 keyA 的响应按 `ct XOR key` 解开，得到：

    24 08 | 01 02 1b 59 | 02 02 77 4e
            tag1 = 0x1b59 = 7001   <- 正是我们发的 RID
    （解出的密钥流 00 11 22 ... 99 与 keyA 逐字节相同）

所以在没有更强证据前，先按「循环异或」试通，再回头验证。

本脚本每个用例都用独立连接，避免一次断开污染后续。

用法
----
    python3 tools/im_login_xor.py --brp-from-creds
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

HOST, PORT = "im3.okii.com", 8002
KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


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


def read_all(tls, limit=32768, idle=2.5):
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


def parse_frames(buf: bytes):
    out, i = [], 0
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
        out.append((cmd, buf[j:j + ln]))
        i = j + ln
    return out


def xor(data: bytes) -> bytes:
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(data))


class Conn:
    def __init__(self, key: bytes = KEY):
        self.key = key
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.tls = ctx.wrap_socket(
            socket.create_connection((HOST, PORT), timeout=10), server_hostname=HOST)
        self.rid = 8000

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
        enc = pub.encrypt(self.key, padding.PKCS1v15())
        self.tls.sendall(frame(33, tlv_int(1, self.rid) + tlv(11, enc) + tlv(10, b"\x01")))
        return read_all(self.tls, 256, 3)

    def send(self, data: bytes):
        self.tls.sendall(data)
        return read_all(self.tls)

    def close(self):
        try:
            self.tls.close()
        except Exception:
            pass


def login_inner(brp: str, rid: int) -> bytes:
    return b"".join([
        tlv_int(1, rid), tlv_int(10, 0), tlv(11, b""), tlv_int(12, 1),
        tlv(17, b""), tlv(18, b"1.0.0"), tlv_int(19, 0), tlv_int(20, 1),
        tlv_int(21, int(time.time())), tlv(24, brp.encode()), tlv(26, b""),
    ])


def try_case(name: str, build, brp: str) -> bytes:
    """一次独立连接跑一个用例。"""
    c = Conn()
    hs = c.handshake()
    ok = hs.endswith(b"\x00\xc8")
    rid = 8100
    data = build(brp, rid)
    resp = c.send(data)
    c.close()
    # 解出响应
    desc = []
    for cmd, pl in parse_frames(resp):
        tl = parse_tlvs(pl)
        if len(tl) == 1 and tl[0][0] == 10 and tl[0][1]:
            dec = xor(tl[0][1])
            desc.append(f"cmd={cmd} tag10→{dec.hex()}")
        else:
            desc.append(f"cmd={cmd} {pl[:24].hex()}")
    print(f"  {name:32} 握手={'OK' if ok else 'BAD'} 响应{len(resp):5d}B  " + " | ".join(desc))
    return resp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brp")
    ap.add_argument("--brp-from-creds", action="store_true")
    args = ap.parse_args()

    brp = args.brp
    if not brp and args.brp_from_creds:
        p = Path.home() / ".config" / "xtc-bridge" / "imoo-credentials.json"
        c = json.loads(p.read_text("utf-8"))
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

    # 用例：tag10 里放 [命令号 + TLV]，整体循环异或
    def case_cmd_tlv(brp, rid):
        inner = enc_varint(3) + login_inner(brp, rid)
        return frame(32, tlv(10, xor(inner)))

    def case_tlv_only(brp, rid):
        inner = login_inner(brp, rid)
        return frame(32, tlv(10, xor(inner)))

    def case_cmd_tlv_plain(brp, rid):
        inner = enc_varint(3) + login_inner(brp, rid)
        return frame(32, tlv(10, inner))

    def case_cmd_direct_enc(brp, rid):
        inner = login_inner(brp, rid)
        return frame(3, tlv(10, xor(inner)))

    def case_cmd_direct_plain(brp, rid):
        return frame(3, login_inner(brp, rid))

    print("=== 登录发送方式（每例独立连接）===")
    try_case("cmd32 tag10=XOR[T,TLV]", case_cmd_tlv, brp)
    try_case("cmd32 tag10=XOR[TLV]", case_tlv_only, brp)
    try_case("cmd32 tag10=明文[T,TLV]", case_cmd_tlv_plain, brp)
    try_case("cmd3 tag10=XOR[TLV]", case_cmd_direct_enc, brp)
    try_case("cmd3 直发明文", case_cmd_direct_plain, brp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
