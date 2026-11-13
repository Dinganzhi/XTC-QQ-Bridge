#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TLS 透明代理：解密并记录 IM 通道的原始字节。

用途
----
IM 通道（im3.okii.com:8002）是标准 TLS 1.3，里面跑的是自定义 TLV 协议，
不是 HTTP。mitmproxy 处理不了裸 TLS，所以这里写一个最小代理：

    客户端 --TLS--> 本代理 --TLS--> 真实服务器

两侧都记录解密后的明文，落盘成两个文件（c2s / s2c），便于分析帧格式。

用法
----
    python3 tools/tls_tap.py --listen 8443 --target im3.okii.com:8002

配合 adb reverse + iptables 使用：
    adb reverse tcp:8443 tcp:8443
    iptables -t nat -A OUTPUT -p tcp --dport 8002 \
             -j DNAT --to-destination 127.0.0.1:8443
"""
from __future__ import annotations

import argparse
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 证书默认放在 /tmp/mitm（临时目录，不进仓库）；也可用 --cert-dir 指定
CERT_DIR = Path("/tmp/mitm")

OUT = Path("/tmp/mitm")
_lock = threading.Lock()


def log(tag: str, data: bytes) -> None:
    """把一段明文追加写入 /tmp/mitm/<tag>.bin，同时打印十六进制摘要。"""
    if not data:
        return
    with _lock:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(OUT / f"{tag}.bin", "ab") as f:
            f.write(data)
        ts = time.strftime("%H:%M:%S")
        head = data[:64].hex()
        print(f"[{ts}] {tag} {len(data):5d}B  {head}", flush=True)


def pump(src, dst, tag: str) -> None:
    """单向搬运并记录。任一端关闭就结束。

    注意 SSLSocket 没有 flush()（只有 socket 有），所以这里不调用。
    """
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            log(tag, data)
            dst.sendall(data)
    except (ssl.SSLError, OSError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


def handle(client_sock, addr, target_host: str, target_port: int) -> None:
    print(f"[+] 新连接 {addr}", flush=True)

    # 1. 与客户端握手（用我们的证书冒充 im3.okii.com）
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(CERT_DIR / "srv.crt"), str(CERT_DIR / "srv.key"))
    sctx.set_alpn_protocols(["http/1.1"])
    try:
        client_tls = sctx.wrap_socket(client_sock, server_side=True)
    except (ssl.SSLError, OSError) as e:
        print(f"[!] 客户端 TLS 握手失败: {e}", flush=True)
        try:
            client_sock.close()
        except Exception:
            pass
        return

    # 2. 连真实服务器
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.check_hostname = False
    cctx.verify_mode = ssl.CERT_NONE
    cctx.set_alpn_protocols(["http/1.1"])
    try:
        raw = socket.create_connection((target_host, target_port), timeout=15)
        server_tls = cctx.wrap_socket(raw, server_hostname=target_host)
    except (ssl.SSLError, OSError) as e:
        print(f"[!] 连真实服务器失败: {e}", flush=True)
        client_tls.close()
        return

    print(f"[+] 双向隧道已建立 -> {target_host}:{target_port}", flush=True)
    t1 = threading.Thread(target=pump, args=(client_tls, server_tls, "c2s"), daemon=True)
    t2 = threading.Thread(target=pump, args=(server_tls, client_tls, "s2c"), daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()
    print(f"[-] 连接结束 {addr}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="TLS 透明代理（记录 IM 明文）")
    ap.add_argument("--listen", type=int, default=8443)
    ap.add_argument("--target", default="im3.okii.com:8002")
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    host, _, port_s = args.target.partition(":")
    port = int(port_s or 8002)

    for p in (CERT_DIR / "ca.crt", CERT_DIR / "srv.crt", CERT_DIR / "srv.key"):
        if not p.exists():
            print(f"[!] 缺少 {p}，先跑生成证书的步骤")
            return 1

    # 清空上次的记录
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("c2s.bin", "s2c.bin"):
        try:
            (OUT / name).unlink()
        except FileNotFoundError:
            pass

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.listen))
    srv.listen(16)
    print(f"[*] 监听 {args.bind}:{args.listen} -> {host}:{port}", flush=True)

    while True:
        cs, addr = srv.accept()
        threading.Thread(target=handle, args=(cs, addr, host, port), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
