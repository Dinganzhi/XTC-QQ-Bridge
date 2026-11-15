#!/usr/bin/env python3
"""小天才 IM 会话：自建 TLS 握手 + 按配置构造登录帧。

背景见 docs/imoo-api.md 第 4.3 节。要点：

* IM 通道是 `im3.okii.com:8002` 上的标准 TLS 1.3，无证书固定；
* 帧格式 `a0 | 命令号 | varint(长度) | TLV 载荷`；
* 公钥交换另用 `0x36`（请求）/ `0x37`（应答），结构是 `魔数 | varint(长度) | 载荷`；
* 会话密钥由客户端生成，用服务端 RSA-1024 公钥加密后放在 `cmd 33` 的 `tag11`；
* 载荷用「会话密钥循环异或」加密。

本工具做两件事：

1. 自己完成握手（拿到 `code = 200`）；
2. 把**一次抓取**到的登录帧明文用当前会话密钥重新加密后发出。

服务端只认 `registToken`，不认连接来源，所以登录帧本身可以复用；
这样抓一次之后就不再需要客户端在线。

明文帧放在 `~/.config/xtc-bridge/im-frames/`，权限 600，不进仓库。
"""

from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

HOST = "im3.okii.com"
PORT = 8002

# 会话密钥必须是 16 个可打印 ASCII：服务端把密钥当字符串处理，
# 非 ASCII 字节会被替换成 U+FFFD，导致双方密钥不一致。
SESSION_KEY = b"0123456789abcdef"

MAGIC_BIZ = 0xA0
MAGIC_PUB_REQ = 0x36
MAGIC_PUB_RESP = 0x37

FRAMES_DIR = Path.home() / ".config" / "xtc-bridge" / "im-frames"
CRED_FILE = Path.home() / ".config" / "xtc-bridge" / "imoo-credentials.json"


def _load_cfg() -> dict:
    """读本地凭据文件（仓库外，权限 600）。"""
    import json as _json

    return _json.loads(CRED_FILE.read_text("utf-8"))


# ---------------------------------------------------------------- 编解码


def enc_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def dec_varint(buf: bytes, i: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    return None, i


def tlv(tag: int, value: bytes) -> bytes:
    return enc_varint(tag) + enc_varint(len(value)) + value


def tlv_int(tag: int, value: int) -> bytes:
    # 整数按大端裸字节写入，不用 varint
    if value == 0:
        return tlv(tag, b"\x00")
    width = max(1, (value.bit_length() + 7) // 8)
    return tlv(tag, value.to_bytes(width, "big"))


def parse_tlvs(buf: bytes) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytes]] = []
    i = 0
    while i < len(buf):
        tag, i2 = dec_varint(buf, i)
        if tag is None:
            break
        length, i3 = dec_varint(buf, i2)
        if length is None or i3 + length > len(buf):
            break
        out.append((tag, buf[i3 : i3 + length]))
        i = i3 + length
    return out


def build_frame(cmd: int, payload: bytes) -> bytes:
    return bytes([MAGIC_BIZ, cmd]) + enc_varint(len(payload)) + payload


def xor_key(data: bytes, key: bytes) -> bytes:
    n = len(key)
    return bytes(b ^ key[i % n] for i, b in enumerate(data))


# ---------------------------------------------------------------- 会话


@dataclass
class Response:
    cmd: int
    payload: bytes  # 已用会话密钥解密


