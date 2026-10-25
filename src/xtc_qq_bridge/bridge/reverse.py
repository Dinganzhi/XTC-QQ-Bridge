# -*- coding: utf-8 -*-
"""反向链路：QQ → 小天才 的命令执行与送达确认（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403


class ReverseMixin:
    """反向链路：QQ → 小天才 的命令执行与送达确认"""

    def qq_sender_allowed(self, qq: str, group: str = "") -> bool:
        """QQ->小天才 接收白名单（config.yaml -> webhook）：
        - 私聊消息：QQ 号必须在 webhook.allow_from 列表里；
        - 群聊消息：群号必须在 webhook.allow_groups 列表里；
        - 对应列表为空 = 该类消息全部拒绝（严格白名单）。
        """
        wh = self.cfg.get("webhook") or {}
        if group:
            allow = {str(g) for g in (wh.get("allow_groups") or [])}
            ok = str(group) in allow
            if not ok:
                self._log("info", f"群聊 {group} 不在白名单（webhook.allow_groups），已忽略")
            return ok
        allow = {str(u) for u in (wh.get("allow_from") or [])}
        ok = str(qq) in allow
        if not ok:
            self._log("info", f"私聊 {qq} 不在白名单（webhook.allow_from），已忽略")
        return ok

    def forward_to_xiaotiancai(self, text: str, user_id: str = "",
                               group_id: str = "", request_id: str = "") -> bool:
        """QQ -> 小天才：入队（FIFO 保证多消息按顺序处理），由单工作线程串行执行。"""
        if not text:
            return False
        where = f"群 {group_id}" if group_id else (f"私聊 {user_id}" if user_id else "未知会话")
        # 收到就打印：便于在控制台确认 QQ 命令/消息真的到达了桥接（用户报告"无法看到"）
        self._log("info", f"[收到QQ命令] 来源={where} 内容={text!r}"
                          + (f" request_id={request_id}" if request_id else "")
                          + f"（队列中 {self._job_queue.qsize() + self._fast_queue.qsize()}"
                            " 条待处理）")
        # 让轮询先停一轮 dump，把 adb/操作锁让给发送（见 _poll_loop 开头的让路逻辑）
        if not self._send_pending:
            self._send_pending_ts = time.monotonic()
        self._send_pending = True
        # 走**发送专用队列**：工作线程优先处理它，不排在转发/历史/回执后面（见 _next_job）
        self._fast_queue.put(("send", text, user_id, group_id, request_id))
        return True

    def _blind_send_allowed(self, contact: str) -> bool:
        """能不能走"预输入"：最近一次轮询确认过**就是目标聊天页**（含标题校验）。

        为什么要这个门闩：预输入是按**缓存坐标**盲点输入框/发送按钮，
        只有"确实在目标聊天页"时才安全。条件：
          * 近 30 秒内轮询判过 STATE_CHAT；
          * 那次读到的聊天标题就是目标联系人（防止盲点把消息发到别的聊天里）；
          * 上次成功发送留下了坐标缓存（`blind_send_ready`）。
        """
        if time.monotonic() - self._chat_ok_ts > 30.0:
            return False
        title = (self._chat_ok_title or "").strip()
        want = (self._display_name(contact) or contact or "").strip()
        if not title or not want:
            return False
        if want != title and want not in title and title not in want:
            return False
        try:
            if not self.xtc.blind_send_ready():
                return False
        except Exception:  # noqa: BLE001 判断失败就走稳妥流程
            return False
        return True

    def _push_text_to_xtc(self, text: str, tag: str = "QQ->小天才") -> bool:
        """把一段文字送进小天才聊天窗口（能用"预输入"就用，失败退回稳妥流程）。

        tag 只用于日志前缀（QQ->小天才 / 送达确认），两条路径共用同一套发送逻辑，
        所以"回执"也能享受预输入的 ~1 秒速度，而不是每次都走 2~3 次 dump 的稳妥流程。
        """
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml -> target.xtc_contact")
            return False
        ok = False
        skip_safe = False
        # ① "预输入"：直接按缓存坐标点输入框 + 广播注入 + 点发送，
        #    **不等轮询那次 dump、也不抢操作锁** —— 文字 ~1 秒内就出现在输入框里。
        #    复核放在后面（要 dump），失败再退回稳妥流程。
        if self._blind_send_allowed(contact):
            t0 = time.monotonic()
            staged, why = self.xtc.begin_blind_send(text)
            if staged:
                self._log("info", f"[{tag}] 已预输入（{time.monotonic() - t0:.1f}s），"
                                  "正在复核…")
                with self._op_lock:
                    ok, why, retryable = self.xtc.end_blind_send(text)
                if ok:
                    self._log("info", f"[{tag}] 预输入后发送已复核通过"
                                      f"（总 {time.monotonic() - t0:.1f}s）")
                else:
                    skip_safe = not retryable
                    self._log("warning" if skip_safe else "info",
                              f"[{tag}] 预输入发送未确认（{why}）"
                              + ("，且不宜重发，按失败上报" if skip_safe else "，改用稳妥流程"))
            else:
                self._log("debug", f"[{tag}] 预输入未启用（{why}），走稳妥流程")
        # ② 稳妥流程（未走预输入 / 预输入未送达且可安全重发时）
        if not ok and not skip_safe:
            with self._op_lock:
                in_chat = self.xtc.open_chat(contact)
                ok = in_chat and self.xtc.send_message(text)
            if not in_chat:
                self._log("error", f"[{tag}] 未能进入小天才聊天窗口，未发送")
        return ok

    def _do_send_job(self, text: str, user_id: str, group_id: str, request_id: str) -> None:
        """实际执行 QQ->小天才 发送 + 送达确认（工作线程内，按入队顺序）。"""
        # 媒体占位文本（"图片"/"表情X"）不能记：所有照片的文本都是"图片"，
        # 一旦记进回声表，后续每张照片都会被当成"自己发的"而跳过。
        if not self._is_media_placeholder(text):
            self.echo.mark(text)
        contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
        if not contact:
            self._log("error", "反向转发需要 config.yaml -> target.xtc_contact")
            return
        self._log("info", f"[QQ->小天才] 开始发送: {text[:80]!r}")
        t_all = time.monotonic()
        ok = False
        try:
            ok = self._push_text_to_xtc(text, tag="QQ->小天才")
        except Exception as e:  # noqa: BLE001 单条发送异常不能让工作线程退出
            self._log("warning", f"[QQ->小天才] 发送异常: {e}")
            ok = False
        finally:
            # 队列里还有待发的就继续让路（_send_pending_ts 不刷新，最长 30 秒兜底）
            self._send_pending = not (self._job_queue.empty() and self._fast_queue.empty())
        # 带上总耗时：以后"到底慢在哪一步"看这一行就够了（输入阶段/复核阶段也各有日志）
        self._log("info" if ok else "error",
                  f"[QQ->小天才] {'发送成功' if ok else '发送失败'}"
                  f"（总 {time.monotonic() - t_all:.1f}s）: {text[:80]!r}")
        if ok:
            # 记录到长期历史：即使重启，这条消息也不会被当作"新消息"转发回 QQ
            self.history.mark("qq2xtc", text)
            self._archive_qq_send(text, user_id=user_id, group_id=group_id)
        # 命令类文本（/小天才 …）的"结果"由命令任务自己写回小天才聊天，
        # 不再向 QQ 发"发送成功"确认，避免误导。
        is_cmd_text = bool(self._xtc_cmd_prefix
                           and text.startswith(self._xtc_cmd_prefix))
        if self._confirm_delivery() and (user_id or group_id) and not is_cmd_text:
            result_msg = ("发送成功：" if ok else "发送失败：") + text
            if request_id:
                try:
                    ok_r = self.forwarder.reply_result(request_id, result_msg)
                    self._log("info" if ok_r else "error",
                              f"[送达确认] {result_msg}（引用+@ 回传{'成功' if ok_r else '失败'}）")
                except Exception as e:  # noqa: BLE001
                    self._log("warning", f"送达确认回传异常: {e}")
            else:
                self._send_confirm(user_id, group_id, result_msg)

    def _xtc_source(self, contact: str = "") -> str:
        """小天才（手表）侧来源：手表/家长侧在 App 内说的都归这里。

        注意：标签里不要出现 GBK 无法表示的符号（某些 emoji / 特殊符号）——中文 Windows 控制台
        会因此整条日志丢失（utils/logger.py 已做兜底，这里也不主动引入）。
        """
        who = self._display_name(contact) if contact else ""
        return "手表" + (f"-{who}" if who else "")

    @staticmethod
    def _qq_source(user_id: str = "", group_id: str = "") -> str:
        """QQ 侧来源：QQ群 <群号> / QQ私聊 <QQ号>。"""
        if group_id:
            return f"QQ群 {group_id}"
        if user_id:
            return f"QQ私聊 {user_id}"
        return "QQ"

    def _archive_qq_send(self, text: str, user_id: str = "", group_id: str = "") -> None:
        """QQ -> 小天才 发送成功后归档到本地消息库（带来源标签）。
        - 发送的整条消息是插件格式 `[MM-DD HH:MM] [QQ昵称] 内容` -> 拆出昵称与内容；
        - 命令文本（/小天才 …）与系统提示不入库。"""
        m = re.match(r"^\[(\d{2}-\d{2} \d{2}:\d{2})\] \[(.+?)\] (.*)$", text)
        if m:
            sender, content = m.group(2), m.group(3)
        else:
            sender, content = "QQ", text
        content = (content or "").strip()
        if not content:
            return
        if any(content.startswith(p) for p in self._system_msg_prefixes()):
            return
        if self._xtc_cmd_prefix and content.startswith(self._xtc_cmd_prefix):
            return
        self.msgs.append("qq", sender, content,
                         source=self._qq_source(user_id, group_id),
                         source_id=str(group_id or user_id or ""))

    def _confirm_delivery(self) -> bool:
        return bool((self.cfg.get("target") or {}).get("confirm_delivery", True))

    def _send_confirm(self, user_id: str, group_id: str, message: str) -> None:
        """向 QQ 发送方发送送达确认（无 request_id 时的降级路径，走插件 /api/forward）。"""
        try:
            if group_id:
                ok = self.forwarder.send("group", str(group_id), message)
            elif user_id:
                ok = self.forwarder.send("private", str(user_id), message)
            else:
                return
            self._log("info" if ok else "error",
                      f"[送达确认] {message} -> {group_id or user_id}（{'成功' if ok else '失败'}）")
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"送达确认发送异常: {e}")

    def _confirm_xtc_delivery(self, message: str) -> None:
        """小天才消息转发到 QQ 成功后，在小天才聊天内回复「发送成功：<转发内容>」。
        确认消息以"发送成功"开头且为家长侧消息（右侧气泡），读取路径按前缀过滤，不会循环转发。

        这段是**低优先级任务**（见 `_queue_confirm_xtc`）：要往聊天页打字，占界面锁好几秒，
        所以排在用户新发的 QQ 消息后面做；能用预输入就用（~1 秒）。"""
        if not self._confirm_delivery():
            return
        try:
            contact = (self.cfg.get("target") or {}).get("xtc_contact", "")
            # 能用预输入时不必先 dump 一次查"在不在聊天页"（门闩已经保证刚确认过聊天页）
            if not self._blind_send_allowed(contact) and not self.xtc.is_in_chat():
                return  # 不在聊天页就不打扰
            confirm_text = "发送成功：" + message
            self.echo.mark(confirm_text)
            if self._push_text_to_xtc(confirm_text, tag="送达确认"):
                self._log("info", f"[送达确认] 已在小天才聊天回复 {confirm_text}")
            else:
                self._log("warning", f"[送达确认] 未能在小天才聊天回复 {confirm_text}")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"小天才送达确认跳过: {e}")

