# -*- coding: utf-8 -*-
"""本地消息库：`/小天才 历史消息` 的数据源，同时是"这条消息处理过没有"的判定库。

记录桥接处理过的**真实对话消息**（手表侧读到的消息 + 从 QQ 发进小天才的消息），
不记录系统提示（发送成功/发送失败）、命令文本、桥接自己的回复。
每条记录都带**来源**（source：手表 / QQ私聊 xxx / QQ群 xxx），历史消息按来源标注。

分库（用户要求）
----------------
- `cap`：最多保留多少条；**0 = 不限制**。
- `shard_size`：每写满这么多条就新建一个分库文件；**0 = 不分库**（单文件，
  文件名沿用 `msg_log.json`，兼容旧数据）。
- `read_shards`：启动时加载最新几个分库用于判定/历史查询，默认 **1**。
  判定"这条消息在不在库里"只需要最新的库，所以默认只读一个，需要追溯更早数据时才惰性加载。

分库文件命名：`msg_log-0001.json`、`msg_log-0002.json` ……（序号补零，字典序即时间序；
首次开启分库时会把老的 `msg_log.json` 自动迁移成 `0001` 号分库）。

效率：追加时只重写**当前分库**（≤ shard_size 条），不像单文件那样每次重写全量。

内存：常驻进程最多在内存里保留 `cap` 条（分库轮换时按整个分库丢弃更老的，需要时由
`_all_locked()` 惰性从磁盘补齐），因此内存占用有上界，不随运行时长无限增长；
`cap=0`（不限制）时全部保留，与单文件模式一致。
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
import uuid
import weakref
from pathlib import Path

from . import runtime_paths

_SHARD_DIGITS = 4

# 节流刷盘参数（性能）：append() 位于每条消息的转发路径上，分库写满瞬间最坏要
# 231.84 ms；默认逐条同步写盘太贵。改为脏标记 + 合并写，最多每 `_FLUSH_INTERVAL`
# 秒或累计 `_FLUSH_PENDING` 条才真正落盘（首次 append 仍立即写，保证“刚写就崩”和安全）。
_FLUSH_INTERVAL = 1.0
_FLUSH_PENDING = 20


def _path_key(path) -> str:
    """把消息库路径归一成比较用的 key（同一文件的不同写法视为同一个）。"""
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return str(path)


# 本进程里还活着的消息库实例：新建**同路径**实例前先把已有实例刷盘，
# 保证「同一进程内换一个实例读同一文件」仍能读到最新数据（测试/重启模拟依赖这个）。
# 跨进程可见性由 `_save()` 的「写前读磁盘并合并」保证。
_LIVE_LOGS: "weakref.WeakSet" = weakref.WeakSet()


def _flush_live_peers(path) -> None:
    """新建实例前，把同路径的存活实例的脏数据先落盘。"""
    key = _path_key(path)
    for obj in list(_LIVE_LOGS):
        try:
            if _path_key(obj.path) == key:
                obj.flush()
        except Exception:  # noqa: BLE001 别人的刷盘失败不影响本实例初始化
            continue


def _flush_all_logs() -> None:
    """进程退出前把脏数据落盘（等价于在 MessageBridge.stop() 里显式 flush）。"""
    for obj in list(_LIVE_LOGS):
        try:
            obj.flush(timeout=0.5)
        except Exception:  # noqa: BLE001 退出阶段不再抛异常
            continue


atexit.register(_flush_all_logs)


def _entry_sig(entry: dict) -> str:
    """条目的比较签名（合并磁盘/内存时判重、且不依赖字段顺序）。"""
    try:
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(entry)


class MessageLog:
    def __init__(self, path: str = "", cap: int = 1000, shard_size: int = 0,
                 read_shards: int = 1, logger=None):
        # 默认放可写数据目录：Nuitka onefile 下是 exe 旁边，而不是退出即删的临时目录
        self.path = Path(path) if path else runtime_paths.data_path("msg_log.json")
        self.cap = max(0, int(cap))                 # 0 = 不限制
        self.shard_size = max(0, int(shard_size))   # 0 = 不分库
        self.read_shards = max(1, int(read_shards))
        self.logger = logger
        self._lock = threading.Lock()
        # 已加载的分库内容（旧 -> 新）；默认只有最新 read_shards 个
        self._entries: list[dict] = []
        # 内存中各分库的边界：[(分库序号, 该分库在 _entries 里的起始下标)]，旧 -> 新。
        # 序号 0 表示"不分库"时的 msg_log.json 本身。
        # `_cur_start` / `_current` / `_loaded_first_seq` 都由它推导（唯一真相来源），
        # 避免多份状态不同步 —— 曾经因为 `_cur_start` 写死为 0，在 read_shards>1 时
        # 把更老分库的内容一并写进最新分库文件（历史消息出现重复，且每轮重启翻倍）。
        self._mem_shards: list[tuple[int, int]] = [(0, 0)]
        self._older: list[dict] | None = None       # 更老的分库（惰性加载，用于历史查询）
        # 节流刷盘状态（见模块顶部 _FLUSH_*）：append() 只改内存并标脏，
        # 按间隔/条数合并写盘；flush() 供进程退出/测试显式落盘。
        self._dirty = False
        self._pending = 0
        self._last_save = 0.0
        self._save_warned = False                   # 写盘失败只告警一次
        self.last_error = ""                        # 最近一次写盘失败原因（诊断用）
        # 新建同路径实例前先把已有实例的脏数据落盘：本进程内跨实例可见性与
        # 原先的“每次 append 立即落盘”一致（跨进程由 _save 的读合并保证）。
        _flush_live_peers(self.path)
        self._load()
        _LIVE_LOGS.add(self)

    # ------------------------------------------------------------------ 内部状态
    @property
    def _cur_start(self) -> int:
        """当前分库在 `_entries` 里的起始下标（写盘只写这一段）。"""
        return self._mem_shards[-1][1]

    @property
    def _current(self) -> Path:
        """当前分库文件（不分库时就是 msg_log.json）。"""
        seq = self._mem_shards[-1][0]
        return self.path if not seq else self._shard_path(seq)

    @property
    def _loaded_first_seq(self) -> int:
        """内存里最老那个分库的序号（惰性补齐更老分库时的分界）。"""
        return self._mem_shards[0][0]

    # ------------------------------------------------------------------ 分库文件
    def _shard_files(self) -> list[Path]:
        """磁盘上**已存在**的分库文件（旧 -> 新）。

        注意：这里跟本实例有没有开分库（shard_size）无关——只要盘上有分库文件就要认，
        否则只读工具（live_probe / 历史消息查询）会以为库是空的。
        """
        pat = f"{self.path.stem}-{'[0-9]' * _SHARD_DIGITS}{self.path.suffix}"
        return sorted(self.path.parent.glob(pat))

    def _seq(self, p: Path) -> int:
        """分库文件的序号（裸文件名算 0）。"""
        try:
            return int(p.stem.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    def _shard_path(self, seq: int) -> Path:
        return self.path.with_name(
            f"{self.path.stem}-{seq:0{_SHARD_DIGITS}d}{self.path.suffix}")

    def _warn(self, msg: str) -> None:
        """写一条告警到日志（没有 logger 时直接打印，绝不静默丢信息）。"""
        if self.logger is not None:
            try:
                self.logger.warning(msg)
                return
            except Exception:  # noqa: BLE001 日志本身出错不能影响主流程
                pass
        print(msg)

    def _isolate_corrupt(self, p: Path, err: Exception) -> None:
        """把损坏的分库文件改名隔离为 `*.corrupt-<时间戳>`，并告警。

        为什么必须隔离：旧实现解析失败只返回 `[]`，随后 `append()` 用“只剩新条目”
        的内容覆盖该分库 —— 历史与 `seen()` 判重数据**永久丢失且毫无告警**，
        重启后这些消息会被当成新消息重新转发。改名保留原文件既方便事后人工抢救，
        也保证之后不会再往里追加（新分库从空文件重新开始）。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = p.with_name(f"{p.name}.corrupt-{stamp}")
        try:
            n = 1
            while dest.exists():     # 同一秒内损坏多个分库时不要互相覆盖
                dest = p.with_name(f"{p.name}.corrupt-{stamp}-{n}")
                n += 1
            p.rename(dest)
        except OSError:
            dest = p                 # 改名失败只能按原样处理（下面记原因）
        reason = f"{type(err).__name__}: {err}"
        self.last_error = f"分库文件损坏已隔离: {dest.name}（{reason}）"
        self._warn(f"消息库文件损坏，已改名隔离为 {dest.name}（原内容保留，不再追加）: {reason}")

    def _read_file(self, p: Path) -> list[dict]:
        """读一个分库文件；解析失败/结构不对时隔离该文件，返回空列表。"""
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001 解析失败：隔离并告警，不能当成空库覆盖
            self._isolate_corrupt(p, e)
            return []
        if not isinstance(data, list):
            # 顶层不是数组同样属于损坏：按坏文件隔离，避免下一次 append 把它覆盖掉。
            self._isolate_corrupt(p, ValueError(f"顶层不是数组（{type(data).__name__}）"))
            return []
        return [e for e in data if isinstance(e, dict)]

    def _load(self) -> None:
        # 首次开启分库：把老的单文件迁成 0001 号分库
        if self.shard_size and self.path.exists():
            first = self._shard_path(1)
            try:
                if not first.exists():
                    self.path.replace(first)
            except Exception:  # noqa: BLE001 迁移失败就按现状继续
                pass
        files = self._shard_files()
        self._older = None
        if files:
            # 只要磁盘上已经有分库文件就按分库读（哪怕本实例没开分库）：
            # 否则只读工具（live_probe / 历史消息）会看到"库是空的"这种假象。
            loaded = files[-self.read_shards:]
            self._entries = []
            self._mem_shards = []
            for p in loaded:
                entries = self._read_file(p)
                if not p.exists():
                    # `_read_file` 解析失败时已把它改名隔离：这条分库不再当“当前分库”，
                    # 否则接下来会往里追加、把原文件彻底覆盖掉。
                    continue
                self._mem_shards.append((self._seq(p), len(self._entries)))
                self._entries.extend(entries)
            if not self._mem_shards:
                # 所有加载的分库都损坏：从头开始，但不复用它们的名称（已改名隔离）。
                self._entries = []
                self._mem_shards = [(self._next_seq() if self.shard_size else 0, 0)]
            return
        # 没有分库文件：按单文件处理（单文件损坏时会先被隔离，随后写新文件）
        self._entries = self._read_file(self.path) if self.path.exists() else []
        self._mem_shards = [(1 if self.shard_size else 0, 0)]
        if not self.shard_size and self.cap:
            self._entries = self._entries[-self.cap:]

    def _next_seq(self) -> int:
        seqs = [self._seq(p) for p in self._shard_files()]
        seqs = [s for s in seqs if s > 0]
        return (max(seqs) + 1) if seqs else 1

    def _merge_shard(self, mem: list[dict], disk: list[dict]) -> list[dict]:
        """把磁盘上当前分库里有、内存里没有的条目合并进来（写入前调用）。

        多进程共用同一个数据目录时，各方都持有自己的内存快照；直接全量覆盖会让
        后写者把先写者刚追加的记录抹掉（实测：两个实例分别 append，磁盘只剩后者，
        重启后前者被重新转发）。合并按条目签名判重，磁盘独有的条目按时间插入。
        """
        if not disk:
            return mem
        out = list(mem)
        seen = {_entry_sig(e) for e in out}
        for e in disk:
            sig = _entry_sig(e)
            if sig in seen:
                continue
            seen.add(sig)
            # 按时间戳插入（保持“旧 -> 新”的顺序；时间不可比时退化为追加到末尾）
            pos = len(out)
            while pos > 0:
                prev, cur = out[pos - 1].get("t"), e.get("t")
                if not (isinstance(prev, (int, float)) and isinstance(cur, (int, float))):
                    break
                if prev > cur:
                    pos -= 1
                else:
                    break
            out.insert(pos, e)
        return out

    def _save(self) -> None:
        """只重写当前分库（这是分库带来的效率提升）。

        写盘前先重读该分库并把磁盘独有的条目合并进来：多实例不会互相丢记录
        （见 `_merge_shard`）。临时文件名带进程号与随机串，避免并发写互相踩踏。
        """
        target = self._current
        tmp = ""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 写后复核 + 重试：两个进程"同时读-改-写"的极小窗口里，对方可能读到我们
            # 写盘前的快照并把它写回、把我们刚写的记录覆盖掉。写完重新读一遍，发现
            # 自己的条目不在磁盘上就重新合并再写（最多 3 轮，双方会收敛到并集）。
            for _attempt in range(3):
                mem = self._entries[self._cur_start:]
                disk = self._read_file(target) if target.exists() else []
                if disk:
                    mem = self._merge_shard(mem, disk)
                    if not self.shard_size and self.cap:   # 单文件模式仍受条数上限约束
                        mem = mem[-self.cap:]
                tmp = f"{target}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
                Path(tmp).write_text(json.dumps(mem, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, target)
                tmp = ""
                written = {_entry_sig(e) for e in mem}
                now_disk = self._read_file(target) if target.exists() else []
                if written.issubset({_entry_sig(e) for e in now_disk}):
                    break                     # 自己的条目都在 -> 本轮写入有效
            self.last_error = ""
        except Exception as e:  # noqa: BLE001 写盘失败不影响运行，但要让用户知道
            # 静默失败会让"历史消息/是否已处理"的判定库悄悄停止更新（磁盘满、
            # 目录只读、被杀毒软件占用等），因此首次失败记一条告警。
            self.last_error = f"{type(e).__name__}: {e}"
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not self._save_warned:
                self._save_warned = True
                self._warn(f"消息库写入失败（历史与判重将停留在内存中）: {self.last_error}")
        finally:
            # 无论成败都刷新节流窗口：失败时不要每条消息都重试写盘拖慢转发路径。
            self._dirty = False
            self._pending = 0
            self._last_save = time.time()

    def _maybe_save(self) -> None:
        """节流刷盘：首次 append、累计条数到上限、或距上次写盘超过间隔时真正落盘。"""
        if not self._dirty:
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

    def _rotate_if_needed(self) -> bool:
        """当前分库写满 -> 换新分库（并把更老的分库移出内存）。

        返回值表示这次是否发生了轮换：调用方据此**立即落盘**新分库的第一条记录
        （轮换本身很少发生，这一次同步写不影响性能，但能保证新分库文件立刻存在）。
        """
        if not self.shard_size:
            return False
        if len(self._entries) - self._cur_start < self.shard_size:
            return False
        # 写满的分库必须在换到新分库**之前**落盘：一旦新分库成为 _current，
        # 老分库就再也不会被重写，没落盘的记录会永久丢失。
        if self._dirty:
            self._save()
        self._mem_shards.append((self._next_seq(), len(self._entries)))
        self._trim_memory()
        return True

    def _trim_memory(self) -> None:
        """内存里最多保留 `cap` 条（按整个分库丢弃）；更老的由 `_all_locked()` 惰性读盘。

        为什么要裁剪：常驻进程跑几周后 `_entries` 会积累到几万条，`seen()` 每次都要
        倒序扫一遍，而磁盘上的老分库其实早已按 cap 删掉。
        `cap=0`（不限制）时不裁剪 —— 与单文件模式的行为一致，"全部保留"是用户的显式选择。
        """
        if not (self.shard_size and self.cap):
            return
        while len(self._mem_shards) > 1 and len(self._entries) > self.cap:
            start = self._mem_shards[1][1]
            if start <= 0:
                break
            del self._entries[:start]
            self._mem_shards.pop(0)
            self._mem_shards = [(seq, st - start) for seq, st in self._mem_shards]
            self._older = None          # 内存边界变了，缓存的"更老分库"要重算

    def _enforce_shard_cap(self) -> None:
        """按 cap 删掉最老的分库（分库粒度：最多保留 ceil(cap / shard_size) 个）。

        必须在 `_save()` 之后调用：新分库写盘前不在文件列表里，提前算会多留一个。
        """
        if not (self.shard_size and self.cap):
            return
        keep = max(1, -(-self.cap // self.shard_size))
        for p in self._shard_files()[:-keep]:
            try:
                p.unlink()
            except OSError:
                pass
        # 被删掉的分库若还在内存里，也要一并丢弃（否则 count()/recent() 会算进已删除的数据）
        existing = {self._seq(p) for p in self._shard_files()}
        while len(self._mem_shards) > 1 and self._mem_shards[0][0] not in existing:
            start = self._mem_shards[1][1]
            if start > 0:
                del self._entries[:start]
            self._mem_shards.pop(0)
            self._mem_shards = [(seq, st - start) for seq, st in self._mem_shards]
            self._older = None

    # ------------------------------------------------------------------ 写入 / 查询
    def append(self, kind: str, sender: str, text: str,
               t: float | None = None, source: str = "",
               source_id: str = "", media: str = "") -> None:
        """追加一条消息。

        kind:      'xtc'（手表侧）/ 'qq'（QQ 发入小天才）
        source:    人类可读来源标签，如「手表」「QQ私聊 10001」「QQ群 123456」
        source_id: 来源标识（QQ 号 / 群号 / 联系人名），便于按来源过滤
        media:     图片/表情在 `media_store` 里的短引用码（没有图时为空）。
                   只存这个十几字节的码，图片本体在 data/history_media/ 下，
                   历史消息据此可以显示 `[图片 #code]` 并用 `/小天才 查看图片 code` 取回。
        """
        text = str(text or "").strip()
        if not text:
            return
        entry = {
            "t": t if t is not None else time.time(),
            "kind": kind,
            "sender": str(sender or "").strip(),
            "text": text,
            "source": str(source or "").strip(),
            "source_id": str(source_id or "").strip(),
        }
        if media:
            entry["media"] = str(media)
        with self._lock:
            rotated = self._rotate_if_needed()
            self._entries.append(entry)
            if not self.shard_size and self.cap and len(self._entries) > self.cap:
                del self._entries[:len(self._entries) - self.cap]
            self._dirty = True
            self._pending += 1
            # 发生轮换时立即落盘（新分库文件马上建立、第一条记录不延迟）；
            # 其余情况走节流：append 在每条消息的转发路径上，逐条全量写盘太贵。
            if rotated:
                self._save()
            else:
                self._maybe_save()
            self._enforce_shard_cap()

    def seen(self, text: str, kind: str = "") -> bool:
        """库里有没有这条消息（只查已加载的分库，默认就是最新那个）。

        这是"从最新往回走、撞库即停"的判定入口：命中说明这条已经处理过。
        """
        needle = str(text or "").strip()
        if not needle:
            return False
        with self._lock:
            for e in reversed(self._entries):        # 从新往旧找，命中更快
                if e.get("text") == needle and (not kind or e.get("kind") == kind):
                    return True
        return False

    def _all_locked(self) -> list[dict]:
        """已加载的 + 更老的分库（惰性加载一次并缓存，不重复计入内存里已有的）。

        `_entries` 里可能已经包含本进程自己写出来的多个分库，所以只补齐
        "比 `_loaded_first_seq` 还老"的那些文件，否则会把同一批消息数两遍。

        注意提前返回的条件：只有**确实处于单文件模式**（内存里唯一的分库序号是 0）
        才能直接返回内存。不能看配置项 `self.shard_size` —— 盘上已有分库、用户又把
        `msg_log.shard_size` 改成 0 时，`_load()` 仍按分库只读最新几个；若这里按
        “没开分库”提前返回，更老的分库就永远不会被读：`/小天才 历史消息` 丢老消息，
        `seen()` 漏判导致重复转发。
        """
        if self._mem_shards and self._mem_shards[0][0] == 0:
            return self._entries
        if self._older is None:
            older = []
            first = self._loaded_first_seq
            for p in self._shard_files():
                if self._seq(p) < first:
                    older.extend(self._read_file(p))
            self._older = older
        return self._older + self._entries

    def recent(self, count: int = 20) -> list[dict]:
        """返回最近 count 条（时间从旧到新）；不够时惰性加载更老的分库。"""
        n = max(1, int(count))
        with self._lock:
            items = self._all_locked()
            return list(items[-n:])

    def find_media(self, code: str) -> dict | None:
        """按图片引用码找那条消息（`/小天才 查看图片 <code>` 用）。

        除了精确命中，还接受"码的前缀"：用户从历史里抄码容易少几位，
        只要前缀在全部记录里**唯一**就认定。多个命中返回 None（宁可让用户重抄，
        也不要发错图 —— 与表情取图"重名不猜"的原则一致）。

        先转 str：码可能来自 HTTP 请求体，JSON 里的数字会让 .strip() 抛 AttributeError。
        """
        want = str(code or "").strip().lower()
        if not want:
            return None
        with self._lock:
            items = list(self._all_locked())
        hits = [e for e in items
                if (e.get("media") or "").lower() == want]
        if len(hits) == 1:
            return hits[0]
        if len(want) >= 4:
            pref = [e for e in items
                    if (e.get("media") or "").lower().startswith(want)]
            if len(pref) == 1:
                return pref[0]
            if len(pref) > 1:
                return None
        return None

    def media_codes(self) -> list[str]:
        """出现过的图片引用码（诊断用）。"""
        with self._lock:
            items = list(self._all_locked())
        return [str(e["media"]) for e in items if e.get("media")]

    def sources(self) -> list[str]:
        """出现过的来源标签（按首次出现顺序）。"""
        with self._lock:
            items = self._all_locked()
        out: list[str] = []
        for e in items:
            tag = (e.get("source") or e.get("kind") or "").strip()
            if tag and tag not in out:
                out.append(tag)
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._all_locked())

    def shard_count(self) -> int:
        """分库文件数（诊断用）；单文件模式返回 1。"""
        return len(self._shard_files()) or (1 if self.path.exists() else 0)

    def memory_entries(self) -> int:
        """常驻内存里的条数（诊断用：应稳定在 read_shards 个分库以内）。"""
        return len(self._entries)
