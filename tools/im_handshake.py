#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IM 网关自建握手：自己生成 AES 密钥并上报。

思路
----
命令 22 可以在**无认证**的情况下拿到服务端 RSA-1024 公钥（实测）。
于是可以完全自己做这次握手，不需要客户端：

    1. cmd 22               -> 拿公钥（tag10=PEM, tag11=模数, tag12=指数）
    2. 自己生成 16 字节 AES 密钥
    3. RSA(PKCS#1 v1.5) 加密 -> 128 字节
    4. cmd 33 (EncryptSet)  -> t1=RID, t11=密文, t10=1(encryptType)
    5. 服务端回 t2=200       -> 会话建立，AES 密钥只有我们知道

这一步通了，后面的登录帧与消息帧就能用我们自己的密钥加密，
也就真正"脱离客户端"了。

用法
----
    python3 tools/im_handshake.py
    python3 tools/im_handshake.py --rid 1002
"""
from __future__ import annotations

import argparse
import base64
import socket
import ssl
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HOST = "im3.okii.com"
PORT = 8002


# ----------------------------------------------------------------- 编解码
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


def build_frame(cmd: int, tlvs) -> bytes:
    payload = bytearray()
    for tag, val in tlvs:
        payload += enc_varint(tag)
        payload += enc_varint(len(val))
        payload += val
    return bytes([0xA0, cmd]) + enc_varint(len(payload)) + bytes(payload)


def frame_bytes_raw(cmd: int, payload: bytes) -> bytes:
    """不带 a0 头的裸帧，用于首次未建会话时。"""
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


def find_tlvs(buf: bytes):
    """在整段响应里找 TLV 起点。

    实测两种响应头：
      未建会话: 37 9d 03 | TLV...
      已建会话: a0 <cmd> <varint len> | TLV...
    这里两种都试，取能完整解析的那种。
    """
    candidates = []
    for start in (3, 2):
        if len(buf) > start:
            tlvs = parse_tlvs(buf[start:])
            if tlvs:
                candidates.append((start, tlvs))
    # a0 帧
    for i in range(len(buf) - 2):
        if buf[i] == 0xA0:
            ln, j = dec_varint(buf, i + 2)
            if ln and j + ln <= len(buf):
                candidates.append((i, [("frame", buf[j:j + ln])]))
                break
    return candidates[0] if candidates else (0, [])


# ----------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="IM 网关自建握手")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--rid", type=int, default=1002)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print(f"[*] 连接 {args.host}:{args.port}")
    raw = socket.create_connection((args.host, args.port), timeout=args.timeout)
    tls = ctx.wrap_socket(raw, server_hostname=args.host)
    tls.settimeout(args.timeout)
    print(f"[+] TLS {tls.version()} / {tls.cipher()[0]}")

    # --- 1. 取公钥 ---
    tls.sendall(build_frame(22, [(1, args.rid.to_bytes(2, "big"))]))
    buf = b""
    try:
        while len(buf) < 4096:
            c = tls.recv(65536)
            if not c:
                break
            buf += c
            if len(buf) > 400:
                break
    except Exception:
        pass
    print(f"[<] cmd22 响应 {len(buf)} 字节")

    start, tlvs = find_tlvs(buf)
    pem = modulus = exponent = None
    for tag, val in tlvs:
        if tag == 10:
            pem = val.decode("utf-8", "replace")
        elif tag == 11:
            modulus = val
        elif tag == 12:
            exponent = int.from_bytes(val, "big")
    if not pem and not modulus:
        print("[!] 没解析出公钥")
        print(f"    原始: {buf[:120].hex()}")
        return 1

    if pem:
        body = pem.replace("-----BEGIN PUBLIC KEY-----", "").replace(
            "-----END PUBLIC KEY-----", "").strip()
        pub = serialization.load_der_public_key(base64.b64decode("".join(body.split())))
    else:
        n = int.from_bytes(modulus, "big")
        pub = rsa.RSAPublicNumbers(exponent, n).public_key()
    print(f"[+] 服务端公钥 {pub.key_size} 位")

    # --- 2. 自己生成 AES 密钥 ---
    aes_key = bytes.fromhex("0123456789abcdef0123456789abcdef")  # 16 字节，先固定便于调试
    print(f"[*] 我们的 AES 密钥: {aes_key.hex()}")

    # --- 3. RSA 加密并上报 ---
    enc = pub.encrypt(aes_key, padding.PKCS1v15())
    print(f"[*] RSA 密文 {len(enc)} 字节: {enc[:24].hex()}...")

    frame = build_frame(33, [(1, args.rid.to_bytes(2, "big")), (11, enc), (10, b"\x01")])
    print(f"[>] cmd33 EncryptSet {len(frame)} 字节: {frame[:16].hex()}...")
    tls.sendall(frame)

    # --- 4. 读应答 ---
    res = b""
    try:
        while True:
            c = tls.recv(65536)
            if not c:
                break
            res += c
            if len(res) > 256:
                break
    except Exception:
        pass
    print(f"[<] 应答 {len(res)} 字节: {res.hex()}")

    start, tlvs = find_tlvs(res)
    code = None
    for tag, val in tlvs:
        if tag == 2 and len(val) <= 4:
            code = int.from_bytes(val, "big")
    if code == 200:
        print("[✓] 会话建立成功（t2=200）—— 服务端接受了我们自己的 AES 密钥")
    elif code is not None:
        print(f"[!] 服务端返回 code={code}")
    else:
        print("[?] 没解出应答码")

    tls.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