class ImSession:
    """一条已握手的 IM 连接。"""

    def __init__(self, *, timeout: float = 10.0, log=None):
        self._log = log or (lambda *_a, **_k: None)
        self._tls = self._connect(timeout)
        self.rid = 2000

    # -- 连接与握手 ------------------------------------------------

    def _connect(self, timeout: float):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((HOST, PORT), timeout=timeout)
        tls = ctx.wrap_socket(raw, server_hostname=HOST)
        self._tls = tls
        self._fetch_and_set_key()
        return tls

    def _recv_frame(self, timeout: float = 6.0) -> bytes:
        self._tls.settimeout(timeout)
        buf = b""
        while True:
            try:
                chunk = self._tls.recv(65536)
            except (TimeoutError, ssl.SSLError):
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) < 2:
                continue
            magic = buf[0]
            offset = 2 if magic == MAGIC_BIZ else 1
            length, start = dec_varint(buf, offset)
            if length is not None and start + length <= len(buf):
                return buf[: start + length]
        return buf

    def _fetch_and_set_key(self) -> None:
        # 取公钥：0x36 | len | tag1=RID
        self._tls.sendall(bytes([MAGIC_PUB_REQ]) + enc_varint(4) + tlv(1, (1000).to_bytes(2, "big")))
        frame = self._recv_frame()
        if not frame or frame[0] != MAGIC_PUB_RESP:
            raise RuntimeError("取公钥失败：响应魔数不是 0x37")
        length, start = dec_varint(frame, 1)
        payload = frame[start : start + length]
        pem = None
        for tag, value in parse_tlvs(payload):
            if tag == 10:
                pem = value.decode()
        if not pem:
            raise RuntimeError("取公钥失败：响应里没有 PEM")
        body = "".join(
            pem.replace("-----BEGIN PUBLIC KEY-----", "").replace("-----END PUBLIC KEY-----", "").split()
        )
        pub = serialization.load_der_public_key(base64.b64decode(body))
        encrypted = pub.encrypt(SESSION_KEY, padding.PKCS1v15())
        payload = tlv_int(1, 1002) + tlv(11, encrypted) + tlv(10, b"\x01")
        self._tls.sendall(build_frame(33, payload))
        resp = self._recv_frame()
        code = self._status_code(resp)
        self._log("info", f"会话密钥协商完成，code={code}")
        if code != 200:
            raise RuntimeError(f"会话密钥协商被拒：code={code}")

    # -- 收发 ------------------------------------------------------

    @staticmethod
    def _status_code(frame: bytes) -> int | None:
        if not frame or frame[0] != MAGIC_BIZ:
            return None
        cmd = frame[1]
        length, start = dec_varint(frame, 2)
        if length is None:
            return None
        body = frame[start : start + length]
        items = parse_tlvs(body)
        # cmd 32 的载荷包一层 tag10（加密），cmd 34 等是直连 TLV
        for tag, value in items:
            if tag == 10 and value:
                plain = xor_key(value, SESSION_KEY)
                inner_cmd, i = dec_varint(plain, 0)
                inner_len, j = dec_varint(plain, i)
                if inner_len is None:
                    continue
                body = plain[j : j + inner_len]
                items = parse_tlvs(body)
                break
        for tag, value in items:
            if tag == 2 and len(value) <= 4:
                return int.from_bytes(value, "big")
        return None

    def send(self, cmd: int, payload: bytes, *, timeout: float = 6.0) -> Response | None:
        """发一条业务命令，返回解密后的响应（没有响应时返回 None）。"""
        self._tls.sendall(build_frame(cmd, payload))
        frame = self._recv_frame(timeout)
        if not frame or frame[0] != MAGIC_BIZ:
            return None
        rcmd = frame[1]
        length, start = dec_varint(frame, 2)
        if length is None:
            return None
        body = frame[start : start + length]
        for tag, value in parse_tlvs(body):
            if tag == 10 and value:
                return Response(rcmd, xor_key(value, SESSION_KEY))
        return Response(rcmd, body)

    def code_of(self, resp: Response | None) -> int | None:
        if resp is None:
            return None
        inner_cmd, i = dec_varint(resp.payload, 0)
        inner_len, j = dec_varint(resp.payload, i)
        if inner_len is None:
            return None
        for tag, value in parse_tlvs(resp.payload[j : j + inner_len]):
            if tag == 2 and len(value) <= 4:
                return int.from_bytes(value, "big")
        return None

    # -- 保活 ------------------------------------------------------

    def heartbeat(self, timeout: float = 5.0) -> bool:
        """发一次心跳。返回连接是否还活着。

        心跳应答是 7 字节、没有状态码字段（不是 200 那种），
        所以这里只判断"有没有收到数据"，不解析 code。
        """
        try:
            self.rid += 1
            self._tls.sendall(build_frame(7, tlv_int(1, self.rid) + tlv_int(2, int(time.time()))))
            frame = self._recv_frame(timeout)
            return bool(frame)
        except Exception:
            return False

    def start_keepalive(self, interval: float = 60.0) -> None:
        """后台心跳保活。

        票据大约 10 小时过期，但**会话一旦建立就不会因为票据过期而失效** ——
        只要连接不断，就不需要重新登录。长连接因此能绕开票据刷新的问题。
        """
        import threading

        if getattr(self, "_ka_thread", None) is not None:
            return
        stop = threading.Event()
        self._ka_stop = stop

        def _loop() -> None:
            while not stop.wait(interval):
                if not self.heartbeat():
                    self._log("warning", "心跳失败，连接可能已断开")
                    return

        t = threading.Thread(target=_loop, name="im-keepalive", daemon=True)
        self._ka_thread = t
        t.start()
        self._log("info", f"已启动心跳保活（每 {interval:.0f} 秒）")

    def stop_keepalive(self) -> None:
        stop = getattr(self, "_ka_stop", None)
        if stop is not None:
            stop.set()
        self._ka_thread = None

    def close(self) -> None:
        try:
            self.stop_keepalive()
        except Exception:
            pass
        try:
            self._tls.close()
        except Exception:
            pass

    # -- 登录 ------------------------------------------------------

    def login_from_config(self, cfg: dict | None = None) -> int | None:
        """按配置里的设备参数构造登录帧（不依赖任何抓包）。

        需要的字段见 `_load_cfg`。这些都是设备级常量 + 一次性取得的票据，写进配置即可。
        """
        c = cfg or _load_cfg()
        ticket = c["im_account_token"]
        brp = json.dumps(
            {
                "accountId": c["mobile_id"],
                "appId": c["im_app_id"],
                "deviceId": c["im_device_id_brp"],
                "imFlag": c["im_im_flag"],
                "mac": c["im_mac"],
                "machineId": c["im_machine_id"],
                "program": c["im_program"],
                "registId": c["im_regist_id"],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "token": ticket,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        inner = (
            tlv_int(1, 2101)
            + tlv(24, brp.encode())
            + tlv(26, c["im_device_id_tlv"].encode())
            + tlv(18, int(c["im_sdk_version_name"]).to_bytes(7, "big"))
            + tlv_int(20, int(c["im_platform"]))
            + tlv_int(10, int(c["im_regist_id"]))
            + tlv(11, b"null")
            + tlv_int(12, int(c["im_sdk_version"]))
            + tlv(21, int(time.time() * 1000).to_bytes(8, "big"))
        )
        resp = self.send(32, tlv(10, xor_key(tlv(35, inner), SESSION_KEY)))
        return self.code_of(resp)

    def send_text(self, receiver_id: int, text: str, cfg: dict | None = None) -> int | None:
        """给单个联系人发一条文本消息（`cmd 40`）。

        `receiver_id` 是对方的 `imAccountId`；发送方 `imAccountId` 取自配置
        （注意每个手机账号都不一样，填错会得到 `102400`）。
        """
        import uuid as _uuid

        c = cfg or _load_cfg()
        content = json.dumps(
            {
                "content": text,
                "hintType": -1,
                "isSensitive": False,
                "msgNewContent": "",
                "msgNewType": 0,
                "showType": 0,
                "text": text,
                "textMsgType": 0,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = (
            tlv_int(1, 100000 + int(time.time()) % 100000)
            + tlv_int(11, int(c["im_sender_account_id"]))
            + tlv_int(16, 3)
            + tlv(14, content.encode())
            + tlv(15, _uuid.uuid4().hex.encode())
            + tlv_int(13, 1)
            + tlv_int(10, receiver_id)
            + tlv_int(12, int(c["im_regist_id"]))
        )
        resp = self.send(40, payload)
        return self.code_of(resp)

    def login_from_captured(self, frame_path: Path | None = None) -> int | None:
        """用抓到的登录帧明文重新加密后发出，返回状态码。

        登录帧里带着 `registToken`，服务端只认它，所以可以长期复用。
        发送前会把里面的 `timestamp` 换成当前时间，避免被判过期。
        """
        path = frame_path or (FRAMES_DIR / "c2s_006_cmd32.bin")
        if not path.exists():
            raise FileNotFoundError(f"找不到登录帧明文：{path}")
        plain = path.read_bytes()
        now = time.strftime("%Y-%m-%d %H:%M:%S").encode()
        plain = re.sub(rb'"timestamp":"[^"]*"', b'"timestamp":"' + now + b'"', plain)
        resp = self.send(32, tlv(10, xor_key(plain, SESSION_KEY)))
        return self.code_of(resp)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="小天才 IM 会话：自建握手 + 复用登录帧")
    parser.add_argument("--login", action="store_true", help="握手后按配置构造登录帧")
    parser.add_argument("--replay", action="store_true", help="改用重放抓包帧的方式登录")
    parser.add_argument("--frame", type=Path, default=None, help="登录帧明文路径")
    args = parser.parse_args()

    session = ImSession(log=lambda lvl, msg: print(f"[{lvl}] {msg}"))
    try:
        if args.login:
            code = session.login_from_config()
            print(f"登录状态码：{code}")
            if code != 200:
                return 1
        else:
            print("握手成功（未登录）")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
