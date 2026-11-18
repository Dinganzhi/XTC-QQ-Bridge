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
        # 当前这条消息的类型与媒体地址（latest_kind / latest_sticker 用）
        self._current_kind = ""
        self._media_kind = "sticker"
        self._media_url = ""
        # syncKey 落盘：不持久化的话进程一重启就归零，下次会用 syncKey=0
        # 把服务端缓冲区里的历史全部重拉一遍（去重表能挡住重复转发，但白拉一次很浪费）。
        self._sync_key = self._load_sync_key()
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
        # nDeviceId 固定复用：每次换新值等于伪装成另一台设备，可能触发二次验证
        n_dev = str(self.cred.get("im_n_device_id") or "").strip()
        if not n_dev:
            import os as _os
            n_dev = self.cred["im_n_device_id"] = _os.urandom(16).hex()
        res = SsoClient().password_login(phone, password,
                                         device_id=dev, n_device_id=n_dev)
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

    # 本实现不读屏、不点击：初始化流程据此跳过"前台/启动/学按钮坐标"这些界面步骤
    uses_ui = False

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
        return "输入框无需清理"

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
                msgs, key = self.fetch_messages(sync_key=self._sync_key)
                if key and key != self._sync_key:
                    self._sync_key = key
                    self._save_sync_key(key)
                # 新消息追加到待处理队列（fetch_messages 只返回增量的文字消息）
                for m in msgs:
                    if m not in self._pending:
                        self._pending.append(m)
            except Exception as exc:  # noqa: BLE001 网络抖动不致命
                self.log("warning", f"[IM] 拉取消息失败：{exc}")
        if not self._pending:
            self._current_kind = ""
            self._media_url = ""
            return (None, None, "", "", [])
        m = self._pending.pop(0)
        kind = str(m.get("kind") or "text")
        self._current_kind = kind
        self._media_kind = kind
        self._media_url = str(m.get("mediaUrl") or "")
        if kind in ("sticker", "image"):
            # 媒体消息的正文只是给桥接做身份/说明用的占位，图片本身走 latest_sticker()
            text = str(m.get("mediaLabel") or ("表情" if kind == "sticker" else "图片"))
        else:
            text = self._extract_text(m.get("content", ""))
        label = self._label_of(m.get("timestamp", 0))
        contact = self._target_contact or self._current_contact
        return (contact, text, label, "", [])

    # 可能是消息时间的候选标签（按优先级）。App 给**表情包/图片**的帧偶尔不带
    # tag17，早先的实现会得到 0，然后 _label_of 返回空、_format_xtc_time 退回
    # "当前时间" —— 于是滞留消息的时间看起来就是拉取时刻。这里按候选依次找，
    # 只接受像毫秒时间戳的值（>= 1e12），找不到才返回 0（由调用方标注为未知）。
    _TS_TAGS = (17, 19, 21, 15)

    @classmethod
    def _pick_timestamp(cls, fields: dict) -> int:
        for tag in cls._TS_TAGS:
            raw = fields.get(tag)
            if not raw:
                continue
            v = int.from_bytes(raw, "big")
            if v >= 1_000_000_000_000:      # 毫秒时间戳
                return v
            if 1_000_000_000 <= v < 10_000_000_000:   # 秒级，补成毫秒
                return v * 1000
        return 0

    def _sync_key_file(self):
        from .. import runtime_paths
        return runtime_paths.data_path("im_sync_key.json")

    def _load_sync_key(self) -> int:
        try:
            return int(json.loads(self._sync_key_file().read_text("utf-8")).get("sync_key", 0))
        except Exception:
            return 0

    def _save_sync_key(self, key: int) -> None:
        try:
            self._sync_key_file().write_text(
                json.dumps({"sync_key": int(key)}), "utf-8")
        except Exception:
            pass

    # 表情包的媒体地址在这些键上（实测前两个有效）
    _MEDIA_KEYS = ("appPath", "wholeUrl", "watchPath", "url", "imgUrl", "picUrl",
                   "imageUrl", "stickerUrl")

    @classmethod
    def _classify(cls, raw: str) -> tuple[str, str, str]:
        """解析消息体，返回 `(kind, mediaUrl, label)`。

        聊天消息（`type=3`）有三种形态，媒体地址的位置**各不相同**：

        * 纯文字    `{"content":"文本", "textMsgType":0, ...}`
        * 表情包    `{"content":"{\"appPath\":\"http://…\",\"code\":…}", "textMsgType":0}`
                    —— 媒体在 `content` 这层 JSON 里
        * 照片/视频 `{"msgNewType":6, "textMsgType":2,
                     "msgNewContent":"{\"source\":{\"downloadUrl\":\"https://…\"}}"}`
                    —— 媒体在 `msgNewContent` 里，`content` 字段甚至不存在

        早期实现只看 `content`，因此照片类消息被当成文字转发（QQ 里只剩一句
        「给你发了一条图片消息」），这是图片收不到的真正原因。
        """
        try:
            obj = json.loads(raw)
        except Exception:
            return "text", "", ""
        if not isinstance(obj, dict):
            return "text", "", ""

        # ① 照片 / 视频：媒体信息在 msgNewContent（新格式，优先判定）
        newc = obj.get("msgNewContent")
        if isinstance(newc, str) and newc.lstrip().startswith("{"):
            try:
                nb = json.loads(newc)
            except Exception:
                nb = None
            if isinstance(nb, dict):
                src = nb.get("source")
                if isinstance(src, dict):
                    url = str(src.get("downloadUrl") or src.get("url") or "")
                    if url.startswith("http"):
                        # 只有视频才带时长字段；这里统一按图片处理
                        return "image", url, ""
                for key in ("downloadUrl", "url", "imgUrl", "picUrl"):
                    url = str(nb.get(key) or "")
                    if url.startswith("http"):
                        return "image", url, ""

        # ② 表情包：媒体在 content 这层 JSON 里
        inner = obj.get("content")
        if isinstance(inner, str) and inner.lstrip().startswith("{"):
            try:
                body = json.loads(inner)
            except Exception:
                body = None
            if isinstance(body, dict):
                for key in cls._MEDIA_KEYS:
                    url = str(body.get(key) or "")
                    if not url.startswith("http"):
                        continue
                    emojiish = any(k in body for k in
                                   ("emojiType", "expressionType", "facecode",
                                    "packageName", "seriesId"))
                    kind = "sticker" if emojiish else "image"
                    label = str(body.get("code") or body.get("desc") or "") or "表情"
                    return kind, url, label

        return "text", "", ""

    @staticmethod
    def _sniff_ext(data: bytes) -> str:
        """按**内容魔数**判定图片格式。

        不能信 URL 后缀：实测照片地址写的是 `.webp`，下载下来的字节却是 JPEG
        （`ff d8 ff`）。按后缀命名会让下游解码器选错分支。
        """
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:3] == b"GIF":
            return ".gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        return ".bin"

    def _fetch_media(self, url: str) -> tuple[bytes, str]:
        """下载媒体（带本地缓存）。失败返回 `(b"", "")`，由调用方走重试。"""
        import hashlib as _hashlib
        import urllib.request as _req

        from .. import runtime_paths as _rp

        # 缓存按 URL 哈希命名、不含扩展名：扩展名由内容决定，等下载完再补
        name = _hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".bin"
        cache = _rp.data_path("im_media") / name
        try:
            if cache.exists() and cache.stat().st_size > 0:
                data = cache.read_bytes()
                return data, self._sniff_ext(data)
        except OSError:
            pass
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            req = _req.Request(url, headers={"User-Agent": "okhttp/3.12.0"})
            with _req.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if not data:
                return b"", ""
            tmp = cache.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(cache)
            return data, self._sniff_ext(data)
        except Exception as exc:  # noqa: BLE001 网络抖动交给桥接的重试队列
            self.log("warning", f"[IM] 下载媒体失败：{type(exc).__name__}")
            return b"", ""

    def latest_sticker(self, *args, **kwargs):
        """把当前这条消息的媒体交给桥接（IM 模式不需要读屏取图）。"""
        url = getattr(self, "_media_url", "")
        if not url:
            return None
        data, ext = self._fetch_media(url)
        if not data:
            return None
        return {"data": data, "kind": getattr(self, "_media_kind", "sticker"),
                "animated": ext == ".gif", "source": url}

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
        """把毫秒时间戳格式化成 `MM-DD HH:MM`。

        **必须带日期**：桥接的 `_abs_time_label` 对只有 `HH:MM` 的标签会补"今天"，
        于是昨天的滞留消息会被显示成今天、还会被判成"新消息"重复转发。
        带上日期后 `_abs_time_label` 能直接解析成绝对时间，与 App 的分组标签同口径。
        """
        if not ts_ms:
            return ""
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(ts_ms / 1000.0))
        except Exception:
            return ""

    def latest_kind(self, *args, **kwargs):
        """当前这条消息的类型：`text` / `sticker` / `image`。

        桥接据此决定走"文字"还是"图片"。必须和 `get_latest_message` 同源，
        否则媒体消息会被当成文字发出（QQ 只收到一段 JSON 或"图片"两个字）。
        """
        return getattr(self, "_current_kind", "") or ""

    def message_kind(self, *args, **kwargs):
        return getattr(self, "_current_kind", "") or "text"

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
                            # 表情包/图片也走 type=3，必须分类，否则会被当文字转发
                            _raw = fields[14].decode("utf-8", "replace")
                            _kind, _media_url, _media_label = self._classify(_raw)
                            out.append(
                                {
                                    "content": fields[14].decode("utf-8", "replace"),
                                    "kind": _kind,
                                    "mediaUrl": _media_url,
                                    "mediaLabel": _media_label,
                                    "msgId": fields.get(16, b"").decode("utf-8", "replace"),
                                    "timestamp": self._pick_timestamp(fields),
                                    "type": mtype,
                                }
                            )
                j = j3 + ln
        return out, next_key


__all__ = ["ServerXiaotiancai", "STATE_CHAT", "STATE_LIST", "LOGGED_IN", "NOT_LOGGED_IN"]
