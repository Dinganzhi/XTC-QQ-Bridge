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

import json
import threading
import time
from pathlib import Path

from . import runtime_paths

_SHARD_DIGITS = 4


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
        self._save_warned = False                   # 写盘失败只告警一次
        self.last_error = ""                        # 最近一次写盘失败原因（诊断用）
        self._load()

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

    def _read_file(self, p: Path) -> list[dict]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 读取失败不致命
            return []
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

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
                self._mem_shards.append((self._seq(p), len(self._entries)))
                self._entries.extend(self._read_file(p))
            return
        # 没有分库文件：按单文件处理
        self._entries = self._read_file(self.path) if self.path.exists() else []
        self._mem_shards = [(1 if self.shard_size else 0, 0)]
        if not self.shard_size and self.cap:
            self._entries = self._entries[-self.cap:]

    def _next_seq(self) -> int:
        seqs = [self._seq(p) for p in self._shard_files()]
        seqs = [s for s in seqs if s > 0]
        return (max(seqs) + 1) if seqs else 1

    def _save(self) -> None:
        """只重写当前分库（这是分库带来的效率提升）。"""
        target = self._current
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(target) + ".tmp")
            tmp.write_text(json.dumps(self._entries[self._cur_start:], ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(target)
            self.last_error = ""
        except Exception as e:  # noqa: BLE001 写盘失败不影响运行，但要让用户知道
            # 静默失败会让"历史消息/是否已处理"的判定库悄悄停止更新（磁盘满、
            # 目录只读、被杀毒软件占用等），因此首次失败记一条告警。
            self.last_error = f"{type(e).__name__}: {e}"
            if not self._save_warned:
                self._save_warned = True
                if self.logger is not None:
                    self.logger.warning(f"消息库写入失败（历史与判重将停留在内存中）: {self.last_error}")

    def _rotate_if_needed(self) -> None:
        """当前分库写满 -> 换新分库（并把更老的分库移出内存）。"""
        if not self.shard_size:
            return
        if len(self._entries) - self._cur_start < self.shard_size:
            return
        self._mem_shards.append((self._next_seq(), len(self._entries)))
        self._trim_memory()

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
               source_id: str = "") -> None:
        """追加一条消息。

        kind:      'xtc'（手表侧）/ 'qq'（QQ 发入小天才）
        source:    人类可读来源标签，如「手表」「QQ私聊 10001」「QQ群 123456」
        source_id: 来源标识（QQ 号 / 群号 / 联系人名），便于按来源过滤
        """
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._rotate_if_needed()
            self._entries.append({
                "t": t if t is not None else time.time(),
                "kind": kind,
                "sender": (sender or "").strip(),
                "text": text,
                "source": (source or "").strip(),
                "source_id": str(source_id or "").strip(),
            })
            if not self.shard_size and self.cap and len(self._entries) > self.cap:
                del self._entries[:len(self._entries) - self.cap]
            self._save()
            self._enforce_shard_cap()

    def seen(self, text: str, kind: str = "") -> bool:
        """库里有没有这条消息（只查已加载的分库，默认就是最新那个）。

        这是"从最新往回走、撞库即停"的判定入口：命中说明这条已经处理过。
        """
        needle = (text or "").strip()
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
        """
        if not self.shard_size:
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
