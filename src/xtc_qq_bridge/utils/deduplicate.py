# -*- coding: utf-8 -*-
"""消息去重与回声过滤。

- Deduplicator：LRU 集合 + TTL，用于“同一消息不被重复转发”（进程内）。
- EchoFilter：记录本桥发出过的文本（双向），防止轮询到自己发的消息又回传。
  支持文件持久化（store_path）：多实例/重启后共享回声状态，避免重复转发。
- HistoryFilter：长期已处理消息表，语义与 EchoFilter 相同，只是 TTL 更长。

持久化策略（EchoFilter / HistoryFilter 共用）
--------------------------------------------
1. **启动读一次 + 写入前重读合并**：放盘格式是 `[[键, 时间戳], ...]`。写之前先重读
   磁盘、把磁盘上有而内存里没有的条目并进来（同键取较新的时间戳），再整体写回。
   早期实现是「启动读一次 + 每次 mark 全量覆盖写」：两个进程各自拿自己的内存快照
   覆盖文件，后写者胜出，先写者的记录永久丢失（实测两个实例分别 mark，磁盘只剩后者；
   重启后前者被重新转发）。合并写让多实例共用同一个文件时不再互相丢记录。
2. **唯一临时文件**：`.tmp` 名字带上进程号与随机串。固定名字的多进程/多线程同时写
   会互相踩踏（A 刚 rename、B 再 rename 就丢数据），唯一名字 + `os.replace`
   才能保证每次写入都是完整快照、读者永远读到旧或新而不会读到半个文件。
3. **脏标记 + 节流刷盘（性能）**：mark() 只改内存并标脏，最多每 `_FLUSH_INTERVAL`
   秒或累计 `_FLUSH_PENDING` 条才真正写盘（首次 mark 仍然立即落盘，方便“刚记下就
   崩”的场景与既有测试）。`flush()` 供进程退出/测试显式落盘。
   读路径（seen/is_echo）只看内存，因此**同一进程内 mark 后立即可见**，不受节流影响。

注意：不使用文件锁 —— 本项目要支持 Windows，`fcntl` 是 Unix 专有；「唯一 tmp +
读合并写」已在无锁前提下把丢记录的概率降到最低（只剩两个进程同时读-改-写这一极小
窗口，且合并会覆盖大部分情况）。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
import uuid
import weakref
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

# 节流刷盘参数（性能）：mark() 在每条消息的转发路径上，全量写盘的实测中位是
# 28.9 ms、最大 53.8 ms（2000 条满载时），必须合并写。这两个值决定"最坏延迟"：
# 崩溃时最多丢 `_FLUSH_INTERVAL` 秒内或 `_FLUSH_PENDING` 条记录（见 flush()）。
_FLUSH_INTERVAL = 1.0
_FLUSH_PENDING = 20


def _store_key(path) -> str:
    """把持久化路径归一成比较用的 key（同一文件的不同写法视为同一个）。"""
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return str(path)


# 本进程里还活着的持久化过滤器：新建**同路径**实例前先把已有实例刷盘，
# 保证「同一进程内换一个实例读同一文件」仍能读到最新数据（等价于原先的立即落盘）。
# 用 WeakSet 避免拖住对象；跨进程可见性由「写入前读合并」保证。
_LIVE_STORES: "weakref.WeakSet" = weakref.WeakSet()


def _flush_live_peers(store_path: str) -> None:
    """新建实例前，把同路径的存活实例的脏数据先落盘（同进程内跨实例可见性）。"""
    key = _store_key(store_path)
    for obj in list(_LIVE_STORES):
        try:
            if obj._store_path and _store_key(obj._store_path) == key:
                obj.flush()
        except Exception:  # noqa: BLE001 别人的刷盘失败不影响本实例初始化
            continue


def _flush_all() -> None:
    """进程退出前把脏数据落盘（等价于在 MessageBridge.stop() 里显式 flush）。

    为什么放在 atexit：桥接的 stop() 不在本模块范围内，进程被正常退出（Ctrl+C /
    sys.exit）时 atexit 一定会跑；再配合 `_FLUSH_INTERVAL` 的短窗口，
    “跨重启不重复转发”的既有语义只在崩溃（SIGKILL/断电）时才会有 ≤1 秒的误差。
    """
    for obj in list(_LIVE_STORES):
        try:
            obj.flush(timeout=0.5)
        except Exception:  # noqa: BLE001 退出阶段不再抛异常
            continue


atexit.register(_flush_all)


def _parse_pairs(items) -> list[tuple[str, float]]:
    """把落盘格式 `[[键, 时间戳], ...]` 解析成 (键, 浮点时间戳) 列表。

    单条坏数据跳过而不是整表放弃：一条脏记录不该让整份历史/回声缓存失效。
    """
    out: list[tuple[str, float]] = []
    for item in items if isinstance(items, list) else []:
        try:
            key, ts = str(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        out.append((key, ts))
    return out


def _read_pairs(path: str) -> list[tuple[str, float]] | None:
    """读持久化表：文件不存在返回 None（与"读到空表"区分），读失败返回 None。"""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return _parse_pairs(json.load(f))
    except Exception:  # noqa: BLE001 读取失败不致命（按"文件不存在"处理）
        return None


class EchoFilter:
    """记录本桥发出过的消息文本，TTL 内视为“回声”，不再转发。

    若给定 store_path，会把记录持久化（JSON），多个桥接实例/重启后共享，
    防止“实例 A 发过、实例 B 又转发一遍”的重复问题（写盘细节见模块文档）。
    """

    def __init__(self, ttl: float = 60.0, capacity: int = 100, store_path: str | None = None):
        self.ttl = ttl
        self.capacity = capacity
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._store_path = store_path
        # 节流刷盘状态：首写立即落盘（_last_save <= 0），之后按间隔/条数合并写。
        self._dirty = False
        self._pending = 0
        self._last_save = 0.0
        self.last_error = ""              # 最近一次写盘失败原因（诊断用）
        if store_path:
            _flush_live_peers(store_path)
            self._load()
            _LIVE_STORES.add(self)

    def _load(self) -> None:
        pairs = _read_pairs(self._store_path)
        if not pairs:
            return
        # 时间戳必须用**墙钟**（time.time()），与落盘内容一致。
        # 早期版本写入 time.monotonic()、读回也用 monotonic 比较：monotonic 每次启动
        # 都从新基准开始（Windows 上是开机时长），重启后 now-ts 可能是**负数** ->
        # 条目永不失效 -> 被记过的文本会被永久当成"回声"而不再转发（实机表现为
        # "重启后同样的消息再也不转发了"）。HistoryFilter 的注释已记录过同一个坑。
        now = time.time()
        for text, ts in pairs:
            # 时间戳在**未来**（时钟被回拨，或读到了早期版本写的 monotonic 值）
            # 时不能当"新鲜"：那会让 now-ts 恒为负数、条目永不过期 ——
            # 表现为"重启/校时之后，同样的消息再也不转发了"。
            if -_FUTURE_TOLERANCE <= now - ts <= self.ttl:
                self._data[text] = ts

    def _merge_from_disk(self, now: float) -> None:
        """写盘前重读磁盘并合并（多实例共用同一文件时不互相覆盖，见模块文档第 1 条）。

        顺序约定：先放磁盘条目，再把内存条目叠上去（同键取较新时间戳）。这样
        “刚 mark 的”记录位于表尾，容量裁剪优先淘汰最老的磁盘记录，不会把新记录挤掉。
        """
        pairs = _read_pairs(self._store_path)
        if not pairs:
            return
        merged: OrderedDict[str, float] = OrderedDict()
        for text, ts in pairs:
            if -_FUTURE_TOLERANCE <= now - ts <= self.ttl:
                merged[text] = ts
        for text, ts in self._data.items():
            old = merged.get(text)
            if old is None or ts > old:
                merged[text] = ts
                merged.move_to_end(text)
        self._data = merged

    def _save(self) -> None:
        tmp = ""
        try:
            if not self._store_path:
                return
            os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
            # 写后复核 + 重试：多进程"同时读-改-写"的极小窗口里，对方可能读到我们
            # 写盘前的快照、并用自己的快照把我们刚写的记录覆盖掉。写完重新读一遍，
            # 发现自己的键不在磁盘上就重新合并再写（最多 3 轮，双方都会收敛到并集）。
            for _attempt in range(3):
                now = time.time()
                # 关键：写之前先合并磁盘现状，避免覆盖掉别的实例刚写进去的记录。
                self._merge_from_disk(now)
                # 合并进来的条目可能已在磁盘上放旧了，按本实例的 TTL/容量再收一遍。
                self._purge(now)
                # 唯一临时文件名：多进程/多线程同时写也各写各的，不会互相踩踏。
                tmp = (f"{self._store_path}.{os.getpid()}"
                       f".{uuid.uuid4().hex[:8]}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump([[t, ts] for t, ts in self._data.items()], f,
                              ensure_ascii=False)
                os.replace(tmp, self._store_path)  # 原子替换
                tmp = ""
                disk = _read_pairs(self._store_path)
                if disk is None or set(self._data).issubset({k for k, _ in disk}):
                    break                     # 自己的记录都在 -> 本轮写入有效
            self.last_error = ""
        except Exception as e:  # noqa: BLE001 写盘失败不致命，但记录下来便于排障
            self.last_error = f"{type(e).__name__}: {e}"
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        finally:
            # 无论成败都刷新节流窗口：失败时避免每条消息都重试写盘把转发路径拖慢。
            self._dirty = False
            self._pending = 0
            self._last_save = time.time()

    def _maybe_save(self) -> None:
        """节流刷盘：首次 mark、累计条数到上限、或距上次写盘超过间隔时真正落盘。"""
        if not self._store_path or not self._dirty:
            return
        now = time.time()
        if (self._last_save <= 0.0 or self._pending >= _FLUSH_PENDING
                or now - self._last_save >= _FLUSH_INTERVAL):
            self._save()

    def flush(self, timeout: float | None = None) -> None:
        """立即把脏数据落盘（进程退出 / 测试显式调用）。无脏数据时是空操作。

        `timeout`：等锁的最长秒数（atexit 里用短超时）。退出阶段若某个守护线程
        恰好持着锁且已被冻结，阻塞等待会让进程卡在退出上，宁可丢掉这一小段
        脏数据也要能正常退出。
        """
        acquired = self._lock.acquire() if timeout is None \
            else self._lock.acquire(timeout=timeout)
        if not acquired:
            return
        try:
            if self._dirty:
                self._save()
        finally:
            self._lock.release()

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
            self._dirty = True
            self._pending += 1
            self._maybe_save()

    def is_echo(self, text: str) -> bool:
        now = time.time()                     # 墙钟：与落盘格式一致（见 _load）
        with self._lock:
            self._purge(now)
            return normalize(text) in self._data

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._dirty = False
            self._pending = 0
            if self._store_path:
                try:
                    os.remove(self._store_path)
                except OSError:
                    pass


class HistoryFilter:
    """长期已处理消息表（跨重启、多实例共享）。

    把"已经转发/已发送过"的消息按内容哈希记录到本地文件，TTL 默认 7 天。
    用于：重启后不会再转发历史消息、同一消息不会隔段时间被重复转发（死循环）。
    seen() 只查不记，mark() 记录（写盘细节见 EchoFilter / 模块文档）。
    """

    def __init__(self, store_path: str | None = None, ttl: float = 7 * 24 * 3600,
                 capacity: int = 2000):
        self.ttl = ttl
        self.capacity = capacity
        self._data: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._store_path = store_path
        self._dirty = False
        self._pending = 0
        self._last_save = 0.0
        self.last_error = ""
        if store_path:
            _flush_live_peers(store_path)
            self._load()
            _LIVE_STORES.add(self)

    def _load(self) -> None:
        pairs = _read_pairs(self._store_path)
        if not pairs:
            return
        # 注意：落盘用的是**墙钟**（time.time()），不是 time.monotonic()。
        # monotonic 每次重启都从新的基准开始（Windows 上是开机时长），拿它跨进程比较
        # 会让 TTL 完全失效（重启后恰好都是"负年龄"，条目永不失效）。
        now = time.time()
        for key, ts in pairs:
            # 未来时间戳（时钟回拨 / 旧 monotonic 数据）同样要丢弃：
            # 否则 now-ts 恒为负，条目永不过期，消息会被永久判成"已处理"。
            if -_FUTURE_TOLERANCE <= now - ts <= self.ttl:
                self._data[key] = ts

    def _merge_from_disk(self, now: float) -> None:
        """写盘前重读磁盘并合并（多实例/多进程共用一个文件时不互相丢记录）。"""
        pairs = _read_pairs(self._store_path)
        if not pairs:
            return
        merged: OrderedDict[str, float] = OrderedDict()
        for key, ts in pairs:
            if -_FUTURE_TOLERANCE <= now - ts <= self.ttl:
                merged[key] = ts
        for key, ts in self._data.items():
            old = merged.get(key)
            if old is None or ts > old:
                merged[key] = ts
                merged.move_to_end(key)
        self._data = merged

    def _save(self) -> None:
        tmp = ""
        try:
            if not self._store_path:
                return
            os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
            # 写后复核 + 重试（原因见 EchoFilter._save）：并发写者覆盖时重新合并再写。
            for _attempt in range(3):
                now = time.time()
                self._merge_from_disk(now)
                self._purge(now)
                tmp = (f"{self._store_path}.{os.getpid()}"
                       f".{uuid.uuid4().hex[:8]}.tmp")
                # 必须写每个条目**自己的**时间戳（self._data 里存的就是它被 mark 的时刻）。
                # 早期版本这里写的是 time.time()，等于每次落盘都把全表年龄清零 ——
                # TTL 永远不会到期，表只会涨到 capacity 上限，旧记录还会挤掉新记录的名额。
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump([[k, ts] for k, ts in self._data.items()], f,
                              ensure_ascii=False)
                os.replace(tmp, self._store_path)
                tmp = ""
                disk = _read_pairs(self._store_path)
                if disk is None or set(self._data).issubset({k for k, _ in disk}):
                    break
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        finally:
            self._dirty = False
            self._pending = 0
            self._last_save = time.time()

    def _maybe_save(self) -> None:
        if not self._store_path or not self._dirty:
            return
        now = time.time()
        if (self._last_save <= 0.0 or self._pending >= _FLUSH_PENDING
                or now - self._last_save >= _FLUSH_INTERVAL):
            self._save()

    def flush(self, timeout: float | None = None) -> None:
        """立即把脏数据落盘（进程退出 / 测试显式调用）。无脏数据时是空操作。

        `timeout`：等锁的最长秒数（atexit 里用短超时）。退出阶段若某个守护线程
        恰好持着锁且已被冻结，阻塞等待会让进程卡在退出上，宁可丢掉这一小段
        脏数据也要能正常退出。
        """
        acquired = self._lock.acquire() if timeout is None \
            else self._lock.acquire(timeout=timeout)
        if not acquired:
            return
        try:
            if self._dirty:
                self._save()
        finally:
            self._lock.release()

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
            self._dirty = True
            self._pending += 1
            self._maybe_save()
