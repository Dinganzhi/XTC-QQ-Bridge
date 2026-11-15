#!/usr/bin/env python3
"""IM 模式：不依赖 ADB，直接走小天才 IM 服务器收发消息。

`Xiaotiancai`（ADB + 界面自动化）的替代实现，对外暴露同名方法与状态常量，
让桥接层无需改动即可切换后端。

能力对照：

| 能力 | ADB 模式 | IM 模式 |
|---|---|---|
| 发文字 | 模拟点击输入框 | `cmd 40` 单聊消息 |
| 收消息 | 读界面气泡 | `cmd 12` 同步拉取 |
| 图片/表情 | 截屏取图 | 暂不支持（返回 None） |

配置项（`config.yaml`）：

```yaml
xiaotiancai:
  mode: "im"              # adb（默认）| im
  server:
    credentials: "~/.config/xtc-bridge/imoo-credentials.json"
    contacts: {}          # 联系人名 -> 对方 imAccountId
```

`contacts` 也可写在凭据文件的 `im_contacts` 字段里。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

# 状态常量与 ADB 模式保持一致
STATE_BACKGROUND = "background"
STATE_LOGIN = "login"
STATE_LIST = "list"
STATE_CHAT = "chat"
STATE_TEXT = "text"
STATE_POPUP = "popup"
STATE_OTHER = "other"
STATE_BLIND = "blind"
NOT_LOGGED_IN = "not_logged_in"
LOGGED_IN = "logged_in"


def _dv(buf: bytes, i: int):
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


class _NullAdb:
    """IM 模式下桥接层仍会摸 ADB 的几个方法，这里给最小空壳。"""

    def is_connected(self) -> bool:
        return True

    def is_in_foreground(self, package: str = "") -> bool:
        return True

    def ensure_connected(self, **kwargs) -> bool:
        return True

    def device_summary(self) -> str:
        return "server-mode"

    def __getattr__(self, _name):
        # 其余调用一律返回一个“什么都不做”的可调用对象
        return lambda *a, **k: None


class ServerXiaotiancai:
    """用 IM 协议代替界面自动化的后端。"""

    # 与 Xiaotiancai 对齐的状态常量
    STATE_BACKGROUND = STATE_BACKGROUND
    STATE_LOGIN = STATE_LOGIN
    STATE_LIST = STATE_LIST
    STATE_CHAT = STATE_CHAT
    STATE_TEXT = STATE_TEXT
    STATE_POPUP = STATE_POPUP
    STATE_OTHER = STATE_OTHER
    STATE_BLIND = STATE_BLIND
    # 桥接层会按 state 取中文名，所以这里必须是 dict 而不是字符串
    STATE_TEXT = {
        STATE_CHAT: "聊天窗口",
        STATE_LIST: "消息列表",
        STATE_LOGIN: "登录/验证页",
        STATE_OTHER: "小天才其它页面",
        STATE_POPUP: "弹窗遮挡界面",
        STATE_BACKGROUND: "App 不在前台",
        STATE_BLIND: "无法读取界面",
    }
    NOT_LOGGED_IN = NOT_LOGGED_IN
    LOGGED_IN = LOGGED_IN

    def __init__(self, cfg: dict | None = None, logger=None, cache_path: str | None = None):
        self.cfg = cfg or {}
        if logger is None:
            self.log = lambda *_a, **_k: None
        elif callable(logger):
            self.log = logger
        else:
            # 传进来的是 logging.Logger：包一层，兼容 log(level, msg) 调用约定
            _lg = logger

            def _log(level: str, msg: str, _lg=_lg) -> None:
                fn = getattr(_lg, str(level).lower(), None) or _lg.info
                try:
                    fn(msg)
                except Exception:
                    pass

            self.log = _log
        self._lock = threading.RLock()
        self._session = None
        self._current_contact = ""
        self._current_peer = 0
        # 子键既支持 im（新名），也兼容 server（旧名）
        srv = self.cfg.get("im") or self.cfg.get("server") or {}
        self.credentials_path = Path(
            os.path.expanduser(srv.get("credentials") or "~/.config/xtc-bridge/imoo-credentials.json")
        )
        self.contacts = dict(srv.get("contacts") or {})
        # 收消息：轮询每轮调 get_latest_message，
        # 这里做节流 + 缓存，避免每 2 秒就打一次网络
        self._last_fetch = 0.0
        self._fetch_interval = float(srv.get("fetch_interval", 2.0) or 2.0)
        self._pending: list[dict] = []
        self._sync_key = 0
        self._target_contact = str((cfg or {}).get("target_contact") or "")
        self._load_credentials()

    # ---------------------------------------------------------- 凭据

    def _load_credentials(self) -> None:
        try:
            self.cred = json.loads(self.credentials_path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"读取 IM 凭据失败：{self.credentials_path}（{exc}）") from exc
        # 凭据文件里也可以带联系人表
        for name, peer in (self.cred.get("im_contacts") or {}).items():
            self.contacts.setdefault(name, peer)

    def _peer_of(self, contact: str) -> int:
        """联系人名 -> 对方 imAccountId。"""
        name = (contact or "").strip()
        if not name:
            return 0
        if name in self.contacts:
            return int(self.contacts[name])
        # 支持直接写数字
        if name.isdigit():
            return int(name)
        for key, peer in self.contacts.items():
            if key and (key in name or name in key):
                return int(peer)
        return 0

    # ---------------------------------------------------------- 会话

    def renew_ticket(self) -> str:
        """用 SSO 重新签发一张 IM 票据。

        关键：`deviceId` 必须用账号的**持久化 SSO 设备身份**（配置里的
        `im_sso_device_id`）。随机 deviceId 签出来的票 IM 侧会回 4200。
        """
        from ..api.sso import SsoClient

        dev = str(self.cred.get("im_sso_device_id") or "").strip()
        if not dev:
            raise RuntimeError("配置缺少 im_sso_device_id，无法自动续票")
        phone = str(self.cred.get("phone") or "").strip()
        password = str(self.cred.get("password") or "").strip()
        if not phone or not password:
            raise RuntimeError("凭据文件缺少 phone/password，无法自动续票")
        res = SsoClient().password_login(phone, password, device_id=dev)
        self.cred["im_account_token"] = res.ticket
        try:
            self.credentials_path.write_text(
                __import__("json").dumps(self.cred, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass
        self.log("info", "[IM] 票据已自动续签")
        return res.ticket

    def _ensure_session(self):
        from ..im.session import ImSession

        if self._session is not None:
            return self._session
        session = ImSession(log=lambda lvl, msg: self.log("info", f"[IM] {msg}"))
        code = session.login_from_config(self.cred)
        if code in (4200, 102400):
            # 票据过期：自动续签后重试一次
            self.log("warning", f"[IM] 登录被拒（code={code}），尝试自动续票")
            try:
                session.close()
            except Exception:
                pass
            self.renew_ticket()
            session = ImSession(log=lambda lvl, msg: self.log("info", f"[IM] {msg}"))
            code = session.login_from_config(self.cred)
        if code != 200:
            session.close()
            raise RuntimeError(f"IM 登录失败：code={code}")
        # 长连接保活：票据约 10 小时过期，但会话建立后不会因票据过期失效，
        # 心跳不断就不用重新登录，从而绕开"必须找客户端续票"的问题。
        session.start_keepalive(float(self._fetch_interval * 30) or 60.0)
        self._session = session
        return session

    def _with_session(self, fn):
        with self._lock:
            try:
                return fn(self._ensure_session())
            except Exception:
                # 连接可能已断，重建一次
                try:
                    if self._session:
                        self._session.close()
                except Exception:
                    pass
                self._session = None
                return fn(self._ensure_session())

    # ---------------------------------------------------------- 兼容接口

    def launch(self) -> bool:
        """IM 模式无需启动 App。"""
        return True

    def login_state(self, force: bool = False) -> str:
        try:
            self._ensure_session()
            return LOGGED_IN
        except Exception as exc:  # noqa: BLE001
            self.log("warning", f"IM 会话不可用：{exc}")
            return NOT_LOGGED_IN

    def login(self, *args, **kwargs) -> str:
        """返回状态字符串（与 ADB 版一致）：already / ok / fail / error。

        注意**不能返回布尔值**：调用方会做 `"登录：" + _login_status_text(status)`，
        布尔会直接抛 `can only concatenate str (not "bool") to str`。
        """
        if self._session is not None:
            return "already"
        try:
            self._ensure_session()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            self.log("warning", f"[IM] 登录失败：{exc}")
            return "fail"

    def recover(self, *args, **kwargs) -> bool:
        try:
            if self._session:
                self._session.close()
        except Exception:
            pass
        self._session = None
        return self.login_state(force=True) == LOGGED_IN

    def app_state(self, *args, **kwargs) -> str:
        return STATE_CHAT if self._current_peer else STATE_LIST

    def app_state_with_root(self, *args, **kwargs):
        # 报 CHAT，轮询才会去取 get_latest_message；界面为 None
        return STATE_CHAT, None

    def is_in_chat(self) -> bool:
        return bool(self._current_peer)

    def chat_title(self, *args, **kwargs) -> str:
        return self._current_contact

    def chat_view_bounds(self, *args, **kwargs):
        return None

    def settle(self, *args, **kwargs) -> None:
        """界面稳定等待——IM 模式无界面，直接返回。"""
        return None

    def scroll_chat_by(self, *args, **kwargs) -> bool:
        return False

    def dismiss_blockers(self, *args, **kwargs) -> bool:
        return True

    def ensure_input_clean(self, *args, **kwargs) -> str:
        # 界面版返回的是给用户看的状态文字，不是布尔
        return "输入框无需清理（im 模式）"

    def learn_send_point(self, *args, **kwargs) -> bool:
        return True

    def blind_send_ready(self) -> bool:
        return True

    def begin_blind_send(self, text: str):
        return True, ""

    def end_blind_send(self, text: str):
        return True, "", False

    # 媒体类：IM 模式暂不支持
    def capture_sticker(self, *args, **kwargs):
        return None

    def sticker_of_latest(self, *args, **kwargs):
        return None

    def image_of_latest(self, *args, **kwargs):
        return None

    def bubble_clipped(self, *args, **kwargs) -> bool:
        return False

    # ---------------------------------------------------------- 收发

    def open_chat(self, contact: str) -> bool:
        peer = self._peer_of(contact)
        if not peer:
            self.log("warning", f"IM 模式找不到联系人「{contact}」的 imAccountId，"
                                f"请在 config.yaml -> xiaotiancai.server.contacts 里补上")
            return False
        self._current_contact = contact
        self._current_peer = peer
        return True

    def send_message(self, text: str) -> bool:
        if not self._current_peer:
            self.log("warning", "IM 模式未打开会话，无法发送")
            return False
        code = self._with_session(lambda s: s.send_text(self._current_peer, text, self.cred))
        if code == 200:
            self.log("info", f"[IM] 已发送 -> {self._current_contact}（{len(text)} 字）")
            return True
        self.log("warning", f"[IM] 发送失败 code={code}")
        return False

    def get_latest_message(self, *args, **kwargs):
        """界面版签名：(contact, text, time_label, own_text, own_recent)。

        IM 模式下由 `cmd 12` 同步拉取，返回一条待转发消息；没有新消息时返回空。
        """
        now = time.time()
        if now - self._last_fetch >= self._fetch_interval:
            self._last_fetch = now
            try:
                msgs, key = self.fetch_messages()
                self._sync_key = key or self._sync_key
                # 新消息追加到待处理队列（fetch_messages 只返回增量的文字消息）
                for m in msgs:
                    if m not in self._pending:
                        self._pending.append(m)
            except Exception as exc:  # noqa: BLE001 网络抖动不致命
                self.log("warning", f"[IM] 拉取消息失败：{exc}")
        if not self._pending:
            return (None, None, "", "", [])
        m = self._pending.pop(0)
        text = self._extract_text(m.get("content", ""))
        label = self._label_of(m.get("timestamp", 0))
        contact = self._target_contact or self._current_contact
        return (contact, text, label, "", [])

    @staticmethod
    def _extract_text(raw: str) -> str:
        """消息体是 JSON，取其中的 text/content 字段。"""
        try:
            obj = json.loads(raw)
        except Exception:
            return raw
        if isinstance(obj, dict):
            return str(obj.get("text") or obj.get("content") or raw)
        return raw

    @staticmethod
    def _label_of(ts_ms: int) -> str:
        """把毫秒时间戳格式化成 HH:MM（桥接按这个口径做去重和"太旧"判定）。"""
        if not ts_ms:
            return ""
        try:
            return time.strftime("%H:%M", time.localtime(ts_ms / 1000.0))
        except Exception:
            return ""

    def latest_kind(self, *args, **kwargs):
        return ""

    def message_kind(self, *args, **kwargs):
        return "text"

    def _chat_bubbles(self, *args, **kwargs):
        return []

    def _is_system_msg(self, *args, **kwargs) -> bool:
        return False

    def last_open_reason(self) -> str:
        return "server"

    def _dump_with_retry(self, *args, **kwargs):
        return None

    # IM 模式下部分代码会摸 `xtc.adb`
    adb = _NullAdb()
    package = "com.xtc.watch"

    # ---------------------------------------------------------- 拉取

    def fetch_messages(self, limit: int = 30, sync_key: int = 0) -> tuple[list[dict], int]:
        """拉一批同步消息，返回 (消息列表, 新的 syncKey)。

        `syncKey` 是**增量游标**而不是历史页码：传 0 表示"从头再来一次"，
        之后用返回的新游标继续增量拉取。服务端只保留增量，**不是**完整聊天归档，
        所以这里拿不到很久以前的聊天记录。

        只保留 `type == 3`（文字聊天），其它类型是电量/健康/成员等系统数据。
        """
        from .session import build_frame, tlv_int

        rid = 100000 + int(time.time()) % 100000

        def _pull(session):
            session._tls.sendall(
                build_frame(
                    12,
                    tlv_int(1, rid)
                    + tlv_int(11, int(self.cred["im_sender_account_id"]))
                    + tlv_int(12, int(self.cred["im_regist_id"]))
                    + tlv_int(13, int(sync_key))
                    + tlv_int(14, int(limit)),
                )
            )
            # 同步应答是**多帧**的：必须持续读到空闲，只读一帧会丢掉后续消息
            buf = b""
            session._tls.settimeout(6.0)
            try:
                while len(buf) < 2097152:
                    chunk = session._tls.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except Exception:
                pass
            return buf

        raw = self._with_session(_pull) or b""
        key = b"0123456789abcdef"
        out: list[dict] = []
        next_key = sync_key
        i = 0
        while i < len(raw):
            if raw[i] != 0xA0:
                i += 1
                continue
            length, start = _dv(raw, i + 2)
            if length is None or start + length > len(raw):
                break
            payload = raw[start : start + length]
            i = start + length
            j = 0
            while j < len(payload):
                tag, j2 = _dv(payload, j)
                if tag is None:
                    break
                ln, j3 = _dv(payload, j2)
                if ln is None or j3 + ln > len(payload):
                    break
                if tag == 10:
                    plain = bytes(b ^ key[k % len(key)] for k, b in enumerate(payload[j3 : j3 + ln]))
                    # 解出来的载荷还有一层内层头：[命令号][长度][TLV…]
                    _ic, _ip = _dv(plain, 0)
                    _il, _is = _dv(plain, _ip)
                    if _il is not None and _is + _il <= len(plain):
                        plain = plain[_is : _is + _il]
                    fields = {}
                    k = 0
                    while k < len(plain):
                        t2, k2 = _dv(plain, k)
                        if t2 is None:
                            break
                        l2, k3 = _dv(plain, k2)
                        if l2 is None or k3 + l2 > len(plain):
                            break
                        fields[t2] = plain[k3 : k3 + l2]
                        k = k3 + l2
                    if 13 in fields and 14 not in fields:
                        next_key = int.from_bytes(fields[13], "big")
                    if 14 in fields:
                        mtype = int.from_bytes(fields.get(18, b"\x00"), "big")
                        if mtype == 3:
                            out.append(
                                {
                                    "content": fields[14].decode("utf-8", "replace"),
                                    "msgId": fields.get(16, b"").decode("utf-8", "replace"),
                                    "timestamp": int.from_bytes(fields.get(17, b"\x00"), "big"),
                                    "type": mtype,
                                }
                            )
                j = j3 + ln
        return out, next_key


__all__ = ["ServerXiaotiancai", "STATE_CHAT", "STATE_LIST", "LOGGED_IN", "NOT_LOGGED_IN"]
