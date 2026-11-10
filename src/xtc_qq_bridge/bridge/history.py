# -*- coding: utf-8 -*-
"""历史消息与 xtc 侧命令（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403
from ..utils import timelabel


class HistoryMixin:
    """历史消息与 xtc 侧命令"""

    def fetch_xtc_history(self, count: int = 20, request_id: str = "",
                          into_chat: bool = False, source: str = "") -> str:
        """读取小天才最近对话历史（入队，由工作线程串行执行，保证与发送顺序）。
        - QQ 侧：request_id 回传插件，插件在原会话引用+@ 回复；
        - xtc 侧：into_chat=True 时结果写进小天才聊天；
        - source：可选来源过滤（手表 / QQ私聊 <号> / QQ群 <群号>），留空为全部。
        返回 'queued'。count 自动夹到 1..100。"""
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        self._job_queue.put(("history", count, request_id or "", bool(into_chat),
                             str(source or "")))
        return "queued"

    # ------------------------------------------------------------------ 历史图片取回
    def fetch_xtc_media(self, code: str, request_id: str = "") -> str:
        """`/小天才 查看图片 <码>`：把历史里那张图**重新发回 QQ**。

        分成两步（都在工作线程里做，避免和发送抢界面/顺序）：
          1. 用引用码在消息库里找到对应条目，再从 media_store 读出原始字节；
          2. 通过转发器把这张图发到配置的 QQ 目标（与当初转发时同一批目标）。
        返回 'queued'；码无效时也回 'queued'（由工作线程回复"找不到"给用户）。
        """
        self._job_queue.put(("history_media", str(code or "").strip(), request_id or ""))
        return "queued"

    def _do_history_media_job(self, code: str, request_id: str = "") -> None:
        """工作线程内：查出历史图片并回发 QQ / 写回小天才聊天。"""
        reply = ""
        entry = None
        data = b""
        try:
            entry = self.msgs.find_media(code) if code else None
            if entry is not None:
                store = getattr(self, "media_store", None)
                data = store.load(str(entry.get("media") or "")) if store else b""
        except Exception as e:  # noqa: BLE001 查找失败按"找不到"处理
            self._log("warning", f"[查看图片] 查找失败: {e}")
        if entry is None or not data:
            why = ("没有提供图片编码" if not code
                   else ("历史里找不到这个编码（可能是编码抄错，或该图已被容量上限清理）"
                         if entry is None else "图片文件已不在本地（可能已被清理）"))
            reply = f"查看图片失败：{why}"
            self._log("info", f"[查看图片] 未命中: code={code!r} {why}")
            self._reply_history_media_result(reply, request_id)
            return
        # 复原当时的抬头信息（时间 / 昵称 / 类型），这样 QQ 里看起来和当初那条一致
        try:
            dt = datetime.fromtimestamp(float(entry.get("t") or 0))
            when = f"{dt:%m-%d %H:%M}"
        except (TypeError, ValueError, OSError):
            when = ""
        text = str(entry.get("text") or "")
        kind = "image" if not text.startswith("表情") else "sticker"
        body = self._typed_body(text, kind)
        nickname = self._display_name(entry.get("sender") or "")
        caption = f"[{when}] [{nickname}] {body}" if when else f"[{nickname}] {body}"
        ok = self._resend_history_media(data, caption, kind)
        if ok:
            reply = f"已重新发送历史图片：{caption}（#{code}）"
            self._log("info", f"[查看图片] 已重发 {caption}（{len(data)} 字节）")
        else:
            reply = f"查看图片失败：发送到 QQ 未成功（检查插件/NapCat 是否在线） #{code}"
            self._log("error", f"[查看图片] 发送失败: {caption}")
        self._reply_history_media_result(reply, request_id)

    def _resend_history_media(self, data: bytes, caption: str, kind: str) -> bool:
        """把历史图片发到配置的 QQ 目标（任一目标成功即算成功）。"""
        targets = []
        try:
            targets = self._qq_targets()
        except Exception as e:  # noqa: BLE001 目标解析失败
            self._log("warning", f"[查看图片] 解析 QQ 目标失败: {e}")
            return False
        if not targets:
            self._log("warning", "[查看图片] 未配置 QQ 目标，无法回发")
            return False
        try:
            image_b64 = base64.b64encode(data).decode("ascii")
        except Exception as e:  # noqa: BLE001
            self._log("warning", f"[查看图片] 图片编码失败: {e}")
            return False
        any_ok = False
        for target_type, target_id in targets:
            try:
                send_image = getattr(self.forwarder, "send_image", None)
                if send_image is not None:
                    ok, why = send_image(target_type, target_id, image_b64,
                                         caption if self._emoji_caption else "")
                else:
                    ok, why = self._send_text(target_type, target_id, caption)
                if ok:
                    any_ok = True
                    self._log("info", f"[查看图片] 已发送 {target_type}:{target_id}")
                else:
                    self._log("warning", f"[查看图片] {target_type}:{target_id} 发送失败: {why}")
            except Exception as e:  # noqa: BLE001 单个目标失败不影响其它目标
                self._log("warning", f"[查看图片] {target_type}:{target_id} 异常: {e}")
        return any_ok

    def _reply_history_media_result(self, reply: str, request_id: str) -> None:
        """把"查看图片"的结果回给用户。

        * 有 request_id（QQ 侧 `/小天才 查看图片`）-> 走插件回传，在原会话引用+@ 回复；
        * 没有 request_id（**小天才聊天里**输入的命令）-> 写回小天才聊天。
          成功也要回一句确认：图是发到 QQ 的，手表这边不给回执的话，
          用户在小天才里输入命令后会觉得"什么都没发生"。
        """
        if not reply:
            return
        if request_id:
            try:
                ok = self.forwarder.reply_result(request_id, reply)
                if not ok:
                    self._log("error", f"[查看图片回传] 失败: {reply[:80]}")
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"[查看图片回传] 异常: {e}")
            return
        self._reply_into_xtc(reply)

    def _match_source(self, entries: list[dict], source: str) -> list[dict]:
        """按来源过滤：支持「手表」「QQ私聊 <号>」「QQ群 <群号>」或裸的号
        （只写号码时，私聊/群聊都能命中）。"""
        want = str(source or "").strip()
        if not want:
            return entries
        if want in ("手表", "xtc", "watch", "家长", "宝贝"):
            return [e for e in entries if e.get("kind") == "xtc"]
        if want in ("qq", "QQ", "私聊", "群聊"):
            # 写入侧的 source 是 "QQ私聊 <号>" / "QQ群 <号>"（见 reverse.py），
            # 前面**没有**方括号 —— 只有拼进 QQ 文本时才补一个左方括号。
            # 这里若按带方括号的形式匹配，两个分支会恒为空，用户只会以为"库里没数据"。
            if want in ("私聊",):
                return [e for e in entries if (e.get("source") or "").startswith("QQ私聊")]
            if want in ("群聊",):
                return [e for e in entries if (e.get("source") or "").startswith("QQ群")]
            return [e for e in entries if e.get("kind") == "qq"]
        # 具体来源：优先 source_id 精确匹配，其次标签包含
        hit = [e for e in entries if str(e.get("source_id") or "") == want]
        if hit:
            return hit
        return [e for e in entries if want in (e.get("source") or "")]

    def _do_history_job(self, count: int, request_id: str, into_chat: bool,
                        source: str = "") -> None:
        """工作线程内：读本地消息库 -> 格式化回传（不滚动界面，不依赖聊天页状态）。"""
        entries = self.msgs.recent(1000)          # 先取全部，再做来源过滤
        if source:
            entries = self._match_source(entries, source)
        # 消息库是**按转发先后**追加的（补发会让顺序与真实时间不一致），
        # 这里按消息自己的时间排序后再取最近 N 条，历史列表才是时间顺序。
        entries = sorted(entries, key=lambda e: float(e.get("t") or 0))
        entries = entries[-max(1, int(count)):]
        if not entries:
            reply = (f"小天才历史消息（来源：{source}）：暂无本地消息记录"
                     if source else
                     "小天才历史消息：暂无本地消息记录"
                     "（消息库自桥接启用后自动积累真实对话）")
        else:
            reply = self._format_history_text(entries, count,
                                              max_chars=900 if into_chat else 3800)
        self._log("info", f"[历史消息] 来源={source or '全部'} {len(entries)} 条，回传方式: "
                          f"{'写入小天才聊天' if into_chat else 'QQ'}")
        if into_chat:
            self._reply_into_xtc(reply)
        elif request_id:
            try:
                ok = self.forwarder.reply_result(request_id, reply)
                self._log("info" if ok else "error",
                          f"[历史消息回传] {reply[:120]}...（{'成功' if ok else '失败'}）")
            except Exception as e:  # noqa: BLE001
                self._log("warning", f"历史消息回传异常: {e}")
        else:
            self._notify("小天才历史消息：\n" + reply)

    def _format_history_text(self, entries: list[dict], count: int,
                             max_chars: int = 3800) -> str:
        """把本地消息库条目格式化为文本。行格式：
            [MM-DD HH:MM] [来源] 发送方: 内容
        来源明确写出「手表」「QQ私聊 <号>」「QQ群 <群号>」，末尾附来源统计。
        - 日期用明确数字（昨天/前天 等已由归档时间戳换算成具体日期，如 09-01）；
        - 跳过系统提示（发送成功/发送失败）与命令文本（防御性过滤）；
        - 图片/表情按类型渲染成 `[图片]`、`[表情] 名字`（消息库里存的是占位文本
          "图片"/"表情X"，直接输出会让人以为那是一条文本消息）；
        - 归档过原文件的图片带上引用码 `[图片 #a1b2c3d4e5f6]`，可用
          `/小天才 查看图片 <码>` 取回真图（`msg_log.media_show_code` 控制是否显示）；
        - 超长从最旧截断。"""
        sys_prefixes = self._system_msg_prefixes()
        lines: list[str] = []
        tally: dict[str, int] = {}
        for e in entries:
            text = (e.get("text") or "").strip()
            if not text:
                continue
            if any(text.startswith(p) for p in sys_prefixes):
                continue
            if self._xtc_cmd_prefix and text.startswith(self._xtc_cmd_prefix):
                continue
            if text.startswith("图片消息"):
                # 图片气泡的无障碍描述（"图片消息，双击查看"）偶尔会被当成一条文本读进来，
                # 它和那条 `[图片]` 是同一件事，历史里只保留一条。
                continue
            sender = (e.get("sender") or "").strip()
            source = (e.get("source") or "").strip()
            if not source:  # 兼容旧数据（没有 source 字段）
                if e.get("kind") == "xtc":
                    source = self._xtc_source(sender)
                    sender = self._display_name(sender)
                else:
                    source = "QQ"
            elif e.get("kind") == "xtc":
                sender = self._display_name(sender)
            try:
                dt = datetime.fromtimestamp(float(e.get("t") or 0))
            except (TypeError, ValueError, OSError):
                dt = datetime.now()
            short = self._short_source(source)
            tally[short] = tally.get(short, 0) + 1
            # 图片/表情：库里存的是占位文本（"图片"/"表情X"），这里渲染成类型标记，
            # 否则历史列表里看起来就是一条说"图片"的文本消息（用户反馈的问题）。
            body = text
            if e.get("kind") == "xtc" and self._is_media_placeholder(text):
                body = self._typed_body(text)
                body = self._history_media_suffix(body, e)
            lines.append(f"[{dt:%m-%d} {dt:%H:%M}] [{short}] {sender}: {body}")
        if not lines:
            return "小天才历史消息：暂无本地消息记录"
        dropped = 0
        while len(lines) > 1 and sum(len(l) for l in lines) > max_chars:
            lines.pop(0)
            dropped += 1
        header = f"小天才历史消息（最近 {len(lines)} 条"
        if dropped:
            header += f"，省略更早 {dropped} 条"
        header += "）："
        summary = "来源统计：" + "、".join(
            f"{k} {v} 条" for k, v in sorted(tally.items(), key=lambda x: -x[1]))
        tail = ""
        if any(self._history_media_code(e) for e in entries):
            tail = "\n看图：/小天才 查看图片 <#后面的编码>"
        return header + "\n" + "\n".join(lines) + "\n" + summary + tail

    def _history_media_code(self, entry: dict) -> str:
        """条目对应的图片引用码（没有/取不到返回空串）。"""
        code = str(entry.get("media") or "").strip()
        if not code:
            return ""
        store = getattr(self, "media_store", None)
        if store is None:
            return ""
        # 文件已被容量淘汰时不再显示编码（否则用户照着码去看会一直失败）
        try:
            return code if store.path_of(code) is not None else ""
        except Exception:  # noqa: BLE001 判断失败就当没有
            return ""

    def _history_media_suffix(self, body: str, entry: dict) -> str:
        """给图片/表情那行补上引用码（配置可关，见 msg_log.media_show_code）。"""
        if not self._history_show_media_code():
            return body
        code = self._history_media_code(entry)
        return f"{body} #{code}" if code else body

    def _history_show_media_code(self) -> bool:
        return bool((self.cfg.get("msg_log") or {}).get("media_show_code", True))

    @staticmethod
    def _short_source(source: str) -> str:
        """来源标签里的「手表-昵称」简化为「手表」，避免每行过长（手表上打字慢）。"""
        s = str(source or "").strip()
        if s.startswith("手表"):
            return "手表"
        return s

    def _system_msg_prefixes(self) -> list:
        """桥接系统提示前缀（送达确认等）。

        必须过滤空串：空串会让 `str.startswith("")` 恒为真，导致**所有**消息都被当成
        系统提示而永不转发（历史版本曾配置里带 emoji 项，删掉后要防止出现空项）。
        """
        ui = ((self.cfg.get("xiaotiancai") or {}).get("ui") or {})
        prefixes = ui.get("system_msg_prefixes", ["发送成功", "发送失败"])
        return [str(p) for p in prefixes if str(p or "").strip()]

    def _label_epoch(self, time_label: str, now: datetime | None = None) -> float | None:
        """把 App 时间标签解析成时间戳（本地消息库归档 / 绝对化都用它）：
        'HH:MM'->今天；'今天'/'昨天'/'前天 HH:MM'->对应日期；'星期X HH:MM'->最近的那个星期X；
        'M月D日 HH:MM'、'09-25 16:18'、'2026-09-25 16:18'->该日期。
        解析失败返回 None（归档时用当前时间兜底）。"""
        return timelabel.label_epoch(time_label, now)

    def _xtc_usage(self) -> str:
        return self._XTC_USAGE

