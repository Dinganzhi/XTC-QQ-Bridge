# -*- coding: utf-8 -*-
"""消息去重与回声过滤。

- Deduplicator：LRU 集合 + TTL，用于“同一消息不被重复转发”（进程内）。
- EchoFilter：记录本桥发出过的文本（双向），防止轮询到自己发的消息又回传。
  支持文件持久化（store_path）：多实例/重启后共享回声状态，避免重复转发。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict


def _hash(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def normalize(text: str) -> str:
    """归一化文本用于比较：统一换行、折叠空白、去首尾。
    避免同一消息因 \r\n vs \n、多余空格等显示差异导致回声/去重失效。"""
    if not text:
        return ""
    return " ".join(str(text).replace("\r\n", "\n").replace("\r", "\n").split())


class Deduplicator:
    """容量上限 + TTL 的 LRU 去重。seen() 只查不记，mark() 记录。

    **线程安全**：轮询线程会 seen()/mark()，webhook 线程与工作线程也会 mark()。
    早期版本没有锁，"先 seen 再 mark"之间可能被另一线程插入，同一条消息会被
    两个线程同时判成新消息；这里补上与 EchoFilter / HistoryFilter 一致的锁。
    """

    def __init__(self, capacity: int = 200, ttl: float = 120.0):
        self.capacity = capacity
        self.ttl = ttl
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        # 过期的两种情况：超过 TTL，或时间戳落在未来（时钟回拨 / 旧 monotonic 数据）。
        expired = [k for k, ts in self._data.items()
                   if now - ts > self.ttl or now - ts < -_FUTURE_TOLERANCE]
        for k in expired:
            del self._data[k]
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def seen(self, *parts) -> bool:
        now = time.monotonic()
        key = _hash(*[normalize(p) for p in parts])
        with self._lock:
            self._purge(now)
            return key in self._data

    def mark(self, *parts) -> None:
        now = time.monotonic()
        key = _hash(*[normalize(p) for p in parts])
        with self._lock:
            self._data[key] = now
            self._data.move_to_end(key)
            self._purge(now)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# 允许的时间戳"超前"容差（秒）：同一台机器上落盘与读回之间会差几十毫秒，
# 秒级校时（NTP 微调）也可能让 ts 略微大于 now，这些都不算"未来"。
_FUTURE_TOLERANCE = 5.0


class EchoFilter:
    """记录本桥发出过的消息文本，TTL 内视为“回声”，不再转发。

    若给定 store_path，每次 mark 会写盘（JSON），多个桥接实例/重启后共享，
    防止“实例 A 发过、实例 B 又转发一遍”的重复问题。
    """

    def __init__(self, ttl: float = 60.0, capacity: int = 100, store_path: str | None = None):
        self.ttl = ttl
        self.capacity = capacity
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._store_path = store_path
        if store_path:
            self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(self._store_path):
                return
            with open(self._store_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            # 时间戳必须用**墙钟**（time.time()），与落盘内容一致。
            # 早期版本写入 time.monotonic()、读回也用 monotonic 比较：monotonic 每次启动
            # 都从新基准开始（Windows 上是开机时长），重启后 now-ts 可能是**负数** ->
            # 条目永不失效 -> 被记过的文本会被永久当成"回声"而不再转发（实机表现为
            # "重启后同样的消息再也不转发了"）。HistoryFilter 的注释已记录过同一个坑。
            now = time.time()
            for item in items:
                try:
                    text, ts = item[0], float(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                # 时间戳在**未来**（时钟被回拨，或读到了早期版本写的 monotonic 值）
                # 时不能当"新鲜"：那会让 now-ts 恒为负数、条目永不过期 ——
                # 表现为"重启/校时之后，同样的消息再也不转发了"。
                if -_FUTURE_TOLERANCE <= now - ts <= self.ttl:
                    self._data[text] = ts
        except Exception:  # noqa: BLE001 读取失败不致命
            self._data.clear()

    def _save(self) -> None:
        if not self._store_path:
            return
        try:
            os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
            tmp = self._store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([[t, ts] for t, ts in self._data.items()], f, ensure_ascii=False)
            os.replace(tmp, self._store_path)  # 原子替换
        except Exception:  # noqa: BLE001 写盘失败不致命
            pass

    def _purge(self, now: float) -> None:
        # 过期：超过 TTL，或时间戳在未来（时钟回拨 / 旧 monotonic 数据）
        expired = [k for k, ts in self._data.items()
                   if now - ts > self.ttl or now - ts < -_FUTURE_TOLERANCE]
        for k in expired:
            del self._data[k]
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def mark(self, text: str) -> None:
        text = normalize(text)
        if not text:
            return
        now = time.time()                     # 墙钟：与落盘格式一致（见 _load）
        with self._lock:
            self._data[text] = now
            self._data.move_to_end(text)
            self._purge(now)
            self._save()

    def is_echo(self, text: str) -> bool:
        now = time.time()                     # 墙钟：与落盘格式一致（见 _load）
        with self._lock:
            self._purge(now)
            return normalize(text) in self._data

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            if self._store_path:
                try:
                    os.remove(self._store_path)
                except OSError:
                    pass


class HistoryFilter:
    """长期已处理消息表（跨重启、多实例共享）。

    把"已经转发/已发送过"的消息按内容哈希记录到本地文件，TTL 默认 7 天。
    用于：重启后不会再转发历史消息、同一消息不会隔段时间被重复转发（死循环）。
    seen() 只查不记，mark() 记录。
    """

    def __init__(self, store_path: str | None = None, ttl: float = 7 * 24 * 3600,
                 capacity: int = 2000):
        self.ttl = ttl
        self.capacity = capacity
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._store_path = store_path
        if store_path:
            self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(self._store_path):
                return
            with open(self._store_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            # 注意：落盘用的是**墙钟**（time.time()），不是 time.monotonic()。
            # monotonic 每次重启都从新的基准开始（Windows 上是开机时长），拿它跨进程比较
            # 会让 TTL 完全失效（重启后恰好都是"负年龄"，条目永不失效）。
            now = time.time()
            for item in items:
                try:
                    key, ts = item[0], float(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                # 未来时间戳（时钟回拨 / 旧 monotonic 数据）同样要丢弃：
                # 否则 now-ts 恒为负，条目永不过期，消息会被永久判成"已处理"。
                if -_FUTURE_TOLERANCE <= now - ts <= self.ttl:
                    self._data[key] = ts
        except Exception:  # noqa: BLE001
            self._data.clear()

    def _save(self) -> None:
        if not self._store_path:
            return
        try:
            os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
            tmp = self._store_path + ".tmp"
            # 必须写每个条目**自己的**时间戳（self._data 里存的就是它被 mark 的时刻）。
            # 早期版本这里写的是 time.time()，等于每次落盘都把全表年龄清零 ——
            # TTL 永远不会到期，表只会涨到 capacity 上限，旧记录还会挤掉新记录的名额。
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([[k, ts] for k, ts in self._data.items()], f, ensure_ascii=False)
            os.replace(tmp, self._store_path)
        except Exception:  # noqa: BLE001
            pass

    def _purge(self, now: float) -> None:
        # 与 Deduplicator / EchoFilter 保持一致：时间戳落在未来（时钟回拨、
        # 或从旧版本读到的 monotonic 值）也要清掉 —— 否则 now-ts 恒为负，
        # 这些条目永不过期，会一直占着 capacity 的名额。
        expired = [k for k, ts in self._data.items()
                   if now - ts > self.ttl or now - ts < -_FUTURE_TOLERANCE]
        for k in expired:
            del self._data[k]
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def seen(self, *parts) -> bool:
        now = time.time()                     # 墙钟：与落盘格式一致（见 _load）
        key = _hash(*[normalize(p) for p in parts])
        with self._lock:
            self._purge(now)
            return key in self._data

    def mark(self, *parts) -> None:
        if not any(parts):
            return
        now = time.time()                     # 墙钟：与落盘格式一致（见 _load）
        key = _hash(*[normalize(p) for p in parts])
        with self._lock:
            self._data[key] = now
            self._data.move_to_end(key)
            self._purge(now)
            self._save()
