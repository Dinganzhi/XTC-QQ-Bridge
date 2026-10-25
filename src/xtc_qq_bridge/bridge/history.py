# -*- coding: utf-8 -*-
"""历史消息与 xtc 侧命令（原 bridge.py 的一部分，逻辑未改动）。"""
from __future__ import annotations

from .shared import *  # noqa: F401,F403


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

    def _match_source(self, entries: list[dict], source: str) -> list[dict]:
        """按来源过滤：支持「手表」「QQ私聊 <号>」「QQ群 <群号>」或裸的号
        （只写号码时，私聊/群聊都能命中）。"""
        want = (source or "").strip()
        if not want:
            return entries
        if want in ("手表", "xtc", "watch", "家长", "宝贝"):
            return [e for e in entries if e.get("kind") == "xtc"]
        if want in ("qq", "QQ", "私聊", "群聊"):
            if want in ("私聊",):
                return [e for e in entries if "[QQ私聊" in (e.get("source") or "")]
            if want in ("群聊",):
                return [e for e in entries if "[QQ群" in (e.get("source") or "")]
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
        return header + "\n" + "\n".join(lines) + "\n" + summary

    @staticmethod
    def _short_source(source: str) -> str:
        """来源标签里的「手表-昵称」简化为「手表」，避免每行过长（手表上打字慢）。"""
        s = (source or "").strip()
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
        label = (time_label or "").strip()
        if not label:
            return None
        now = now or datetime.now()
        # 已经是绝对时间：09-25 16:18 / 2026-09-25 16:18 / 2026/09/25 16:18
        m = re.search(r"(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})[\sT]+(\d{1,2}):(\d{2})", label)
        if m:
            try:
                dt = now.replace(year=int(m.group(1)) if m.group(1) else now.year,
                                 month=int(m.group(2)), day=int(m.group(3)),
                                 hour=int(m.group(4)), minute=int(m.group(5)),
                                 second=0, microsecond=0)
            except ValueError:
                return None
            if not m.group(1) and dt > now + timedelta(days=1):
                dt = dt.replace(year=dt.year - 1)   # 没写年份且落在未来 -> 通常是去年的
            return dt.timestamp()
        # "今天 16:18" / "今日 16:18"
        m = re.fullmatch(r"(?:今天|今日)\s*(\d{1,2}):(\d{2})", label)
        if m:
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=0, microsecond=0).timestamp()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", label)
        if m:
            dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                             second=0, microsecond=0)
            if dt > now + timedelta(minutes=2):
                # 只有时分、却落在**未来**：那只能是**昨天**那条 —— App 的分组标签跨零点后
                # 还画着旧样子（实测 10-15 16:23 的表情在 10-16 00:00 又出现一次，
                # 被算成"今天 16:23"：身份变了 -> 重复转发，时间也标错一天）。
                # 消息不可能来自未来，所以这里一律回退一天。
                dt -= timedelta(days=1)
            return dt.timestamp()
        m = re.fullmatch(r"(昨天|前天)\s*(\d{1,2}):(\d{2})", label)
        if m:
            days_back = 1 if m.group(1) == "昨天" else 2
            return (now - timedelta(days=days_back)).replace(
                hour=int(m.group(2)), minute=int(m.group(3)),
                second=0, microsecond=0).timestamp()
        # "星期一 16:18" -> 最近一个已经过去的那个星期几
        m = re.search(r"星期([一二三四五六日天])", label)
        if m:
            tm = re.search(r"(\d{1,2}):(\d{2})", label)
            idx = "一二三四五六日天".index(m.group(1)) + 1        # 周一=1 ... 周日=7
            days_back = (now.isoweekday() - idx) % 7
            dt = (now - timedelta(days=days_back)).replace(
                hour=int(tm.group(1)) if tm else 0,
                minute=int(tm.group(2)) if tm else 0, second=0, microsecond=0)
            if dt > now:
                dt = dt - timedelta(days=7)
            return dt.timestamp()
        m = re.search(r"(\d{1,2})月(\d{1,2})日", label)
        if m:
            tm = re.search(r"(\d{1,2}):(\d{2})", label)
            try:
                dt = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                                 hour=int(tm.group(1)) if tm else 0,
                                 minute=int(tm.group(2)) if tm else 0,
                                 second=0, microsecond=0)
            except ValueError:
                return None
            if dt > now + timedelta(days=1):
                dt = dt.replace(year=dt.year - 1)   # 只写月日且落在未来 -> 去年
            return dt.timestamp()
        return None

    def _xtc_usage(self) -> str:
        return self._XTC_USAGE

