# -*- coding: utf-8 -*-
"""桥接主逻辑：轮询、转发、补投、媒体、反向命令

方法按职责拆分到同包的 mixin 模块；本文件只保留 __init__、类属性与未归段的方法。
"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403
from .media import MediaMixin
from .forwarding import ForwardingMixin
from .reverse import ReverseMixin
from .login import LoginMixin
from .history import HistoryMixin
from .commands import CommandMixin


class MessageBridge(MediaMixin, ForwardingMixin, ReverseMixin, LoginMixin, HistoryMixin, CommandMixin):
    _XTC_USAGE = ("用法（在小天才聊天里直接输入）：\n"
                  "/小天才 搜索 <昵称>          白名单QQ私聊/群聊中找人（附最后消息时间）\n"
                  "/小天才 在线人数 <分钟>      最近N分钟白名单QQ会话发言人数（1-60）\n"
                  "/小天才 提醒 <群号> <QQID> [内容]    在指定QQ群内@提醒该用户\n"
                  "/小天才 历史消息 [条数] [来源]   查看对话记录（默认20条，可只看 手表/QQ群/QQ私聊）")

    def __init__(self, cfg: dict, adb, xtc, forwarder, logger=None):
        self.cfg = cfg
        self.adb = adb
        self.xtc = xtc
        self.forwarder = forwarder
        self.logger = logger
        self.running = False
        self._thread: threading.Thread | None = None
        self.dedup = Deduplicator()
        # 去重/回声/消息库的状态文件统一放"可写数据目录"（Nuitka onefile 下是 exe 旁边，
        # 放在 __file__ 旁边会写进退出即删的临时目录 -> 重启丢状态）
        runtime_paths.ensure_dirs()
        # 回声状态写入文件：多实例/重启后共享，防止重复转发
        store_path = str(runtime_paths.data_path("echo_cache.json"))
        self.echo = EchoFilter(store_path=store_path)
        # 长期已处理消息表（7 天持久化）：跨重启/多实例去重，杜绝死循环重复转发
        self.history = HistoryFilter(store_path=str(runtime_paths.data_path("history_cache.json")))
        # 本地消息库（/小天才 历史消息 数据源 + "库里有没有这条消息"的判定库）：
        # 条数上限 / 分库 / 只读最新几个分库，都可由 config.yaml -> msg_log 配置
        _ml = cfg.get("msg_log") or {}
        self.msgs = MessageLog(
            path=str(runtime_paths.data_path("msg_log.json")),
            cap=int(_ml.get("cap", 5000) or 0),                # 0 = 不限制
            shard_size=int(_ml.get("shard_size", 2000) or 0),  # 0 = 不分库（单文件）
            read_shards=int(_ml.get("read_shards", 1) or 1))   # 默认只加载最新分库
        self._poll_interval = float((cfg.get("xiaotiancai") or {}).get("check_interval", 2))
        self._heartbeat_interval = float((cfg.get("adb") or {}).get("heartbeat_interval", 10))
        self._login_check_interval = float(
            (cfg.get("xiaotiancai") or {}).get("login_check_interval", 600))
        # 自动登录重试节奏：超时/临时问题快速重试，明确的账号密码错误与安全验证则拉长间隔，
        # 避免"一次失败就永久不再尝试"（用户报告的"自动登录不生效"）。
        self._login_retry_interval = float(
            (cfg.get("xiaotiancai") or {}).get("login_retry_interval", 120))
        self._login_retry_after_fail = float(
            (cfg.get("xiaotiancai") or {}).get("login_retry_after_fail", 1800))
        self._login_retry_after_risk = float(
            (cfg.get("xiaotiancai") or {}).get("login_retry_after_risk", 900))
        # 操作锁：发送/导航期间暂停轮询，避免两个线程同时 uiautomator dump 冲突
        self._op_lock = threading.Lock()
        # 表情包 / 照片（**仅 小天才 -> QQ 单向**）：优先读 App 数据目录/图片缓存里的
        # **原文件**（动图 GIF 能保住动画、照片能拿回原图），无法取得再按气泡截图，
        # 最后退回发"表情X"/"图片"文字。
        _emoji = cfg.get("emoji") or {}
        self._emoji_image = bool(_emoji.get("forward_image", True))
        self._emoji_caption = bool(_emoji.get("caption", True))
        self._emoji_from_data = bool(_emoji.get("from_app_data", True))
        # 照片（图片消息）单独一个开关：原图动辄 1~2MB，想省流量/流量贵的可以关掉，
        # 关掉后仍会发**气泡截图**（内容对，只是清晰度低）
        self._emoji_photo = bool(_emoji.get("forward_photo", True))
        # root（可选）：设备已 root 时用 `su` 读 App 私有目录，取原图更可靠。
        # 注意：聊天数据库/相关 prefs 由 App 加密（SQLCipher），root 也读不到逐条时间。
        self._root = None
        _root_cfg = cfg.get("root") or {}
        if _root_cfg.get("enabled") and adb is not None:
            try:
                from ..root_access import RootAccess
                self._root = RootAccess(adb, _root_cfg, logger=logger)
                self._root.available()
            except Exception as e:  # noqa: BLE001 root 不可用不影响主流程
                self._log("debug", f"root 初始化失败（按普通权限运行）: {e}")
                self._root = None
        self._emoji_store = None
        if self._emoji_image and self._emoji_from_data and adb is not None:
            try:
                from ..emoji_store import EmojiStore
                self._emoji_store = EmojiStore(
                    adb, package=(cfg.get("xiaotiancai") or {}).get("package", "com.xtc.watch"),
                    logger=logger, match_ok=float(_emoji.get("match_ok", 0.80) or 0.80),
                    root_access=self._root)
            except Exception as e:  # noqa: BLE001 取原文件的能力不可用就只用截图
                self._log("debug", f"表情原文件读取不可用（改为截图）: {e}")
        # 有发送任务在排队/执行时置位：轮询据此让路（见 _poll_loop），别和发送抢 dump
        self._send_pending = False
        self._send_pending_ts = float("-inf")
        # 最近一次"轮询确认过就在聊天页"的时刻与标题（"预输入"快发的放行条件）
        self._chat_ok_ts = float("-inf")
        self._chat_ok_title = ""
        # "别睡"心跳：每 keep_awake_interval 秒发一次 WAKEUP（0 = 关闭）。
        # 实测：容器 大约每 50 秒把虚拟屏睡一次（stayOn/screen_off_timeout 都挡不住），
        # 而每 10 秒发一次 WAKEUP 能让它连续 2 分钟保持 Awake、App 一直留在前台
        # （单次开销仅 0.08 秒）。这是"预输入"能稳定命中、发送不再莫名 10~30 秒的关键。
        try:
            self._keep_awake_interval = float((cfg.get("adb") or {}).get("keep_awake_interval", 10) or 0)
        except (TypeError, ValueError):
            self._keep_awake_interval = 10.0
        self._last_awake_poke = float("-inf")
        # 断连重连的退避状态：设备真的连不上（如 容器连接 卡成 offline）时，
        # 不要每 10 秒就"重连一次"重复输出 + 空跑 12 个候选端口；失败后指数退避到最多 60 秒。
        self._reconnect_fails = 0
        self._next_reconnect_ts = float("-inf")
        # 取图时等发送线程让出界面锁的最长时间（秒）：超过就先按旧坐标试，
        # 宁可少一张图，也不要让补发流程卡在发送线程手里。
        self._media_lock_timeout = float((cfg.get("emoji") or {}).get("lock_timeout", 8) or 8)
        self._login_thread: threading.Thread | None = None
        # 登录待恢复标记：触发安全验证/登录失败后置位；
        # 轮询检测到重新登录时自动确认并 QQ 通知（无需重启）
        self._pending_login_notify = False
        # 自动登录节流状态
        self._login_not_before = 0.0      # 早于该时刻不再尝试自动登录
        self._login_inflight = False      # 已有一次登录任务在队列/执行中
        self._warned_no_cred = False      # 未配置账密的提示只打一次
        self._notify_seen: dict[str, float] = {}   # 通知去重：文本 -> 上次发送时刻
        self._log_seen: dict[str, float] = {}      # 日志去重：key -> 上次打印时刻
        self._poll_fail_streak = 0        # 连续"无法读取消息"的轮数（触发界面自愈）
        self._last_state = ""             # 上一次判定的 App 状态（用于"状态变化才记日志"）
        # 漏消息补发（从最新往回走、撞库即停）：弹窗挡住/界面无法读取期间到达的、
        # 以及启动前积压在聊天里的消息，都会按时间顺序补齐
        _xc = cfg.get("xiaotiancai") or {}
        self._catchup_enabled = bool(_xc.get("catchup_missed", True))
        self._catchup_max = int(_xc.get("catchup_max", 0) or 0)   # 0 = 不限制（默认）
        # 每轮最多往上看多少条气泡（可见区本来只有几条）：撞到"库里已有"的消息**不再停下**，
        # 而是继续往上找没转发过的（用户实测：16:38 的消息被更新的 19:19 照片挡住，5 小时后才发）
        self._catchup_scan = int(_xc.get("catchup_scan", 30) or 0)  # 0 = 不限制
        # 有图片/表情还在等原图时，后面的消息最多压多久再发（秒；0 = 不压、照旧先发）
        # 实测"错位"就是这么来的：那条图在等原图（几秒~几十秒），期间来的文字先发出去，
        # 于是 QQ 里看到"文字在图前面"。阻塞一小会儿，等图就绪后按顺序一起发。
        try:
            self._order_hold_secs = max(0.0, float(_xc.get("order_hold_secs", 45) or 0))
        except (TypeError, ValueError):
            self._order_hold_secs = 45.0
        # 补发"太旧就别发了"（秒，0 = 不限）：想避免"21:48 突然收到 16:38 的消息"就设它，
        # 例如 1800 = 超过 30 分钟的消息不再补发（会记进历史，不会每轮再翻出来）
        try:
            self._catchup_max_age = max(0.0, float(_xc.get("catchup_max_age", 0) or 0))
        except (TypeError, ValueError):
            self._catchup_max_age = 0.0
        self._reconnect_hinted = False  # 容器 断网提示只打一次
        # FIFO 任务队列：QQ->小天才 发送 / 登录 由单工作线程串行执行，
        # 保证多消息到达时按顺序处理，避免并发抢锁导致前后关系紊乱
        self._job_queue: queue.Queue = queue.Queue()
        # "发送专用"高优先级队列：QQ->小天才 是**用户正等着看**的交互（他在 QQ 里发完
        # 就盯着手表），绝不能排在小天才->QQ 的转发、历史查询、聊天内"发送成功"回执后面。
        # 实测（09-29 日志）：一条 QQ 消息在队列里等了 15~47 秒才轮到，就是被这些任务占着。
        self._fast_queue: queue.Queue = queue.Queue()
        # 为了给发送让路而"先取出来、稍后再做"的普通任务（保持原有先后顺序）
        self._held_jobs: collections.deque = collections.deque()
        self._job_thread: threading.Thread | None = None
        # ---- 转发"补投计划"：只补未送达的目标，绝不因为"有个群失败"就把私聊/别的群再发一遍 ----
        # 为什么要独立计划 + 独立线程（用户实测过的坑）：
        #   * 旧实现靠"轮询重新读屏"来重试，于是**退避节奏完全失控**（每 5 秒重发一次）；
        #   * 重试时它会把**所有目标**再发一遍（私聊被重复输出几十分钟）；
        #   * 消息身份在"标签读到了/没读到"之间会跳变，靠身份去重也挡不住这种重发。
        # 现在：第一次尝试就把"这条已经处理过"记进历史，失败的目标写进补投计划，
        # 由专门的线程按退避节奏**只补那些目标**；到达上限后放弃并明确报错。
        self._fwd_plan: dict[tuple, dict] = {}
        self._fwd_plan_lock = threading.Lock()
        self._fwd_retry_thread: threading.Thread | None = None
        # 内容级"刚转发过"护栏：同一联系人 + 同一文本，N 秒内不再重复转发
        # （App 的时间标签会时有时无，光靠"文本+标签"当身份挡不住重复；用户报告的就是这个）
        self._recent_fwd: dict[tuple, float] = {}
        # 同内容最近一次转发的时间（身份标签的 epoch）：用来挡"同内容时间倒流"（标签/时钟漂移）
        self._fwd_clock: dict[tuple, float] = {}
        _fwd = cfg.get("forward") or {}
        try:
            # 0 = 失败就放弃（不补投）；默认 3 次
            self._forward_max_tries = max(0, int(_fwd.get("retry_max_tries", 3) or 0))
        except (TypeError, ValueError):
            self._forward_max_tries = 3
        try:
            self._forward_retry_backoff = max(5.0, float(_fwd.get("retry_backoff", 60) or 60))
        except (TypeError, ValueError):
            self._forward_retry_backoff = 60.0
        try:
            self._forward_content_guard = max(
                0.0, float(_fwd.get("content_dedup_secs", 300) or 0))
        except (TypeError, ValueError):
            self._forward_content_guard = 300.0
        # 媒体（图片/表情）**还没下载完**时的等待重试：App 先画出占位图，原图可能
        # 几秒~几分钟后才落盘（实测 22:12 检测到消息、22:19 原图才出现）。
        # 这期间先不转发，否则 QQ 只会收到"图片"两个字。
        self._pending_media: dict[tuple, dict] = {}
        # 显示时间的新规则（用户要求 10-20）：标签距今 <= label_now_window（默认 300 秒）
        # 就是"刚刚发生"的消息 -> 用**当前真实时间**（App 的分组标签会让组内后续消息早 1~2 分钟）；
        # 更早的 -> 用**原始标签**，绝不改写成补发时刻（实测 10-19 21:30 的消息被写成 07:31）。
        # 旧配置名 label_stale_secs 仍兼容。
        try:
            self._label_now_window = max(10.0, float(
                _xc.get("label_now_window", _xc.get("label_stale_secs", 300)) or 300))
        except (TypeError, ValueError):
            self._label_now_window = 300.0
        try:
            # 没有标签时，"上一条已入库消息的时间"最多能往回用多久（秒）
            self._label_fallback_max = max(600.0, float(
                _xc.get("label_fallback_max", 12 * 3600) or 12 * 3600))
        except (TypeError, ValueError):
            self._label_fallback_max = 12 * 3600.0
        try:
            # 补发时向上多滚动几屏继续找（0 = 只看当前可见区）
            self._catchup_screens = max(0, int(_xc.get("catchup_screens", 1) or 0))
        except (TypeError, ValueError):
            self._catchup_screens = 1
        try:
            # 每隔多少秒做一次**深度扫描**（无视已处理消息、多滚动几屏），0 = 关闭
            self._catchup_deep_every = max(0.0, float(
                _xc.get("catchup_deep_every", 600) or 0))
        except (TypeError, ValueError):
            self._catchup_deep_every = 600.0
        self._last_deep_ts = 0.0        # 上次深度扫描的时刻（monotonic）
        # 上次**成功读到聊天界面**的时刻（墙钟）；用来判断"中断了多久"，
        # 从而把"太旧不补"的年龄上限放宽到中断时长（见 _stale_limit）。
        self._last_ok_read_ts = 0.0
        # 标签距今不超过该窗口时，显示用"观测时间"而非 App 的组内标签
        # （App 只给一组消息的第一条画标签，同组后续共用组首时间）。
        try:
            self._label_recent_window = max(
                float(self._label_now_window),
                float(_xc.get("label_recent_window", 600) or 600))
        except (TypeError, ValueError):
            self._label_recent_window = 600.0
        self._force_deep = False        # 一次性补发（--catchup）时强制深度扫描
        _media = cfg.get("emoji") or {}
        try:
            self._media_retry_max = max(1, int(_media.get("wait_retries", 8) or 8))
        except (TypeError, ValueError):
            self._media_retry_max = 8
        try:
            self._media_retry_base = max(1.0, float(_media.get("wait_base", 5) or 5))
        except (TypeError, ValueError):
            self._media_retry_base = 5.0
        try:
            self._media_retry_max_wait = max(
                30.0, float(_media.get("wait_max_secs", 1800) or 1800))
        except (TypeError, ValueError):
            self._media_retry_max_wait = 1800.0
        self._last_chat_open = float("-inf")  # 聊天窗口重开冷却（初值 -inf：见上）
        # 自动登录检测开关（/小天才 自动登录 可切换；默认开启）
        self._auto_login_enabled = bool(
            (cfg.get("xiaotiancai") or {}).get("auto_login", True))
        # xtc 侧命令（在小天才聊天里直接输入，由本桥程序解析执行，与 QQ 命令隔离）：
        # 命令前缀。去重按"身份 (侧, 文本, 时间标签)"：已执行过的命令（文件持久化）
        # 不再重复执行——即使消息仍是"最新一条"或桥接重启，也不会反复发帮助；
        # 用户再次输入相同命令（新消息/新时间标签）仍可执行。
        self._xtc_cmd_prefix = str(
            (cfg.get("xiaotiancai") or {}).get("cmd_prefix", "/小天才")).strip()
        self._cmd_pending: dict[str, tuple[str, str]] = {}  # text -> (side, label)
        self._cmd_done: set[tuple[str, str, str]] = set()
        # 会话级"这条命令已经处理过"记忆：(side, text) -> 已处理时的 App 时间标签。
        # 解决"同一条 /小天才 反复触发"：命令消息会一直挂在"最新一条"上，若 App 的
        # 时间标签也一直不变（同一分钟/解析不出时间），仅靠 (side, text, label) 判重
        # 不足；这里标签一变（说明是用户新输入的一条）才允许再次执行。
        self._cmd_seen_text: dict[tuple[str, str], str] = {}
        self._cmd_done_file = str(runtime_paths.data_path("xtc_cmd_done.json"))
        self._cmd_lock = threading.Lock()
        self._load_cmd_done()

    def catchup_now(self, screens: int = 0) -> int:
        """命令行一次性补发：确保在小天才聊天页，然后走与轮询相同的补发逻辑。

        用途：桥接停过一段时间（ADB 掉线、容器休眠、窗口失去焦点）后，把积压的图片与
        文字按原顺序补齐。不启动轮询与 webhook，转发改为**同步执行**（没有工作线程）。
        screens > 0 时覆盖 `xiaotiancai.catchup_screens`（向上多滚动几屏）。
        返回本次转发的条数。
        """
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if screens and screens > 0:
            self._catchup_screens = int(screens)
        self._force_deep = True        # 命令行补发总是做深度扫描
        self._catchup_max_age = 0      # 手动补发不受"太旧不补"限制（自动轮询仍然受限）
        try:
            with self._op_lock:
                try:
                    self.xtc.settle()
                except Exception as e:  # noqa: BLE001 清弹窗失败不影响补发
                    self._log("debug", f"[补发] 清理弹窗失败: {e}")
                if not self.xtc.adb.is_in_foreground(self.xtc.package):
                    self.xtc.launch()
                    self.xtc.settle()
                if not self.xtc.is_in_chat():
                    if not self.xtc.open_chat(contact):
                        self._log("error", f"[补发] 无法打开与「{contact}」的聊天，未能补发")
                        return 0
                    self.xtc.settle()
        except Exception as e:  # noqa: BLE001 界面准备失败就直接返回
            self._log("error", f"[补发] 准备界面失败: {e}")
            return 0
        root = self.xtc._dump_with_retry(2)
        if root is None or not self.xtc.is_in_chat(root):
            self._log("error", "[补发] 当前不在聊天页，未能补发")
            return 0
        # 没有工作线程：把"入队"改成同步转发，逻辑（去重/原图/顺序）与线上一致
        original = self._queue_forward

        def _sync(c, t, l, sticker=None, display_label="", kind=""):
            self._do_forward_job(c, t, l, sticker=sticker,
                                 display_label=display_label, kind=kind)

        self._queue_forward = _sync
        try:
            return self._forward_backlog(root, contact)
        finally:
            self._queue_forward = original

    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, name="xtc-poll", daemon=True)
        self._thread.start()
        # 表情包名字索引后台预热：第一次收到表情时才建要 ~4 秒（会卡一下读屏），
        # 提前在后台建好，索引本身缓存 10 分钟。
        if self._emoji_store is not None:
            threading.Thread(target=self._warm_emoji_store, name="xtc-emoji-warm",
                             daemon=True).start()
        self._login_thread = threading.Thread(target=self._login_check_loop, name="xtc-login-check", daemon=True)
        self._login_thread.start()
        self._job_thread = threading.Thread(target=self._job_worker, name="xtc-jobs", daemon=True)
        self._job_thread.start()
        # 补投线程：只补"未送达的目标"（不碰界面，所以不会和发送抢界面锁）
        self._fwd_retry_thread = threading.Thread(target=self._forward_retry_loop,
                                                  name="xtc-fwd-retry", daemon=True)
        self._fwd_retry_thread.start()
        self._log("info", f"小天才消息轮询已启动（间隔 {self._poll_interval}s）")
        # 启动即自动初始化（等价于 QQ 命令 /小天才 初始化，以前只有手动发命令才会做）：
        # 清弹窗 -> 确认前台 -> 按需登录 -> 进入聊天页 -> 清空输入框残留。
        # 不做这一步时，启动后的第一轮轮询可能还在列表页/残留状态上，
        # 读到的第一条"新消息"实际是启动前的旧消息。
        try:
            self._job_queue.put(("init", ""))
            self._log("info", "已排队启动自动初始化（清弹窗 / 进聊天页 / 清输入框残留）")
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"启动自动初始化入队失败: {e}")

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._login_thread:
            self._login_thread.join(timeout=5)
        if self._job_thread:
            self._job_thread.join(timeout=5)
        if self._fwd_retry_thread:
            self._fwd_retry_thread.join(timeout=5)

    def _next_job(self):
        """取下一个任务：**发送优先**（返回 (来源队列, 任务)）。

        规则：
          1) 发送队列里有活 -> 先做发送（用户正等着）；
          2) 之前为让路而暂存的任务 -> 按原顺序继续（不会再被插队）；
          3) 否则从普通队列取；取到后发现发送在排队 -> 把普通任务暂存，先做发送。
        这样既不饿死普通任务，也不会让"最新的一条 QQ 消息"排在一堆转发后面。
        """
        try:
            return self._fast_queue, self._fast_queue.get_nowait()
        except queue.Empty:
            pass
        if self._held_jobs:
            return self._held_jobs.popleft()
        try:
            job = self._job_queue.get(timeout=0.2)
        except queue.Empty:
            return None
        if not self._fast_queue.empty():
            self._held_jobs.appendleft((self._job_queue, job))
            return self._fast_queue, self._fast_queue.get_nowait()
        return self._job_queue, job

    def _job_worker(self) -> None:
        while self.running:
            picked = self._next_job()
            if picked is None:
                continue
            src, job = picked
            try:
                kind = job[0]
                if kind == "send":
                    _, text, user_id, group_id, request_id = job
                    self._do_send_job(text, user_id, group_id, request_id)
                elif kind == "forward":
                    # 小天才 -> QQ 的转发放在工作线程做：插件要等 QQ 侧真实结果才回包
                    # （最长 30 秒），放在轮询线程里会把"读屏"一起拖住。
                    _, f_contact, f_text, f_label = job[:4]
                    f_sticker = job[4] if len(job) > 4 else None
                    f_disp = job[5] if len(job) > 5 else ""
                    f_kind = job[6] if len(job) > 6 else ""
                    f_hash = job[7] if len(job) > 7 else ""
                    self._do_forward_job(f_contact, f_text, f_label, sticker=f_sticker,
                                         display_label=f_disp, kind=f_kind,
                                         media_hash=f_hash)
                elif kind == "confirm":
                    # 小天才聊天里的"发送成功：…"回执：低优先级（见 _queue_confirm_xtc）
                    self._confirm_xtc_delivery(job[1])
                elif kind == "login":
                    _, request_id = job
                    self._do_login_job(request_id)
                elif kind == "init":
                    _, request_id = job
                    self._do_init_job(request_id)
                elif kind == "history":
                    # 小天才历史消息：count + 回传方式（request_id 或写入小天才聊天）+ 来源过滤
                    _, count, request_id, into_chat = job[:4]
                    src_name = job[4] if len(job) > 4 else ""
                    self._do_history_job(count, request_id, into_chat, src_name)
                elif kind == "cmd":
                    # xtc 侧命令（在小天才聊天里输入的 /小天才 xxx，手表侧或家长侧均可）
                    _, text = job
                    ident = self._cmd_pending.pop(text, None)
                    ok = self._do_cmd_job(text)
                    if ok and ident is not None:
                        # 执行成功 -> 标记为已完成（持久化），同一条消息不再重复执行
                        self._cmd_done_add(ident[0], text, ident[1])
                    # 回复失败：不标记，下一轮轮询自动重试
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"任务处理异常: {e}")
            finally:
                try:
                    src.task_done()
                except ValueError:  # noqa: BLE001 理论上不会发生，别让工作线程挂掉
                    pass

    def _poll_loop(self) -> None:
        last_heartbeat = float("-inf")   # 首轮就做一次 ADB 心跳检查（monotonic 零点任意）
        while self.running:
            loop_started = time.monotonic()
            # 有消息要发时先让路：发送要拿操作锁，轮询若正好开始一轮 dump（3~4 秒），
            # 发送就得等它做完 —— 用户看到的"点了发送半天才出现文字"有一部分就是这个。
            # 最多让路 30 秒（长队列不会把读消息饿死；漏掉的消息由撞库补发兜底）。
            if self._send_pending and (loop_started - self._send_pending_ts) < 30:
                time.sleep(0.05)
                continue
            # 轻量"别睡"心跳：容器会随宿主窗口状态息屏（screen_off_timeout 拉满也挡不住），
            # 隔稍后发一次 WAKEUP，把屏叫醒并重置用户活动计时 —— 屏不睡，App 就一直在前台，
            # "App 不在前台 -> 重新拉起"（实测每次 ~20 秒）和"预输入"被门闩挡住都会少很多。
            if self._keep_awake_interval > 0 and \
                    loop_started - self._last_awake_poke >= self._keep_awake_interval:
                self._last_awake_poke = loop_started
                try:
                    self.adb.poke_awake()
                except Exception as e:  # noqa: BLE001 心跳失败不影响轮询
                    self._log("debug", f"保活心跳失败: {e}")
            try:
                now = time.monotonic()
                if now - last_heartbeat >= self._heartbeat_interval:
                    last_heartbeat = now
                    if not self.adb.is_connected():
                        if not self._heartbeat_reconnect(now):
                            time.sleep(2)
                            continue

                if not self._op_lock.acquire(blocking=False):
                    continue  # 正在发送/导航，跳过本轮，避免 dump 冲突
                try:
                    # 恢复检测：安全验证完成后自动确认并通知（无需重启）
                    if self._pending_login_notify:
                        if self.xtc.login_state() == LOGIN_LOGGED_IN:
                            self._pending_login_notify = False
                            self._notify("小天才已重新登录（安全验证完成），桥接继续运行")
                    # 先判状态再决定做什么（以前只问"在不在聊天页"，于是在登录页/
                    # 别的页面上反复找联系人，刷一堆"找不到联系人"且不对症）。
                    state, root = self.xtc.app_state_with_root(2)
                    self._log_state(state)
                    xtc_contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
                    if state == self.xtc.STATE_CHAT:
                        # 记下"最近一次确认过就在聊天页"（含标题），"预输入"快发据此放行
                        self._chat_ok_ts = time.monotonic()
                        if root is not None:
                            self._last_ok_read_ts = time.time()
                        self._chat_ok_title = self.xtc.chat_title(root)
                        contact, text, time_label, own_text, own_recent = \
                            self.xtc.get_latest_message(root)
                    else:
                        contact, text, time_label, own_text, own_recent = (None, None, "", "", [])
                        if state == self.xtc.STATE_LOGIN:
                            # 登录/安全验证页：等登录线程处理，绝不在这里找联系人
                            self._log_once("state_login",
                                           "当前在小天才登录/安全验证页，等待登录完成"
                                           "（此时不会去找联系人）", interval=600)
                        elif state == self.xtc.STATE_BACKGROUND:
                            # 容器窗口失去焦点/息屏时 App 会"看起来不在前台"，但窗口往往还在。
                            # 先花 ~0.3 秒问一次电源状态：睡着就叫醒 —— 唤醒后 App 通常立刻
                            # 回到前台（它本来就是 resumed 的 Activity），下一轮就能正常读消息，
                            # 省掉"重新拉起 App"那 20 秒。
                            if self.adb is not None and self.adb.wake_if_asleep():
                                self._log_once("wake_asleep",
                                               "容器息屏导致 App 不在前台，已唤醒屏幕"
                                               "（唤醒后通常直接回到聊天页）", interval=120)
                        elif state == self.xtc.STATE_POPUP:
                            # 弹窗遮挡（如"升级提醒"）：弹窗不关就完全无法读取消息，
                            # 所以这里**不设冷却**，每次轮询都尝试关掉；
                            # 关不掉时由 xiaotiancai 侧 10 分钟提醒一次，避免重复输出。
                            if self.xtc.settle():
                                self._log("info", "检测到弹窗遮挡界面，已自动关闭")
                            else:
                                self._log_once(
                                    "state_popup_stuck",
                                    "弹窗遮挡界面且无法自动关闭（可在 config.yaml -> "
                                    "xiaotiancai.ui.popup_skip_texts 里补上它的按钮文案）",
                                    interval=600)
                        elif state in (self.xtc.STATE_LIST, self.xtc.STATE_OTHER):
                            cooldown = 30.0 if self._poll_fail_streak < 5 else 5.0
                            if xtc_contact and time.monotonic() - self._last_chat_open >= cooldown:
                                self._last_chat_open = time.monotonic()
                                if not self.xtc.open_chat(xtc_contact):
                                    reason = self.xtc.last_open_reason or "未知原因"
                                    # 同一原因 5 分钟只报一次，避免轮询重复输出
                                    self._log_once(f"open_chat:{reason[:24]}",
                                                   f"进入聊天失败：{reason}", interval=300)
                finally:
                    self._op_lock.release()
                # 补发：从最新一条往回走、撞库即停（弹窗挡住期间漏掉的、启动前积压的都补齐）。
                # 放在命令流程之前：它会跳过命令文本与系统提示，只处理真实消息。
                # 注意：聊天窗口模式下 get_latest_message 的 contact 本来就是 None，
                # 所以这里**不能**用 `contact is not None` 当条件 —— 那样补发永远不会跑
                # （用户报的"漏掉的消息再也补不上"就是这么来的）。
                if state == self.xtc.STATE_CHAT:
                    try:
                        self._forward_backlog(root, contact or "")
                    except Exception as e:  # noqa: BLE001
                        self._log("warning", f"补发流程异常: {e}")
                # 连续无法读取任何东西（且不在聊天页/被弹窗挡住）-> 自愈
                if not text and not own_recent:
                    self._poll_fail_streak += 1
                    if self._poll_fail_streak in (6, 20) or self._poll_fail_streak % 60 == 0:
                        with self._op_lock:
                            state = self.xtc.recover(
                                (self.cfg.get("target") or {}).get("xtc_contact", ""))
                        self._log("info", f"轮询连续 {self._poll_fail_streak} 轮未读到消息，"
                                          f"界面自愈: {state}")
                else:
                    self._poll_fail_streak = 0
                is_cmd_text = bool(self._xtc_cmd_prefix and text
                                   and text.startswith(self._xtc_cmd_prefix))
                # xtc 侧命令（/小天才 …）：手表侧或家长侧输入均可。
                # 1) 手表侧（对方发来）的命令 -> 执行，不转发、不入消息库；
                #    已被处理过（同一侧+同一文本+同一时间标签）就不再进入执行流程，
                #    这样桥接自己发出去的"结果/帮助"被读回时也不会再被当成新命令。
                if is_cmd_text:
                    if not self._cmd_text_handled("watch", text, time_label):
                        self._log("info", f"[收到小天才命令] 来源=手表 内容={text!r} 时间标签={time_label or '(无)'}")
                        self._maybe_xtc_cmd("watch", text, time_label)
                    else:
                        self._log("debug", f"[收到小天才命令] 重复（已执行过）: {text!r}")
                # 2) 家长侧输入的命令：可能被送达确认等新消息盖过（不再是"最新一条"），
                #    扫最近若干条自己发的消息；跳过桥接自己转发过去的旧命令文本
                elif own_recent:
                    for t, lbl in own_recent:
                        if not (self._xtc_cmd_prefix and t.startswith(self._xtc_cmd_prefix)):
                            continue
                        if self.history.seen("qq2xtc", t):
                            continue  # 曾经由桥接转发进聊天的文本，不视为新输入的命令
                        if self._cmd_text_handled("own", t, lbl):
                            continue  # 已经执行过这条（含桥接自己发出去的回复）
                        self._log("info", f"[收到小天才命令] 来源=家长侧 内容={t!r} 时间标签={lbl or '(无)'}")
                        self._maybe_xtc_cmd("own", t, lbl)
                        break
                # 普通手表消息 -> 转发（命令已被上面拦截，绝不转发/入库）
                if text and not is_cmd_text:
                    # 事件身份 = 文本 + **绝对时间**（把 App 的"今天/昨天/星期X"标签统一成
                    # MM-DD HH:MM）。用 App 原始标签当身份会出事：同一条消息隔天标签从
                    # "16:18" 变成 "昨天 16:18"，于是一条旧消息会被当成新消息重复转发。
                    raw_label = (time_label or "").strip()
                    label = self._abs_time_label(raw_label) or raw_label
                    key = ("xtc", contact or "", text, label)
                    dup = (self.history.seen(*key) or self.dedup.seen(key)
                           or self.echo.is_echo(text)
                           or self._forward_suppressed(contact, text, label))
                    if not dup and not label:
                        # 无法读取时间标签时只能按文本保守判定：宁可不重发，也不要反复刷同一条
                        dup = self.msgs.seen(text, "xtc")
                    # 太旧的积压消息不再转发（用户实测："九点四十八分才发了个四点半的消息"）：
                    # 它必然排在已发过的更新消息后面 = 错位。记进历史，别每轮再翻出来。
                    if not dup:
                        stale = self._stale_age(label, raw_label)
                        if stale is not None:
                            self._log_once(
                                f"stale:{text[:16]}",
                                f"[跳过] 这条是 {stale / 60:.0f} 分钟前的消息"
                                f"（超过 xiaotiancai.catchup_max_age），不再转发: {text[:24]!r}",
                                interval=300)
                            self.history.mark("xtc", contact or "", text, label)
                            dup = True
                    if not dup and self._content_went_backwards(contact, text, label):
                        # 同内容 + 时间比上次转发过的更早 = 标签/时钟漂移（跨零点那类），别重发
                        self._log("info", f"[跳过] 同内容的消息时间倒流了（{label}），"
                                          f"判为重复不再转发: {text[:24]!r}")
                        self.history.mark("xtc", contact or "", text, label)
                        dup = True
                    # 图片/表情：媒体（原图/贴纸）可能**还没下载完**（App 先画出占位图，
                    # 原图要过几秒~几分钟才落盘）。这时不能马上按"文字"转发 ——
                    # 否则 QQ 只会收到"图片"两个字（用户实测：22:12 检测到、22:19 原图才出现）。
                    # **类型一律以气泡本身为准**：实测手打了一条内容就是"表情"的文本，
                    # 按前缀判成表情包后等了 10 分钟，最后发出一条没有图的"[表情]"（空气消息）。
                    # 优先用 `latest_kind()`：它与上面那条 text 来自**同一次解析**；
                    # 只有它无法取得（空）时才回退到拿界面再核对一遍。
                    kind = ((self.xtc.latest_kind() if text else "") or
                            (self.xtc.message_kind(root, text) if text else "text"))
                    is_media = kind in ("sticker", "image")
                    mkey = self._fwd_key(contact, text, label)
                    if not dup and is_media and mkey in self._pending_media:
                        dup = True          # 本轮静默跳过：在等媒体/重试中，不转发也不重复输出日志
                    if not dup and not is_media and self._order_hold_active():
                        # 有图片/表情还在等原图：这条先压一下，否则它一定排在那张图前面（错位）。
                        # 不丢：等媒体就绪后由补发流程按顺序带出去（最多压 order_hold_secs 秒）。
                        self._log("debug", f"[顺序] 有图片/表情在等原图，这条暂缓: {text[:24]!r}")
                        dup = True
                    if not dup:
                        self._log("info", f"[收到小天才消息] 来源={self._xtc_source(contact)} "
                                          f"时间={time_label or '(无)'} 内容={text!r}")
                        # 表情包/图片：**必须在轮询线程里取**（此刻快照/气泡位置才准、
                        # 缓存里刚写进来的原文件也还在），而且要**挡住发送线程**——
                        # 发送会点输入框/打字/写"发送成功"提示，界面一动坐标就废了。
                        # 传消息自己的时间：检测可能滞后几分钟（刚重启/刚唤醒时），
                        # 缓存文件是"消息显示时"写进去的，只有按消息时间才找得回原图。
                        if is_media:
                            got_lock = self._op_lock.acquire(timeout=self._media_lock_timeout)
                            try:
                                sticker = self._capture_sticker(
                                    root, text, near_epoch=self._label_epoch(time_label or ""),
                                    match_label=key[3] or raw_label, kind=kind)
                            finally:
                                if got_lock:
                                    self._op_lock.release()
                            if sticker is None:
                                # 无法取得媒体：登记"稍后重试"，**这一轮先不发**
                                # （不发的话 QQ 只会收到"图片"两个字）
                                self._media_retry_failed(mkey, contact, text, label, time_label,
                                                         "界面上还是占位图、缓存里也尚无原图",
                                                         kind=kind)
                                skip_media = True
                            else:
                                self._media_retry_clear(mkey)
                                skip_media = False
                                mhash = self._media_hash(sticker)
                                if mhash and self.history.seen("xtc", contact or "",
                                                               text, mhash):
                                    self._log("info", "[跳过] 这张图片/表情之前已转发过"
                                                      f"（内容相同），不再重复: {text[:24]!r}")
                                    skip_media = True
                                elif mhash and not label:
                                    label = mhash      # 无标签：用内容哈希当身份
                        else:
                            sticker = None
                            skip_media = False
                        if not skip_media:
                            # 异步转发。**身份标签用归一化后的绝对时间**（与补发路径完全一致）：
                            # 以前这里传的是原始标签（如 "21:55"），而补发用的是绝对标签
                            # （"09-29 21:55"）—— 两条路径算出的去重键不同，于是同一条消息
                            # 会被"补发 + 实时"各转发一次（用户报的"2 转发了两遍"就是这么来的）。
                            self._queue_forward(contact, text, label, sticker=sticker, kind=kind,
                                                display_label=self._display_time_label(contact,
                                                                                       label))
                # 媒体（图片/表情）当时没就绪的那些：到点后重试取图并补发（不依赖"还是不是最新"）
                if self._pending_media:
                    try:
                        self._retry_pending_media(root)
                    except Exception as e:  # noqa: BLE001 重试异常不影响轮询
                        self._log("warning", f"[媒体] 重试流程异常: {e}")
            except Exception as e:  # noqa: BLE001 单轮异常不致命
                self._log("warning", f"轮询异常: {e}")
            # 只补"剩下的"时间：一轮里 dump+转发可能已花 3~4 秒，再无条件 sleep
            # 一个完整间隔就变成 6 秒一轮（检测延迟白白翻倍）。
            time.sleep(max(0.0, self._poll_interval - (time.monotonic() - loop_started)))

    def _scroll_probe(self, root) -> int:
        """当前界面能不能向上翻（返回可用的位移，无法滚动时返回 0）。只读，不真的滚。"""
        try:
            vb = self.xtc.chat_view_bounds(root)
        except Exception:  # noqa: BLE001 无法读取可视区 -> 视为不能翻
            return 0
        if not vb:
            return 0
        step = int((vb[3] - vb[1]) * 0.7) or 200
        return step if step > 0 else 0

    def _in_store(self, contact: str, text: str, label: str = "") -> bool:
        """这条消息"库里有没有"（**补发**时的撞库判定，只认持久记录）。

        依次看：长期已处理表（同一文本 + 同一时间标签）-> 本地消息库 -> 回声过滤。

        **消息库必须按"文本 + 时间"判**：所有照片的文本都是"图片"、同一表情的名字也一样，
        只按文本判会把"另一条同类消息"当成已处理，于是照片永远补不出来
        （实测：10-19 21:30 的图片在 10-20 07:31 之后一直补不到）。
        没有时间标签时（`label` 为空），媒体占位文本（"图片"/"表情…"）不按文本兜底。
        注意这里**不看**短期去重表（120 秒那个）：它只是防重发的节流，
        转发失败的消息不该因为它而被当成"已处理"（否则永远不会重试）。
        """
        if self.history.seen("xtc", contact or "", text, label or ""):
            return True
        if self.echo.is_echo(text):
            return True
        if self._is_media_placeholder(text):
            # 媒体占位文本（"图片"/"表情X"）**必须按"文本 + 时间"判**：
            # 所有照片的文本都是"图片"，只按文本判会把这批照片当成"已处理"，
            # 于是照片永远补不出来（实测：10-19 21:30 的图片一直补不到）。
            epoch = self._label_epoch(label) if label else None
            if epoch is None:
                return False               # 没有时间依据 -> 当作"库里没有"，让上层去补
            return self._msg_stored(text, epoch, window=300.0)
        # 普通文本：沿用保守规则"同文本即已处理"（宁可少补一条，也不要重复输出）
        return self.msgs.seen(text, "xtc")

    @staticmethod
    def _is_media_placeholder(text: str) -> bool:
        """这条文本是不是"媒体占位"（照片的"图片"、表情的"表情X"）。"""
        t = (text or "").strip()
        return t == "图片" or t.startswith("表情")

    def _forward_backlog(self, root, contact: str) -> int:
        """从最新一条往回走，把库里没有的消息补齐；**撞到库里已有的就停**。

        用户要的逻辑：
          发完最新一条后看**上一条**：和库里一样 -> 停（不转发）；
          不一样 -> 转发，再往上看一条，直到撞上库里已有的一条为止。

        好处：弹窗挡住期间漏掉的、以及启动前积压在聊天里的消息，都会按时间顺序补齐；
        而已处理过的消息一旦撞上就停，不会无限向上滚动（也不需要记"启动时刻"来卡范围）。

        返回补发条数。命令文本与系统提示只跳过、不作为停止边界。
        """
        if not self._catchup_enabled:
            return 0
        try:
            bubbles = self.xtc._chat_bubbles(root, include_own=False)
        except Exception as e:  # noqa: BLE001 补发失败不影响正常轮询
            self._log("debug", f"补发扫描失败: {e}")
            return 0
        # 有表情/图片要做补发时：**先在界面锁内重新读一次界面**，整轮补发都用这一份新鲜快照。
        # 为什么：补发是连着发的，每转发成功一条就会往聊天里写一条"发送成功"提示把列表往上顶；
        # 用轮询那份（可能已经过去几秒）的坐标去抠图，抠到的是被移出可视区后的空白区。
        # 一次 dump 服务整轮（而不是每条媒体各 dump 一次）—— 后者在积压多条图片时白花好几秒。
        if any(it.get("sticker") or it.get("image") for it in bubbles):
            fresh = self._fresh_chat_root()
            if fresh is not None:
                try:
                    items = self.xtc._chat_bubbles(fresh, include_own=False)
                    if items:
                        bubbles, root = items, fresh
                except Exception as e:  # noqa: BLE001 解析不出来就沿用旧的
                    self._log("debug", f"[补发] 重读后的界面解析失败: {e}")
            else:
                self._log("debug", "[补发] 未能重新读界面，按现有快照取图")

        pending: list[tuple[str, str, dict | None, str]] = []
        scanned = 0
        hit_store = False          # 本屏里撞到过"库里已有"的消息（日志/调试用）
        stop_all = False           # 需要整体停止（超龄 / 超出扫描上限 / 媒体在等）
        steps: list[int] = []      # 向上滚动过的位移，最后按原路滚回底部
        # 深度扫描：普通轮询在"这一屏全是已处理的"时停；深度扫描无视已处理、一直翻到上限，
        # 用来找回被埋在已处理消息下面的漏发消息（实测前一天的图片在 3 屏之上）。
        deep = self._force_deep or (
            self._catchup_deep_every > 0
            and time.monotonic() - self._last_deep_ts >= self._catchup_deep_every)
        if deep:
            self._last_deep_ts = time.monotonic()
        screens_limit = max(self._catchup_screens, 5) if deep else self._catchup_screens
        # 深度扫描要跨屏，条数上限一并放宽（否则没翻两屏就到了上限）
        scan_limit = max(self._catchup_scan, 120) if deep else self._catchup_scan
        if self._order_hold_active():
            # 有图片/表情在等原图：这一轮先不补发任何东西（否则更新的那条会插到图前面 = 错位）
            self._log("debug", "[补发] 有图片/表情在等原图，本轮暂缓补发（保持顺序）")
            return 0
        try:
            for _screen in range(screens_limit + 1):
                screen_all_known = True
                for it in reversed(bubbles):            # 从最新往回走
                    scanned += 1
                    if scan_limit and scanned > scan_limit:
                        stop_all = True
                        break
                    text = (it.get("text") or "").strip()
                    if not text:
                        continue
                    try:
                        if self.xtc._is_system_msg(text):
                            continue                    # 送达确认这类系统提示：不转发也不当边界
                    except Exception:  # noqa: BLE001
                        pass
                    if self._xtc_cmd_prefix and text.startswith(self._xtc_cmd_prefix):
                        continue                        # 命令文本由命令流程处理
                    # 身份用**这条消息自己的时间**（`own_label`，与实时路径的 `_label_for_bubble`
                    # 同源），显示时间由 `_display_time_label` 统一决定（实时=当前时间，
                    # 补发=原始标签）。两条路径的身份必须同源同格式，否则会被各转发一次。
                    own_lbl = (it.get("own_label") or "").strip()
                    disp_src = (it.get("time_label") or "").strip()
                    ident = self._abs_time_label(own_lbl) or own_lbl
                    disp_lbl = self._display_time_label(
                        contact, ident or self._abs_time_label(disp_src) or disp_src)
                    kind = ("image" if it.get("image")
                            else ("sticker" if it.get("sticker") else "text"))
                    # 没有时间标签的**媒体**：先尝试向上翻一屏把"这条消息自己的标签"找出来
                    # （照片单独一条时，标签在可见区之外）。有了标签，身份才稳定、时间才是原始时间；
                    # 早先试过"用原图文件的写入时间当身份"，但 App 会重写缓存文件（同一张照片
                    # 出现过 2203046 与 2292962 两个文件、时间也不一样），会把同一条照片重复转发。
                    if kind in ("sticker", "image") and not ident:
                        # 只有在"确实翻得动"的时候才先去翻一屏找标签；翻不动（无法读取可视区、
                        # 或者当前环境不支持滑动）就按无标签照常补发，绝不能让照片被漏掉。
                        if len(steps) < self._catchup_screens and self._scroll_probe(root):
                            break            # 跳出本屏 -> 外层会上翻一屏再扫
                        self._log("debug", f"[补发] 这条媒体没有时间标签，按无标签补发: "
                                          f"{text[:24]!r}")
                    stale_limit = self._stale_limit()
                    if stale_limit > 0:
                        age = self._label_age(ident or disp_lbl)
                        if age is not None and age > stale_limit:
                            # 太旧的积压消息不转发：它必然排在已发过的更新消息后面（错位）。
                            # 只提示一次并注意，随后静默；再往上的消息只会更旧，本轮直接停止。
                            if not self.history.seen("xtc", contact or "", text, ident):
                                self._log("info",
                                          f"[补发] 这条是 {age / 60:.0f} 分钟前的消息，超过 "
                                          f"catchup_max_age（{stale_limit / 60:.0f} 分钟），"
                                          f"跳过不转发: {text[:24]!r}")
                                self.history.mark("xtc", contact or "", text, ident)
                            stop_all = True
                            break
                    if self._in_store(contact, text, ident):
                        # 库里有它 -> 不重复转发。但**不能就此停住向上滚动**：实测
                        # "16:38 发的两条消息到 21:48 才转发" 就是因为更新的那条已经在库里，
                        # 旧实现一撞库就 break，把更老、却从没转发过的消息永远挡在外面。
                        self._log("debug", f"[补发] 库里已有，跳过（继续往上找）: {text[:24]!r}")
                        hit_store = True
                        continue
                    if self._content_went_backwards(contact, text, ident):
                        # 同内容 + 时间倒流 = 同一条消息的标签漂移（跨零点那类），别补发
                        self._log("info", f"[补发] 同内容的消息时间倒流了（{ident}），"
                                          f"判为重复不补发: {text[:24]!r}")
                        self.history.mark("xtc", contact or "", text, ident)
                        continue
                    if self.dedup.seen(("xtc", contact or "", text, ident)):
                        # 刚试过（120 秒内：正在发的、或上次转发失败的）：本轮先跳过它，
                        # 但不当作边界，继续往上找更老的那几条
                        self._log("debug", f"[补发] 这条最近试过，先跳过: {text[:24]!r}")
                        continue
                    if self._forward_suppressed(contact, text, ident):
                        # 正在补投 / 刚转发过同一内容（标签跳变也算同一条）：不当作边界，
                        # 但这一轮不重发（旧实现没有这层，群失败会让同一条每 5 秒重转一次）。
                        self._log("debug", f"[补发] 这条在补投/内容护栏内，先跳过: {text[:24]!r}")
                        continue
                    # 补发的表情/图片也取原图：用上面那份新鲜快照（整轮共用，坐标不会过期）
                    sticker = None
                    # 类型以**气泡本身**为准（`it` 就是那条气泡的解析结果）
                    kind = ("image" if it.get("image")
                            else ("sticker" if it.get("sticker") else "text"))
                    if kind in ("sticker", "image"):
                        mkey = self._fwd_key(contact, text, ident)
                        if mkey in self._pending_media:
                            # 已经在"等媒体"队列里：交给 `_retry_pending_media`（那里掌握退避
                            # 节奏，且不依赖它是不是最新一条）。普通轮询里直接停（保持顺序）；
                            # 深度扫描里跳过它继续往上找，否则一条永远无法取得原图的旧表情会
                            # 把更上面的漏发消息全部挡住（实测：昨天的图片就是这样被挡住的）。
                            self._log("debug", f"[补发] 这条的媒体在等待重试，先跳过: {text[:24]!r}")
                            if deep:
                                continue
                            stop_all = True
                            break
                        sticker = self._capture_sticker(
                            root, text, near_epoch=self._label_epoch(disp_lbl or own_lbl),
                            match_label=ident or disp_lbl, kind=kind)
                        if sticker is not None:
                            mhash0 = self._media_hash(sticker)
                            if mhash0 and self.history.seen("xtc", contact or "",
                                                            text, mhash0):
                                self._log("info", "[补发] 这张图片/表情之前已转发过"
                                                  f"（内容相同），跳过: {text[:24]!r}")
                                hit_store = True
                                continue
                            if mhash0 and not ident:
                                # 没有时间标签：用**内容哈希**当身份。否则所有无标签图片
                                # 都共用一个空身份，第二张就会被当成"刚试过"而漏发。
                                ident = mhash0
                        if sticker is None:
                            # 媒体尚未就绪：登记等待重试，这一条先不放行（不发"图片"两个字）
                            self._media_retry_failed(mkey, contact, text, ident, disp_lbl,
                                                     "界面上还是占位图、缓存里也尚无原图",
                                                     kind=kind)
                            if deep:
                                continue     # 深度扫描：跳过它，继续往上找更早的消息
                            stop_all = True
                            break
                    pending.append((text, ident, sticker, disp_lbl, kind,
                                    self._media_hash(sticker)))
                    screen_all_known = False

                if stop_all:
                    break                    # 超龄 / 媒体在等 / 超出扫描上限
                if screen_all_known and not deep:
                    break                    # 这一屏全是已处理的 -> 已经接上了，不必再向上滚动
                if len(steps) >= self._catchup_screens:
                    break
                # 这一屏全都没在库里 -> 上面很可能还有没读到的消息（实测：图片被顶到可见区
                # 之外就永远补不到）。向上滚动一屏继续找，最后按原路滚回底部。
                try:
                    vb = self.xtc.chat_view_bounds(root)
                except Exception as e:  # noqa: BLE001 无法取得可视区就不翻
                    self._log("debug", f"[补发] 无法读取消息列表可视区，不执行向上滚动: {e}")
                    break
                step = int((vb[3] - vb[1]) * 0.7) or 200
                if not self.xtc.scroll_chat_by(vb, step):
                    break
                steps.append(step)
                fresh_root = self._fresh_chat_root()
                if fresh_root is None:
                    break
                root = fresh_root
                try:
                    bubbles = self.xtc._chat_bubbles(root, include_own=False)
                except Exception as e:  # noqa: BLE001 解析失败就停在这一屏
                    self._log("debug", f"[补发] 向上滚动后的界面解析失败: {e}")
                    break
                if not bubbles:
                    break
        finally:
            # 滚回底部：列表停在中间会让轮询无法读取新消息
            for step in reversed(steps):
                try:
                    vb2 = self.xtc.chat_view_bounds(root)
                except Exception:  # noqa: BLE001
                    vb2 = None
                if vb2:
                    self.xtc.scroll_chat_by(vb2, -step)
        if steps:
            self._log("debug", f"[补发] 为了找更早的消息向上滚动了 {len(steps)} 屏（已滚回底部）")

        if not pending:
            return 0
        pending.reverse()                           # 变成 旧 -> 新 的顺序转发
        if self._catchup_max and len(pending) > self._catchup_max:
            self._log("warning", f"[补发] 待补 {len(pending)} 条，超过上限 {self._catchup_max}，"
                                 f"先补最早的 {self._catchup_max} 条（下一轮继续）")
            pending = pending[:self._catchup_max]
        self._log("info", f"[补发] 有 {len(pending)} 条消息库里没有，按时间顺序补发")

        sent = 0
        for text, label, sticker, disp, kind, mhash in pending:
            self._log("info", f"[收到小天才消息] 来源={self._xtc_source(contact)} "
                              f"时间={disp or label or '(无)'} 内容={text!r}（补发）")
            # 异步转发：不阻塞读屏（见 _queue_forward）
            self._queue_forward(contact, text, label, sticker=sticker,
                                display_label=disp, kind=kind, media_hash=mhash)
            sent += 1
        return sent

    def _estimate_time_label(self, max_gap: float | None = None) -> str:
        """消息没有时间标签时的兜底：用**上一条已入库消息的时间**（`MM-DD HH:MM`）。

        为什么需要：App 只在"一组消息的第一条"上方画时间标签，那一行滚出屏幕后，组内其余
        消息就没有任何标签了 —— 旧实现直接退化成"当前时间"，补发旧消息时就会写成**转发
        时刻**（实测：22:04 发的"噢"在 22:20 被补发，显示成了 22:20）。
        消息是严格按时间顺序处理的，所以"上一条已入库消息的时间"是个很接近的真实时间；
        窗口默认 12 小时（`xiaotiancai.label_fallback_max`）—— 桥接停了半天再补发时，
        宁可用"上一条真实消息的时间"，也不要写成补发时刻（实测：10-19 21:30 的消息在
        10-20 07:31 被补发，被写成了 07:31）。
        """
        if max_gap is None:
            max_gap = self._label_fallback_max
        try:
            recent = self.msgs.recent(1)
        except Exception:  # noqa: BLE001 无法取得就用旧行为
            return ""
        if not recent:
            return ""
        try:
            t = float(recent[-1].get("t") or 0)
        except (TypeError, ValueError):
            return ""
        if t <= 0 or abs(time.time() - t) > max_gap:
            return ""
        return datetime.fromtimestamp(t).strftime("%m-%d %H:%M")

    def _fresh_chat_root(self):
        """在界面锁内重新读一次界面（无法取得锁就等一小会儿，超时返回 None）。

        补发取图用它：发送线程（点输入框/打字/点发送/写"发送成功"提示）拿的是同一把锁，
        所以"读界面 -> 抠图"这一小段界面不会被人动。
        """
        got_lock = self._op_lock.acquire(timeout=self._media_lock_timeout)
        try:
            try:
                return self.xtc.adb.dump_ui(retries=2, delay=0.3)
            except Exception as e:  # noqa: BLE001 无法读取就返回 None，调用方沿用旧快照
                self._log("debug", f"[补发] 重新读界面失败: {e}")
                return None
        finally:
            if got_lock:
                self._op_lock.release()

    def _queue_forward(self, contact: str, text: str, label: str,
                       sticker: dict | None = None, display_label: str = "",
                       kind: str = "", media_hash: str = "") -> None:
        """把"小天才 -> QQ"的转发丢给工作线程，立刻返回。

        为什么异步：插件要等 QQ 侧真实发送结果才回包（最长 30 秒），同步做的话
        轮询线程会被卡住，读屏/检测跟着变慢（转发越快，漏消息窗口也越小）。
        这里立刻 short-term 去重，避免下一轮把同一条再入队。

        label：**身份标签**（归一化的绝对时间，实时与补发两条路径必须同源同格式）。
        display_label：显示用的标签（App 的分组标签，显示更准）；留空则用 label。
        sticker：表情图（{data, kind, animated, source}）。**必须在轮询线程里取**——
        那一刻界面快照/气泡位置才准、缓存里刚写进来的文件也还在；没有它就按文字发。
        kind：`sticker`/`image`/`text`（由**气泡本身**判定）。手打的、内容恰好以"表情"
        开头的文本必须传 text，否则会被当成表情包发出一条没有图的"空气消息"。
        """
        self.dedup.mark(("xtc", contact or "", text, label or ""))
        # 注意：内容级护栏（_recent_fwd_seen）**不在这里**打点 —— 打点必须发生在
        # "真的要发出去"那一刻（见 _do_forward_job），否则刚入队的这条会被自己挡掉。
        self._job_queue.put(("forward", contact, text, label or "", sticker,
                             display_label or label or "", kind or "", media_hash or ""))

