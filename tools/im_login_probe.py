#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IM 登录帧探测（命令 3）。

前提：握手已自建成功（cmd22 取公钥 -> 自己生成 AES 密钥 -> cmd33 上报 -> 200）。

LoginRequestEntity 的字段（脱壳代码）：
    @TagValue(1)  int    RID
    @TagValue(10) long   registId
    @TagValue(11) String registToken
    @TagValue(12) long   sdkVersion
    @TagValue(17) String deviceToken
    @TagValue(18) String imSdkVersionName
    @TagValue(19) int    pushType
    @TagValue(20) int    platform
    @TagValue(21) long   timestamp
    @TagValue(24) String baseRequestParam   <- 就是 BRP JSON
    @TagValue(26) String deviceId

登录成功后服务端回命令 4（LoginResponseEntity），里面带 registId / registToken，
之后才能发消息（命令 9 / 40）。

用法
----
    python3 tools/im_login_probe.py --brp '<BRP JSON 字符串>'
    python3 tools/im_login_probe.py --brp-from-creds
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


def tlv_int(tag: int, v: int) -> bytes:
    n = max(1, (v.bit_length() + 7) // 8)
    return tlv(tag, v.to_bytes(n, "big"))


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


def read_resp(tls, limit=8192):
    buf = b""
    try:
        while len(buf) < limit:
            c = tls.recv(65536)
            if not c:
                break
            buf += c
    except Exception:
        pass
    return buf


def dump_resp(buf: bytes, label: str):
    print(f"[<] {label}: {len(buf)} 字节  {buf[:80].hex()}")
    starts = [3, 2, 0]
    for s in starts:
        if len(buf) <= s:
            continue
        tlvs = parse_tlvs(buf[s:])
        if not tlvs:
            continue
        print(f"    (从偏移 {s} 起解析)")
        for tag, val in tlvs:
            if len(val) <= 8:
                print(f"      tag{tag} = {int.from_bytes(val,'big')} ({val.hex()})")
            elif tag in (10, 11, 24) and all(32 <= b < 127 for b in val[:16]):
                print(f"      tag{tag} = [{len(val)}B] {val[:70].decode('utf-8','replace')}")
            else:
                print(f"      tag{tag} = [{len(val)}B] {val[:32].hex()}")
        break


def main() -> int:
    ap = argparse.ArgumentParser(description="IM 登录帧探测")
    ap.add_argument("--brp", help="BRP JSON 字符串")
    ap.add_argument("--brp-from-creds", action="store_true",
                    help="从 ~/.config/xtc-bridge/imoo-credentials.json 组装")
    ap.add_argument("--rid", type=int, default=4001)
    args = ap.parse_args()

    brp_text = args.brp
    if not brp_text and args.brp_from_creds:
        cred = json.loads((Path.home() / ".config" / "xtc-bridge" /
                           "imoo-credentials.json").read_text("utf-8"))
        brp_text = json.dumps({
            "accountId": cred.get("mobile_id", ""),
            "appId": "0",
            "deviceId": cred.get("bind_number", ""),
            "imFlag": "1",
            "mac": cred.get("bind_number", ""),
            "machineId": cred.get("bind_number", ""),
            "program": "android",
            "registId": 0,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "token": cred.get("chip_id", ""),
        }, separators=(",", ":"), ensure_ascii=False)
    if not brp_text:
        print("[!] 需要 --brp 或 --brp-from-creds")
        return 1
    print(f"[*] BRP 长度 {len(brp_text)}")

    # --- 握手 ---
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((HOST, PORT), timeout=10)
    tls = ctx.wrap_socket(raw, server_hostname=HOST)
    tls.settimeout(8)

    rid = args.rid
    tls.sendall(frame(22, tlv_int(1, rid)))
    buf = read_resp(tls, 4096)
    pem = modulus = exponent = None
    for s in (3, 2):
        for tag, val in parse_tlvs(buf[s:]):
            if tag == 10 and pem is None:
                pem = val.decode("utf-8", "replace")
            elif tag == 11 and modulus is None:
                modulus = val
            elif tag == 12 and exponent is None:
                exponent = int.from_bytes(val, "big")
    if pem:
        b = pem.replace("-----BEGIN PUBLIC KEY-----", "").replace(
            "-----END PUBLIC KEY-----", "").strip()
        pub = serialization.load_der_public_key(base64.b64decode("".join(b.split())))
    else:
        pub = rsa.RSAPublicNumbers(exponent, int.from_bytes(modulus, "big")).public_key()
    print(f"[+] 公钥 {pub.key_size} 位")

    rid += 1
    enc = pub.encrypt(AES_KEY, padding.PKCS1v15())
    tls.sendall(frame(33, tlv_int(1, rid) + tlv(11, enc) + tlv(10, b"\x01")))
    buf = read_resp(tls, 256)
    print(f"[+] EncryptSet 应答 {buf.hex()}")

    # --- 登录帧 ---
    rid += 1
    now_s = int(time.time())
    payload = b"".join([
        tlv_int(1, rid),
        tlv_int(10, 0),                    # registId
        tlv(11, b""),                      # registToken（先空）
        tlv_int(12, 1),                    # sdkVersion
        tlv(17, b""),                      # deviceToken（先空）
        tlv(18, b"1.0.0"),                 # imSdkVersionName
        tlv_int(19, 0),                    # pushType
        tlv_int(20, 1),                    # platform
        tlv_int(21, now_s),                # timestamp
        tlv(24, brp_text.encode("utf-8")), # baseRequestParam
        tlv(26, b""),                      # deviceId（先空）
    ])
    f = frame(3, payload)
    print(f"[>] 登录帧 {len(f)} 字节: {f[:40].hex()}...")
    tls.sendall(f)
    resp = read_resp(tls, 8192)
    dump_resp(resp, "登录应答")
    tls.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
