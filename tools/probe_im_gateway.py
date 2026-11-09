# -*- coding: utf-8 -*-
"""IM 接入点协议探测脚本（只读，不发送任何账号凭据）。

用途：复现 `docs/imoo-api.md` 第 4.3 节记录的观察结果。
      这个脚本**不会**登录、不会发消息、不修改任何数据，
      只是连上去看服务器回什么，用于验证"该端口是什么协议"。

用法：
    python3 -B tools/probe_im_gateway.py                # 探测默认接入点
    python3 -B tools/probe_im_gateway.py --port 1883    # 探测备用端口

注意：这是对第三方服务的最小化连通性探测。请仅用于你自己账号相关的
      学习与研究，不要高频调用（脚本内已限制次数）。
"""
from __future__ import annotations

import argparse
import re
import socket
import sys

DEFAULT_HOST = "gw.im.okii.com"
DEFAULT_PORT = 8000
TIMEOUT = 6.0


def read_len(data: bytes, pos: int) -> tuple[int, int]:
    """读 LEB128 变长长度（与 imoo-client 的 tlv.py 一致）。"""
    value = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
    return value, pos


def parse_tlv_stream(data: bytes, start: int) -> list[tuple[int, bytes]]:
    """从 start 起尽量解析 TLV 序列，遇到不合法就停。"""
    out: list[tuple[int, bytes]] = []
    pos = start
    while pos < len(data):
        tag = data[pos]
        pos += 1
        length, pos = read_len(data, pos)
        if length < 0 or pos + length > len(data):
            break
        out.append((tag, data[pos:pos + length]))
        pos += length
    return out


def fetch_banner(host: str, port: int) -> bytes:
    """连上去发一段无害的 TLS 头，收集服务器首帧。

    发 TLS 头只是为了让服务端"认为有人在说话"从而下发握手包；
    内容本身不重要（实测发任意数据效果相同）。
    """
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        sock.sendall(b"\x16\x03\x01\x00\x50")
        sock.settimeout(3.0)
        buf = b""
        while len(buf) < 8192:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf


def describe(banner: bytes) -> None:
    print(f"  首帧长度: {len(banner)} 字节")
    if len(banner) < 7:
        print("  太短，未取得有效首帧")
        return

    print(f"  头部 7 字节: {banner[:7].hex(' ')}")
    print(f"    魔数 0x{banner[0]:02x}{banner[1]:02x}"
          f"  版本 {banner[2]}.{banner[3]}")

    items = parse_tlv_stream(banner, 7)
    if not items:
        print("  未能解析出 TLV")
        return

    print(f"  解析出 {len(items)} 个 TLV:")
    for tag, value in items:
        if tag == 10:
            m = re.search(rb"-----BEGIN PUBLIC KEY-----(.*?)-----END PUBLIC KEY-----",
                          value, re.S)
            if m:
                body = m.group(1).strip()
                # 密钥位数：优先用 cryptography（可靠），不可用时退回手算 DER。
                bits = 0
                try:
                    from cryptography.hazmat.primitives import serialization
                    text = body.decode()
                    wrapped = ("-----BEGIN PUBLIC KEY-----\n"
                               + "\n".join(text[i:i + 64] for i in range(0, len(text), 64))
                               + "\n-----END PUBLIC KEY-----\n")
                    pub = serialization.load_pem_public_key(wrapped.encode())
                    bits = pub.key_size
                except Exception:  # noqa: BLE001 没装 cryptography 时走下面的手算
                    bits = 0
                if not bits:
                    try:
                        import base64 as _b64
                        der = _b64.b64decode(body + b"=" * (-len(body) % 4))

                        def _read_len(buf: bytes, pos: int) -> tuple[int, int]:
                            first = buf[pos]
                            pos += 1
                            if first & 0x80:
                                n = first & 0x7F
                                return int.from_bytes(buf[pos:pos + n], "big"), pos + n
                            return first, pos

                        def _skip(buf: bytes, pos: int) -> int:
                            length, body_pos = _read_len(buf, pos + 1)
                            return body_pos + length

                        pos = _skip(der, 0)          # 外层 SEQUENCE
                        pos = _skip(der, pos)        # AlgorithmIdentifier
                        pos = _skip(der, pos) + 1    # BIT STRING（+1 跳过 unused-bits）
                        pos = _skip(der, pos)        # RSAPublicKey SEQUENCE
                        if der[pos] == 0x02:         # modulus INTEGER
                            mlen, _ = _read_len(der, pos + 1)
                            if mlen % 8 == 1:        # 去掉 DER 的 0x00 前导
                                mlen -= 1
                            bits = mlen * 8
                    except Exception:  # noqa: BLE001 估算失败不影响主流程
                        bits = 0
                hint = f"约 {bits} 位" if bits else "位数未知"
                print(f"    tag=10 len={len(value):3}  RSA 公钥（单行 PEM，{hint}）")
            else:
                print(f"    tag=10 len={len(value):3}  （含 BEGIN PUBLIC KEY 但未匹配）")
        elif tag == 11:
            print(f"    tag=11 len={len(value):3}  二进制块 {value[:16].hex()}...")
        elif tag == 12:
            print(f"    tag=12 len={len(value):3}  值 {value.hex()}"
                  f"（若为 010001 则是 RSA 指数 65537）")
        else:
            print(f"    tag={tag} len={len(value):3}  {value[:24].hex()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="探测小天才 IM 接入点协议")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--repeat", type=int, default=2,
                    help="重复次数，用于判断公钥/数据块是否每次相同（默认 2）")
    args = ap.parse_args()

    print(f"探测 {args.host}:{args.port}")
    banners = []
    for i in range(max(1, min(args.repeat, 5))):
        try:
            b = fetch_banner(args.host, args.port)
        except Exception as e:  # noqa: BLE001 探测脚本，失败即报告
            print(f"  第 {i+1} 次连接失败: {type(e).__name__}: {e}")
            return 1
        banners.append(b)
        print(f"\n--- 第 {i+1} 次 ---")
        describe(b)

    if len(banners) > 1:
        same = all(b == banners[0] for b in banners[1:])
        print(f"\n首帧是否每次完全相同: {same}")
        if same:
            print("  → 说明公钥与数据块都是固定的，不是逐连接的会话密钥")
        else:
            print("  → 说明存在逐连接随机成分（如会话密钥）")

    print("\n结论：该端口是自定义二进制协议，不是标准 MQTT，也不是 TLS。")
    print("详见 docs/imoo-api.md 第 4.3 节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
