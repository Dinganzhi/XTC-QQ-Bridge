# -*- coding: utf-8 -*-
"""媒体处理：图片/表情等待原图、拍照类与表情包取图（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403


class MediaMixin:
    """媒体处理：图片/表情等待原图、拍照类与表情包取图"""

    def _media_retry_decision(self, key: tuple) -> str:
        """返回 'now'（可以试取）/ 'wait'（还没到点）/ 'give_up'（等太久了，按文字发）。"""
        st = self._pending_media.get(key)
        if st is None:
            return "now"
        now = time.monotonic()
        # 表情贴纸不依赖缓存原图：图上就是图上，等再久也不会变，因此只给很短的窗口，
        # 避免"一条表情把后面消息全阻塞、11 分钟后才发出一个[表情]"。
        if self._is_sticker_media(st.get("kind"), st.get("text")):
            if (now - float(st.get("first_ts") or now) > 30.0
                    or int(st.get("tries") or 0) >= 3):
                return "give_up"
        elif (now - float(st.get("first_ts") or now) > self._media_retry_max_wait
                or int(st.get("tries") or 0) >= self._media_retry_max):
            return "give_up"
        if now < float(st.get("next_ts") or 0.0):
            return "wait"
        return "now"

    @staticmethod
    def _is_sticker_media(kind: str | None, text: str | None) -> bool:
        """这条待取图的消息是不是表情贴纸（贴纸不需要等缓存原图）。"""
        if (kind or "").strip().lower() == "sticker":
            return True
        return (text or "").strip().startswith("表情")

    def _media_retry_failed(self, key: tuple, contact, text: str, label: str,
                            disp: str, reason: str = "", kind: str = "") -> None:
        """媒体无法取得：登记"稍后重试"，本轮**暂不转发**（避免 QQ 仅收到占位文本）。"""
        now = time.monotonic()
        st = self._pending_media.get(key)
        if st is None:
            while len(self._pending_media) >= 20:      # 只留最近若干条
                self._pending_media.pop(next(iter(self._pending_media)), None)
            st = {"tries": 0, "first_ts": now, "next_ts": now}
            self._pending_media[key] = st
        st["tries"] = int(st.get("tries") or 0) + 1
        if self._is_sticker_media(kind, text):
            # 贴纸：固定 6 秒后重试，最多 3 次（合计约 20 秒）；等不到就按文字发
            gap = 6.0
            max_tries = 3
        else:
            gap = min(300.0, self._media_retry_base * (2 ** (st["tries"] - 1)))
            max_tries = self._media_retry_max
        st["next_ts"] = now + gap
        st.update({"contact": contact, "text": text, "label": label, "disp": disp,
                   "kind": kind or ""})
        self._log("info", f"[媒体] {text[:24]!r} 的原图尚未就绪（{reason or '缓存/界面里都还没有'}）；"
                          f"{int(gap)} 秒后重试（第 {st['tries']}/{max_tries} 次），"
                          "暂不发送文字，避免仅发出占位文本")

    def _media_retry_clear(self, key: tuple) -> None:
        self._pending_media.pop(key, None)

    def _retry_pending_media(self, root) -> None:
        """到点后重试"媒体尚未就绪"的消息：拿到图就补发，等太久就退回发文字。

        为什么放在轮询里、而且**不依赖"它还是不是最新一条"**：App 先画出"图片"占位气泡、
        原图可能几分钟后才落盘（实测 22:12 检测到、22:19:44 原图文件才出现）。这期间要是
        用户又发了别的消息，光标/补发都轮不到它了 —— 这里按**消息自己的时间标签**去找那条
        气泡，找到就取图补发（顺序上它比后来的消息早，回 QQ 时会带自己的时间抬头）。
        """
        if not self._pending_media:
            return
        for key, st in list(self._pending_media.items()):
            contact = st.get("contact")
            text = st.get("text") or ""
            label = st.get("label") or ""
            disp = st.get("disp") or label
            kind = st.get("kind") or ""
            decision = self._media_retry_decision(key)
            if decision == "wait":
                continue
            sticker = None
            if decision == "now":          # give_up 时不再取图，直接按文字发
                got_lock = self._op_lock.acquire(timeout=self._media_lock_timeout)
                try:
                    sticker = self._capture_sticker(
                        root, text, near_epoch=self._label_epoch(disp or label),
                        match_label=label or disp, kind=kind)
                except Exception as e:  # noqa: BLE001 取图失败继续等下一轮
                    self._log("debug", f"[媒体] 重试取图失败: {e}")
                finally:
                    if got_lock:
                        self._op_lock.release()
            if sticker is None and decision != "give_up":
                self._media_retry_failed(key, contact, text, label, disp,
                                         "重试时仍未取得原图/截图", kind=kind)
                continue
            self._media_retry_clear(key)
            self._log("info", f"[媒体] {'拿到图，补发' if sticker else '等太久了，按文字转发'}: "
                              f"{text[:24]!r}")
            self._queue_forward(contact, text, label, sticker=sticker,
                                display_label=disp, kind=kind)

    def _label_age(self, label: str) -> float | None:
        """某个时间标签距今多少秒（解析不出来返回 None）。"""
        epoch = self._label_epoch(label) if label else None
        if not epoch:
            return None
        return max(0.0, time.time() - float(epoch))

    def _display_time_label(self, contact, label: str) -> str:
        """决定 QQ 那条消息抬头显示的**时间**（身份/去重不受影响）。

        用户要求（10-20）：**实时消息用真实时间；补发/旧消息用完全原始的时间**，
        绝不能把"补发一条 21:30 的消息"写成"转发时的 07:31"。

        规则：
          1. 标签距今 <= `label_now_window`（默认 300 秒）= 这条就是"刚刚发生"的 ——
             App 的分组标签只画在组首，组内后续消息会**早 1~2 分钟**（实测），
             所以这里用**当前时间**（轮询间隔只有 2~4 秒，误差远小于标签的偏差）；
          2. 更早的标签 = 补发/积压消息 -> 用**原始标签**（绝对化成 `MM-DD HH:MM`），
             不再替换成当前时间；
          3. 完全没有标签 -> 用"上一条已入库消息的时间"（见 `_estimate_time_label`）。
        **身份（去重/历史）仍然用 App 标签**，只有显示时间变 —— 否则身份会跟着时间漂移，
        又会出现"同一条被反复转发"的老问题。
        """
        now = time.time()
        ident = self._abs_time_label(label) or (label or "")
        age = self._label_age(ident)
        if age is not None and age <= self._label_now_window:
            return datetime.fromtimestamp(now).strftime("%m-%d %H:%M")
        if age is not None and age <= self._label_recent_window:
            # App 只给一组消息的第一条画时间标签，同组后续消息共用组首时间（实测
            # 08:19~08:23 的 5 条消息标签全是 08:19）。设备侧没有逐条时间可用
            # （节点、dumpsys window、notification、应用数据库都查过），
            # 因此这段时间内用"观测时间"显示，避免整组都写成组首时间；
            # 更早的消息仍用原始标签（补发旧消息绝不打上转发时刻）。
            return datetime.fromtimestamp(now).strftime("%m-%d %H:%M")
        if ident:
            return ident                       # 原始时间（绝对化），不改成"现在"
        # 完全没有标签、也查不到"上一条已入库消息"时，只能给当前时间
        return self._estimate_time_label() or datetime.now().strftime("%m-%d %H:%M")

    def _fwd_key(self, contact, text: str, label: str) -> tuple:
        """转发身份键（与实时/补发两条路径共用，必须同源）。"""
        return ("xtc", contact or "", text, label or "")

    def _heartbeat_reconnect(self, now: float) -> bool:
        """心跳发现 ADB 断连时的重连 + 退避。返回 True=本轮继续轮询，False=跳过本轮。

        退避的意义（实机踩过）：设备的 transport 卡成 offline 时，`adb connect` 只会回
        "already connected to ..."，重连实际**救不回来**；旧实现每 10 秒就重执行一次
        "12 个候选端口 × 2 个 host"的连接尝试，日志重复输出且空等。现在失败一次就退避
        （10/20/30…最多 60 秒），并且只在第一次与每次失败时各喊一声。
        """
        if now < self._next_reconnect_ts:
            self._log("debug", "ADB 仍未恢复，等退避时间到再试")
            return False
        if self._reconnect_fails == 0:
            self._log("warning", "ADB 断连，尝试重连...")
        try:
            self.adb.ensure_connected()
        except Exception as e:  # noqa: BLE001
            self._reconnect_fails += 1
            backoff = min(60.0, 10.0 * self._reconnect_fails)
            self._next_reconnect_ts = now + backoff
            self._log("error", f"重连失败（连续 {self._reconnect_fails} 次）: "
                               f"{str(e).splitlines()[0][:160]}；{backoff:.0f} 秒后再试")
            self._hint_reconnect()
            return False
        self._log("info", "ADB 已重连" if self._reconnect_fails == 0
                          else f"ADB 已重连（第 {self._reconnect_fails + 1} 次尝试）")
        self._reconnect_fails = 0
        self._next_reconnect_ts = float("-inf")
        return True

    def _hint_reconnect(self) -> None:
        """Android 容器 反复断网时的提示（只提示一次）。"""
        if self._reconnect_hinted:
            return
        self._reconnect_hinted = True
        if os.name != "nt":
            return
        self._log("warning",
                  "若使用 Android 容器 且经常断网，可先在 容器 设置里重启容器"
                  "请在容器的系统设置里执行修复后重连，桥接会自动重新连接 ADB 并继续")

    def _typed_body(self, text: str, kind: str = "") -> str:
        """给转发内容加上**类型标记**，让表情和照片一眼分得开（用户要求）。

        * 表情（贴纸，kind=sticker）-> `[表情] 开心`（名字无法读取时就是 `[表情]`）
        * 照片（图片消息，kind=image）-> `[图片]`
        * 文字消息（kind=text 或空）**原样不动** —— 包括手打的内容恰好以"表情"开头的文本：
          用户实测那种消息被标成了"[表情]"、还没有图，看起来像"空气消息"。

        为什么要标：贴纸无法取得原图时会退化成**气泡截图**（一张 png），看起来和照片没区别，
        用户分不清哪条是表情、哪条是照片（实测 15:24 那条）。标记只加在**发出去的文字**上，
        去重/历史的身份仍然是原始 `text`，不会影响判重。
        """
        t = (text or "").strip()
        if not t:
            return t
        k = (kind or "").strip().lower()
        if not k:
            # 没有类型信息时退回旧推断（外部脚本/老调用方补的 kind 都是空）
            if t == getattr(self.xtc, "IMAGE_TEXT", "图片"):
                k = "image"
            elif t.startswith("表情"):
                k = "sticker"
            else:
                k = "text"
        if k == "image":
            return "[图片]"
        if k == "sticker":
            name = t[len("表情"):].strip() if t.startswith("表情") else t
            # 有的贴纸名字自带扩展名（实机：'表情弹吉他.png'）—— 显示时去掉
            name = re.sub(r"\.(png|gif|webp|jpe?g|apng)$", "", name, flags=re.I).strip()
            return f"[表情] {name}" if name else "[表情]"
        return t

    def _forward(self, contact, text: str, time_label: str = "",
                 sticker: dict | None = None,
                 only: list | None = None, kind: str = "") -> tuple[bool, list, list]:
        """转发到 QQ 目标。返回 (是否全部成功, 成功的目标, 失败的目标)。

        sticker：表情图（{data, kind, animated}）。给了就发"图片（+说明文字）"，
        失败自动退回纯文字。**单向**：只有 小天才 -> QQ 走图片，QQ -> 小天才 依旧是文字。
        only：只发这些目标（补投计划用它**只重试失败的目标**，绝不碰已经成功的那几个）。
        kind：消息类型（sticker/image/text）；只有它才是 sticker/image 才加类型标记。
        """
        targets = self._qq_targets()
        if not targets:
            self._log("info", f"[占位] 收到小天才消息（未配置 QQ 目标，仅打印）: {text}")
            return True, [], []
        if only is not None:
            wanted = {(t, i) for t, i in only}
            targets = [t for t in targets if t in wanted]
            if not targets:
                return True, [], []
        # 转发格式：[日期时间] [本地配置昵称] 消息内容。
        # 时间优先取小天才 App 内该消息的日期标签（如 "昨天 23:42"、"8月30日"）；
        # 只有时分（当天消息）时补当天日期；无标签时用当前时间。
        time_str = self._format_xtc_time(time_label)
        nickname = self._display_name(contact)
        message = f"[{time_str}] [{nickname}] {self._typed_body(text, kind)}"
        image_b64 = ""
        image_size = 0
        if sticker and sticker.get("data"):
            try:
                image_b64 = base64.b64encode(sticker["data"]).decode("ascii")
                image_size = len(sticker["data"])
            except Exception as e:  # noqa: BLE001 编码失败就退回文字
                self._log("warning", f"表情图编码失败（按文字转发）: {e}")
                image_b64 = ""
        ok_all = True
        queued = False
        sent: list = []
        failed: list = []
        for target_type, target_id in targets:
            why = ""
            try:
                if image_b64:
                    send_image = getattr(self.forwarder, "send_image", None)
                    if send_image is not None:
                        caption = message if self._emoji_caption else ""
                        ok, why = send_image(target_type, target_id, image_b64, caption)
                    else:
                        self._log("warning", "转发器不支持图片，改发文字"
                                             "（插件需一并更新）")
                        ok, why = self._send_text(target_type, target_id, message)
                else:
                    ok, why = self._send_text(target_type, target_id, message)
            except Exception as e:  # noqa: BLE001
                self._log("error", f"转发异常({target_type}:{target_id}): {e}")
                ok, why = False, f"{type(e).__name__}: {e}"
            if ok and why == "queued":
                # 插件只把消息排进队列（事件循环还没起来）：没真的发出去，
                # 不能报"转发成功"，更不能发"发送成功"的送达确认
                queued = True
                sent.append((target_type, target_id))   # 已交给插件，重发会变成两条
                self._log("warning", f"[转发未确认] {target_type}:{target_id} "
                                     "插件刚启动，消息只是排队（未确认已发出）")
                continue
            shown = message + (f"（+{sticker.get('label') or '表情图'} {image_size} 字节）"
                               if image_b64 else "")
            if ok:
                sent.append((target_type, target_id))
                self._log("info", f"[转发成功] {target_type}:{target_id} <- {shown}")
            else:
                failed.append((target_type, target_id))
                self._log("error", f"[转发失败] {target_type}:{target_id} <- {shown}"
                                   + (f"  原因: {why}" if why else ""))
                ok_all = False
        if ok_all and not queued:
            # 标记原文 + 格式化消息：多实例/重启后也不会再转发同一条
            # 媒体占位文本（"图片"/"表情X"）不能记：所有照片的文本都是"图片"，
            # 一旦记进回声表，后续每张照片都会被当成"自己发的"而跳过。
            if not self._is_media_placeholder(text):
                self.echo.mark(text)
            self.echo.mark(message)
            # 小天才侧送达确认（发送成功：<内容>）走**低优先级任务**，不堵住后续发送
            self._queue_confirm_xtc(message)
        elif ok_all and queued:
            # 已交出去但没确认：同样记历史避免重复，但不发"发送成功"（不撒谎）
            # 媒体占位文本（"图片"/"表情X"）不能记：所有照片的文本都是"图片"，
            # 一旦记进回声表，后续每张照片都会被当成"自己发的"而跳过。
            if not self._is_media_placeholder(text):
                self.echo.mark(text)
            self.echo.mark(message)
            self._log("info", "小天才侧送达确认已跳过：本次转发未确认（插件排队中）")
        return ok_all, sent, failed

    def _send_text(self, target_type, target_id, message: str) -> tuple:
        """发纯文字（兼容只有 send() 的旧转发器）。返回 (ok, why)。"""
        send_fn = getattr(self.forwarder, "send_detail", None)
        if send_fn is not None:
            return send_fn(target_type, target_id, message)
        return bool(self.forwarder.send(target_type, target_id, message)), ""

    def _format_xtc_time(self, time_label: str) -> str:
        """把 App 内的时间标签转成**绝对** `MM-DD HH:MM`。

        为什么必须转绝对：App 对**同一条消息**的标签会随日期变化 ——
        当天显示 `16:18`，第二天变成 `昨天 16:18`，再往后可能变成 `09-25 16:18`。
        原样输出就会出现"转发里写着昨天"这种相对时间；拿它当消息身份更会导致
        同一条消息隔天被当成新消息重复转发。
        """
        return self._abs_time_label(time_label) or datetime.now().strftime("%m-%d %H:%M")

    def _abs_time_label(self, time_label: str, now: datetime | None = None) -> str:
        """把任意 App 时间标签统一成绝对 `MM-DD HH:MM`；解析不出来返回 ""。

        支持：`16:18`（今天）/ `今天 16:18` / `昨天 16:18` / `前天 16:18` /
        `星期一 16:18` / `8月30日 16:18` / 已经是绝对的 `09-25 16:18`、`2026-09-25 16:18`。
        """
        epoch = self._label_epoch(time_label, now)
        if epoch is None:
            return ""
        try:
            return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):  # noqa: BLE001 时间戳异常就当解析不出来
            return ""

    def _qq_targets(self) -> list[tuple[str, str]]:
        """转发目标列表 [(type, id)]；qq_private/qq_group 支持单个字符串或列表。"""
        t = self.cfg.get("target") or {}
        targets: list[tuple[str, str]] = []
        for key, mtype in (("qq_private", "private"), ("qq_group", "group")):
            v = t.get(key)
            if not v:
                continue
            items = v if isinstance(v, (list, tuple)) else [v]
            for it in items:
                s = str(it).strip()
                if s:
                    targets.append((mtype, s))
        return targets

    def _display_name(self, contact) -> str:
        """小天才联系人 -> 本地配置的显示昵称。映射优先级：
        target.nicknames[联系人] -> target.default_nickname -> App 原始名。
        聊天窗口模式 contact 可能为 None，此时按 target.xtc_contact 查映射。"""
        t = self.cfg.get("target") or {}
        nicknames = t.get("nicknames") or {}
        name = contact or t.get("xtc_contact", "") or ""
        if name and name in nicknames:
            return str(nicknames[name])
        default = t.get("default_nickname")
        if default:
            return str(default)
        return name

