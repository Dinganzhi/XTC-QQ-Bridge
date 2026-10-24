# -*- coding: utf-8 -*-
"""小天才 App 操作：界面/登录/弹窗/发送/读取

方法按职责拆分到同包的 mixin 模块；本文件只保留 __init__、类属性与未归段的方法。
"""
from __future__ import annotations

from .constants import *  # noqa: F401,F403
from .state import StateMixin
from .login import LoginMixin
from .popup import PopupMixin
from .chat import ChatMixin
from .send import SendMixin


class Xiaotiancai(StateMixin, LoginMixin, PopupMixin, ChatMixin, SendMixin):
    IMAGE_TEXT = IMAGE_TEXT
    LOGGED_IN = LOGIN_LOGGED_IN
    NOT_LOGGED_IN = LOGIN_NOT_LOGGED_IN
    LOGIN_UNKNOWN = LOGIN_UNKNOWN
    STATE_CHAT = "chat"              # 已在聊天窗口
    STATE_LIST = "list"              # 消息列表（这里才该找联系人）
    STATE_LOGIN = "login"            # 登录页/安全验证页（等登录，别找联系人）
    STATE_OTHER = "other"            # 其它页面（首页/设置/详情等）
    STATE_POPUP = "popup"            # 弹窗遮挡界面（升级提醒等，读不到下面的内容）
    STATE_BACKGROUND = "background"  # App 不在前台
    STATE_BLIND = "blind"            # 读不到界面（息屏/界面不空闲/dump 失败）
    STATE_TEXT = {
        STATE_CHAT: "聊天窗口",
        STATE_LIST: "消息列表",
        STATE_LOGIN: "登录/验证页",
        STATE_OTHER: "小天才其它页面",
        STATE_POPUP: "弹窗遮挡界面",
        STATE_BACKGROUND: "App 不在前台",
        STATE_BLIND: "读不到界面",
    }

    def __init__(self, adb: ADBController, cfg: dict | None = None, logger=None,
                 cache_path: str | None = None):
        self.adb = adb
        cfg = cfg or {}
        self.package = cfg.get("package", "com.xtc.watch")
        self.main_activity = cfg.get("main_activity", ".MainActivity")
        self.ui = cfg.get("ui", {}) or {}
        self.logger = logger
        self._cache_path = cache_path or ""
        self._warned_not_login = False
        # 登录态缓存：is_logged_in() 需要 dump 界面（1~2s），轮询每 2s 调一次会明显变慢；
        # 这里按 login_state_ttl 秒缓存，登录动作/启动动作后主动失效。
        self._login_state: tuple[float, bool] | None = None
        self._login_state_ttl = float(cfg.get("login_state_ttl", 5.0) or 0.0)
        # 交互路径的等待时间（秒）：默认偏短，可在 ui.interaction_delay 调整
        try:
            self._delay = max(0.1, float(self.ui.get("interaction_delay", 0.6)))
        except (TypeError, ValueError):
            self._delay = 0.6
        # 发送重试轮数（每次仅在"输入框仍留有内容"等可安全重试的情况下重发）
        try:
            self._send_retries = max(1, int(self.ui.get("send_retries", 2)))
        except (TypeError, ValueError):
            self._send_retries = 2
        # 注意：这些"上次做某事"的时间戳一律用 -inf 作初值，**不能写 0.0**。
        # time.monotonic() 的零点任意（Linux 上是开机时长），刚开机/刚启动的机器上
        # now - 0.0 可能小于节流间隔，导致该做的事被当成"刚做过"而跳过。
        self._net_retry_ts = float("-inf")     # 网络弹窗"重试"按钮的点击冷却
        self._last_dump_warn = float("-inf")   # "读不到界面"告警的节流时间戳
        self._last_label_retry = float("-inf")  # "快照里没有时间标签"的补救重读节流
        self._throttle: dict[str, float] = {}   # 同类失败告警节流：key -> 上次打印时刻
        self._last_verify_root = None   # 注入校验时的界面快照（复用给"找发送按钮"，省一次 dump）
        self._last_confirm_root = None  # 发送后确认用的快照（复用给"补点一次发送"，省一次 dump）
        # 弹窗自动关闭的"同一弹窗最多试几轮"状态（空 key 表示当前没有弹窗）
        self._popup_key = ""            # 当前弹窗的指纹（Activity + 标题 + 说明前若干字）
        self._popup_tries = 0           # 这个弹窗已经尝试关闭的轮数
        # 最近一次界面快照（轮询每 2 秒就会更新）：发送等交互复用它，省一次 ~3 秒的 dump
        self._snapshot = None
        self._snapshot_ts = float("-inf")
        # 发送按钮坐标缓存 + 快路径开关（省掉"注入后 dump 一次找发送按钮"的 ~3 秒）
        # 缓存会落盘（cache_path）：重启后第一条消息也能走"先手打字"，
        # 否则重启后的第一条只能走稳妥流程（多 dump 两三次 = 十几秒）。
        self._send_cache: dict | None = None
        self._load_send_cache()
        # "先手打字"的中间状态（begin_blind_send -> end_blind_send）
        self._blind: dict | None = None
        self._blind_enabled = bool(self.ui.get("blind_send", True))
        self._fast_send = bool(self.ui.get("fast_send", True))
        try:
            self._snapshot_reuse = max(0.0, float(self.ui.get("snapshot_reuse", 3.0)))
        except (TypeError, ValueError):
            self._snapshot_reuse = 3.0
        # 点发送后等多久再 dump 确认：太短会白 dump 一次（约 3 秒），太长就只是慢一点。
        # 实测（24 次发送）：第一次 dump 常常撞上界面还没稳定而失败 -> 又花一次 dump，
        # 所以默认给到 0.6~0.8 秒，整体反而更快（`performance.profile` 会按档位覆盖它）。
        try:
            self._confirm_delay = max(0.0, float(self.ui.get("confirm_delay", 0.6)))
        except (TypeError, ValueError):
            self._confirm_delay = 0.6
        self.last_open_reason = ""   # 最近一次 open_chat 失败的原因（桥接据此去重打印）

    def log(self, level: str, msg: str):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    def _warn_throttled(self, key: str, msg: str, interval: float = 300.0) -> None:
        """同一类失败在 interval 秒内只打一次 warning，其余降为 debug。

        轮询每 2 秒跑一轮，失败原因又往往是"状态问题"（未登录、不在前台、
        联系人不在当前页），不节流就会把日志刷成"老是提示找不到联系人"。
        """
        now = time.monotonic()
        if now - self._throttle.get(key, float("-inf")) >= interval:
            self._throttle[key] = now
            self.log("warning", msg)
        else:
            self.log("debug", msg)

