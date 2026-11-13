#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 SSO 各登录版本，找出哪个接口返回 ticket。

APK 反编译结果：
    com/xtc/account/service/Gibraltar.java:24
        mobileAccount.setToken(sSOLoginResult.getTicket());
说明 BRP 里的 token 就是 SSO 登录返回的 ticket。
loginV2 只返回 sign，本脚本试 loginV3 / login2 等看是否有 ticket。

用法：python3 tools/probe_login_ticket.py
凭据从 config.yaml 读，终端只显示字段名与长度，不打印明文。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import yaml  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

HOST = "https://account.okii.com"
SECRET = "LKyF8jThHbZfUeTP"          # appId=11 的 SSO 密钥
MAC = "64ba24f2457d405dbf40346f96d80934"


def sign_of(url: str, params: dict) -> str:
    """SSO 信封签名：md5(url + 排序后 k=v 用 ## 连接 + secret)，小写。"""
    kv = sorted([f"{k}={v}" for k, v in params.items()], key=str.lower)
    return hashlib.md5((url + "##".join(kv) + SECRET).encode()).hexdigest().lower()


def aes(k: bytes, d: bytes, enc: bool = True) -> bytes:
    """AES-128-ECB + PKCS5 填充。"""
    if enc:
        pad = 16 - len(d) % 16
        d += bytes([pad]) * pad
        c = Cipher(algorithms.AES(k), modes.ECB()).encryptor()
    else:
        c = Cipher(algorithms.AES(k), modes.ECB()).decryptor()
    o = c.update(d) + c.finalize()
    return o if enc else (o[:-o[-1]] if o else o)


def post(path: str, params: dict, key: bytes | None = None, pub=None) -> dict:
    p = dict(params)
    p["sign"] = sign_of(HOST + path, p)
    if key is not None:
        body = base64.b64encode(aes(key, json.dumps(p, separators=(",", ":")).encode()))
        ek = base64.b64encode(pub.encrypt(key, padding.PKCS1v15()))
        hdr = {"Content-Type": "application/x-www-form-urlencoded", "Charset": "utf-8",
               "EncryptType": "1", "EncryptKey": ek.decode(), "App-Brand": "xtc"}
    else:
        body = urllib.parse.urlencode(p).encode()
        hdr = {"Content-Type": "application/x-www-form-urlencoded", "Charset": "utf-8",
               "App-Brand": "xtc"}
    req = urllib.request.Request(HOST + path, data=body, method="POST", headers=hdr)
    raw = urllib.request.urlopen(req, timeout=20).read().decode().strip()
    if raw.startswith('"'):
        raw = json.loads(raw)
    if key is not None:
        return json.loads(aes(key, base64.b64decode(raw), enc=False).decode())
    return json.loads(raw)


def ts() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def main() -> int:
    cfg = yaml.safe_load(open(_ROOT / "config.yaml", encoding="utf-8"))
    login = cfg["xiaotiancai"]["login"]
    phone, pwd = login["phone"], login["password"]

    r = post("/app/rsakey", {"appId": "11", "mac": MAC, "timestamp": ts()})
    pub = serialization.load_der_public_key(base64.b64decode(r["data"]))
    print(f"rsakey: {r['code']}\n")

    for path in ("/app/loginV3", "/app/login2", "/app/loginV2"):
        try:
            key = os.urandom(8).hex()[:16].encode()
            params = {"userName": phone, "areaCode": "86", "password": pwd,
                      "appId": "11", "mac": MAC, "timestamp": ts(),
                      "days": "-1", "deviceId": os.urandom(8).hex()}
            rr = post(path, params, key=key, pub=pub)
            print(f"=== {path}  code={rr.get('code')}  desc={rr.get('desc')}")
            d = rr.get("data")
            if d:
                inner = json.loads(d) if isinstance(d, str) else d
                print(f"    字段: {list(inner.keys())}")
                for kk in ("ticket", "userToken", "sign", "serviceData"):
                    if kk in inner:
                        v = str(inner[kk])
                        note = " ← ticket!" if kk == "ticket" and v else ""
                        print(f"    {kk:12} = <{len(v)} 位> {v[:10]}…{note}")
            print()
        except Exception as e:
            print(f"=== {path}  -> {type(e).__name__}: {str(e)[:100]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
