# -*- coding: utf-8 -*-
"""小天才 App 时间标签的解析与归一化（桥接层与界面操作层共用）。

为什么单独一个模块：`bridge`（转发/去重/历史）与 `xiaotiancai`（定位气泡）都要把
App 界面上的时间标签（`16:18`、`昨天 23:42`、`星期一 16:18`、`8月30日`、`09-25 16:18`）
换算成同一个绝对时间。此前这份逻辑只存在于 `bridge`，界面层直接调 `self._abs_time_label`
—— 而 `Xiaotiancai` 并不继承桥接的 mixin，于是"带时间标签去定位图片/表情气泡"必然抛
AttributeError，并被上层的 `except Exception` 吞掉（表现为"界面上找不到气泡"、
照片/表情退化成纯文字）。现在两侧共用这里的实现。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

__all__ = ["label_epoch", "abs_label", "ABS_FORMAT"]

# 绝对标签的统一格式（身份/显示都以它为准）
ABS_FORMAT = "%m-%d %H:%M"


def label_epoch(time_label: str, now: datetime | None = None) -> float | None:
    """把 App 时间标签解析成时间戳；解析不出来返回 None。

    支持：'HH:MM'（今天）、'今天/今日 HH:MM'、'昨天/前天 HH:MM'、'星期X HH:MM'、
    'M月D日 HH:MM'、'09-25 16:18'、'2026-09-25 16:18'、'2026/09/25 16:18'。
    """
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


def abs_label(time_label: str, now: datetime | None = None) -> str:
    """把任意 App 时间标签统一成绝对的 `MM-DD HH:MM`；解析不出来返回 ""。

    为什么必须转绝对：App 对**同一条消息**的标签会随日期变化 —— 当天显示 `16:18`，
    第二天变成 `昨天 16:18`，再往后可能变成 `09-25 16:18`。原样比较就会出现
    "同一条消息隔天被当成新消息"，或"界面标签与补发身份永远对不上"。
    """
    epoch = label_epoch(time_label, now)
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime(ABS_FORMAT)
    except (OverflowError, OSError, ValueError):  # noqa: BLE001 时间戳异常就当解析不出来
        return ""
