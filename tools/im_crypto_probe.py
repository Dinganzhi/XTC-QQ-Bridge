#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定 IM 内层加密：响应用不用我们上报的会话密钥。

方法
----
同样的请求，分别用**两把不同的 AES 密钥**做握手，比对服务端响应：

  响应相同 -> 没用我们的密钥（内层不加密，或用了别的密钥）
  响应不同 -> 用了我们的密钥，于是可以按候选算法逐个试解

同时每个用例都用**独立连接**，避免一次断开污染后续。

用法
----
    python3 tools/im_crypto_probe.py --brp-from-creds
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


def aes_ecb_raw(key: bytes, data: bytes, enc=True) -> bytes:
    c = Cipher(algorithms.AES(key), modes.ECB())
    f = c.encryptor() if enc else c.decryptor()
    return f.update(data) + f.finalize()


def ctr_keystream(key: bytes, n: int, start=0) -> bytes:
    ks = b""
    ctr = start
    while len(ks) < n:
        ks += aes_ecb_raw(key, ctr.to_bytes(16, "big"), True)
        ctr += 1
    return ks[:n]


def xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


class Conn:
    def __init__(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.tls = ctx.wrap_socket(
            socket.create_connection((HOST, PORT), timeout=10), server_hostname=HOST)
        self.rid = 6000

    def handshake(self, key: bytes):
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
        enc = pub.encrypt(key, padding.PKCS1v15())
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


def login_payload(brp: str, rid: int) -> bytes:
    return b"".join([
        tlv_int(1, rid), tlv_int(10, 0), tlv(11, b""), tlv_int(12, 1),
        tlv(17, b""), tlv(18, b"1.0.0"), tlv_int(19, 0), tlv_int(20, 1),
        tlv_int(21, int(time.time())), tlv(24, brp.encode()), tlv(26, b""),
    ])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brp")
    ap.add_argument("--brp-from-creds", action="store_true")
    args = ap.parse_args()

    brp = args.brp
    if not brp and args.brp_from_creds:
        c = json.loads((Path.home() / ".config" / "xtc-bridge" /
                        "imoo_credentials.json").read_text("utf-8")) \
            if (Path.home() / ".config" / "xtc-bridge" / "imoo_credentials.json").exists() \
            else json.loads((Path.home() / ".config" / "xtc-bridge" /
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

    KA = bytes.fromhex("00112233445566778899aabbccddeeff")
    KB = bytes.fromhex("ffeeddccbbaa99887766554433221100")

    print("=== 同一请求，两把不同密钥 ===")
    results = {}
    for name, key in (("keyA", KA), ("keyB", KB)):
        c = Conn()
        hs = c.handshake(key)
        resp = c.send(frame(3, login_payload(brp, 7001)))
        c.close()
        results[name] = resp
        tl = []
        for cmd, pl in parse_frames(resp):
            tl.append((cmd, pl))
        print(f"  {name} 握手={hs.hex()}")
        print(f"       登录应答 {len(resp)}B: {resp.hex()}")
        for cmd, pl in tl:
            print(f"         cmd={cmd} payload={pl.hex()}")

    same = results["keyA"] == results["keyB"]
    print(f"\n[判定] 两次响应{'相同' if same else '不同'} -> "
          f"{'内层未用我们的密钥' if same else '内层用了我们的密钥'}")

    # 如果是我们的密钥，逐种算法试解 tag10
    if not same:
        for name, key in (("keyA", KA),):
            resp = results[name]
            for cmd, pl in parse_frames(resp):
                for tag, val in parse_tlvs(pl):
                    if tag != 10 or not val:
                        continue
                    print(f"\n  试解 {len(val)} 字节 tag10:")
                    cands = {
                        "XOR(key)": xor(val, (key * 8)[:len(val)]),
                        "CTR cnt=0": xor(val, ctr_keystream(key, len(val), 0)),
                        "CTR cnt=1": xor(val, ctr_keystream(key, len(val), 1)),
                    }
                    for cn, pt in cands.items():
                        ok = sum(1 for b in pt if 32 <= b < 127)
                        print(f"    {cn:12} {pt.hex():40} printable={ok}/{len(pt)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
