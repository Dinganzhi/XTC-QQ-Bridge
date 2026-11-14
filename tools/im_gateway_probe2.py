#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接连 IM 网关，探命令 22（取服务端 RSA 公钥）。

背景
----
IM 通道是 im3.okii.com:8002 上的 TLS 1.3，里面跑自定义帧：

    帧 = a0 | 命令号(1B) | varint(载荷长度) | 载荷
    载荷 = 若干 TLV，TLV = tag(varint) | len(varint) | value
    整数类型的 value 是**大端原始字节**（不是 varint）

已知命令号（来自脱壳代码的 @CommandValue）：
    22 (0x16)  PublicKeyRequestEntity   —— 请求服务端 RSA 公钥
    33 (0x21)  EncryptSetRequestEntity  —— 上报本次连接的 AES 密钥
    32 (0x20)  —— 数据帧，真正内容在 tag10
应答命令号 = 请求命令号 + 1（实测 33 -> 34）

为什么先试命令 22
------------------
要"自己发消息"就必须自己生成 AES 会话密钥，再用**服务端真公钥**把它加密上报。
所以第一步是拿到服务端公钥。这一步不需要客户端，也不需要证书替换。

用法
----
    python3 tools/im_gateway_probe2.py
    python3 tools/im_gateway_probe2.py --cmd 22 --rid 1001
"""
from __future__ import annotations

import argparse
import socket
import ssl
import sys

HOST = "im3.okii.com"
PORT = 8002


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


def dec_varint(buf: bytes, i: int) -> tuple[int | None, int]:
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


def build_frame(cmd: int, tlvs: list[tuple[int, bytes]]) -> bytes:
    """按 a0|cmd|varint(len)|TLV 组帧。"""
    payload = bytearray()
    for tag, val in tlvs:
        payload += enc_varint(tag)
        payload += enc_varint(len(val))
        payload += val
    return bytes([0xA0, cmd]) + enc_varint(len(payload)) + bytes(payload)


def parse_frames(data: bytes) -> list[tuple[int, bytes]]:
    out = []
    i = 0
    while i < len(data):
        if data[i] != 0xA0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        cmd = data[i + 1]
        ln, j = dec_varint(data, i + 2)
        if ln is None or j + ln > len(data):
            break
        out.append((cmd, data[j:j + ln]))
        i = j + ln
    return out


def parse_tlvs(payload: bytes) -> list[tuple[int, bytes]]:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="IM 网关命令探测")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--cmd", type=int, default=22, help="命令号，默认 22（取公钥）")
    ap.add_argument("--rid", type=int, default=1001)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except Exception:
        pass

    print(f"[*] 连接 {args.host}:{args.port} …")
    raw = socket.create_connection((args.host, args.port), timeout=args.timeout)
    tls = ctx.wrap_socket(raw, server_hostname=args.host)
    print(f"[+] TLS 建立: {tls.version()} / {tls.cipher()}")

    # RID 是整数，按大端原始字节放（与实测一致：1001 -> 03 e9）
    rid_bytes = args.rid.to_bytes(max(1, (args.rid.bit_length() + 7) // 8), "big")
    frame = build_frame(args.cmd, [(1, rid_bytes)])
    print(f"[>] 发送 cmd={args.cmd} 帧 {len(frame)} 字节: {frame.hex()}")
    tls.sendall(frame)

    tls.settimeout(args.timeout)
    total = b""
    try:
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            total += chunk
            if len(total) > 8192:
                break
    except (socket.timeout, ssl.SSLError, OSError) as e:
        print(f"[!] 读结束: {type(e).__name__}")

    print(f"[<] 收到 {len(total)} 字节: {total[:200].hex()}")
    frames = parse_frames(total)
    print(f"[i] 解出 {len(frames)} 帧")
    for cmd, pl in frames:
        tlvs = parse_tlvs(pl)
        print(f"    cmd={cmd} ({cmd:#04x}) 载荷 {len(pl)}B")
        for tag, val in tlvs:
            if len(val) <= 8:
                print(f"      tag{tag} = {int.from_bytes(val,'big')} ({val.hex()})")
            else:
                print(f"      tag{tag} = [{len(val)}B] {val[:40].hex()}")
    tls.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
