# -*- coding: utf-8 -*-
"""历史媒体仓库：把转发过的图片/表情**原文件**留在本地，供 `/小天才 历史消息` 查看。

为什么需要
----------
消息库（msg_log）里只存文本。图片/表情的文本是占位符（"图片"、"表情开心"），
所以历史消息里只能看到这两个字，**看不到真正的图**（用户反馈）。QQ 里当时发过的
图片在聊天记录里也翻不回去（多目标、群消息、以及 QQ 侧的清理都会丢）。

做法
----
转发成功时顺手把同一份图片字节存到 `data/history_media/<sha1>.<ext>`，
消息库条目只记一个**短引用码**（`media` 字段，如 `a1b2c3d4e5f6`）。历史消息里显示成
`[图片 #a1b2c3d4e5f6]`；用户再用 `/小天才 查看图片 a1b2c3d4e5f6` 把那真正的图发回来。

这样做的三个好处：
* 消息库本身仍然很小（只多一个几十字节的字符串），读写历史不会变慢；
* 同一张图被转发到多个目标/多次补投时只存一份（按内容哈希去重）；
* 存的是原文件（GIF 动图也保留动画），不是缩略图。

容量控制（配置见 `msg_log.media_*`）
------------------------------------
* `enabled`：总开关（默认开）。
* `max_files`：最多保留多少个文件，超出按**最久未使用**删除。
* `max_kb`：单个文件的大小上限，超过就不存（只保留引用为空，历史里退回纯文本标记）。
* `dir`：存放目录，默认 `data/history_media`。

全部失败路径都**不抛异常**：存不下图最多是历史里看不到图，绝不能影响消息转发本身。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path

from . import runtime_paths

# 短引用码长度：12 位十六进制（48 bit）。同一份内容哈希取前 12 位，
# 冲突概率对单人聊天量级可以忽略；万一冲突也只是指向另一张同长度的图。
_CODE_LEN = 12
_CODE_RE = re.compile(r"^[0-9a-fA-F]{6,40}$")

# 允许的图片后缀（与 imgtool 能解码的格式一致；QQ 侧也认这些）
_SUFFIXES = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"}


def _suffix_for(data: bytes, hint: str = "") -> str:
    """按文件头判断后缀（比扩展名可靠：Nuitka 打包/截图路径经常没有扩展名）。"""
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if head[:2] == b"BM":
        return ".bmp"
    h = (hint or "").lower()
    for s in _SUFFIXES:
        if h.endswith(s):
            return s
    return ".png"          # 兜底：插件侧按 base64 图片解析，后缀不影响


class MediaStore:
    """历史媒体文件的落盘/读取/清理（线程安全，失败静默）。"""

    def __init__(self, directory: str | Path | None = None, enabled: bool = True,
                 max_files: int = 500, max_kb: int = 4096, logger=None):
        self.enabled = bool(enabled)
        self.max_files = max(0, int(max_files))          # 0 = 不限制
        self.max_bytes = max(0, int(max_kb)) * 1024       # 0 = 不限制
        self.logger = logger
        self.dir = Path(directory) if directory else runtime_paths.data_path("history_media")
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 基础
    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(msg)

    @staticmethod
    def code_of(data: bytes) -> str:
        """内容哈希 -> 短引用码（同一张图永远同一个码，重复转发不会存第二份）。"""
        return hashlib.sha1(data).hexdigest()[:_CODE_LEN]

    def path_of(self, code: str) -> Path | None:
        """引用码 -> 文件路径（不存在返回 None）。只接受十六进制码，避免路径穿越。"""
        c = (code or "").strip().lower()
        if not c:
            return None
        if not _CODE_RE.match(c):
            return None
        if len(c) > _CODE_LEN:            # 允许用更长的哈希前缀，取前 12 位匹配
            c = c[:_CODE_LEN]
        try:
            hits = sorted(self.dir.glob(f"{c}.*"))
        except OSError:
            return None
        if hits:
            return hits[0]
        # 允许用更短的前缀（用户手抄时容易漏字）：唯一命中才算
        if len(c) >= 6 and len(c) < _CODE_LEN:
            try:
                short = sorted(self.dir.glob(f"{c}*"))
            except OSError:
                return None
            if len(short) == 1:
                return short[0]
        return None

    # ------------------------------------------------------------------ 写入
    def save(self, data: bytes, hint: str = "") -> str:
        """保存一张图，返回短引用码；不保存时返回空串（调用方据此不写 media 字段）。"""
        if not self.enabled or not data:
            return ""
        if self.max_bytes and len(data) > self.max_bytes:
            self._log("info", f"[历史媒体] 图片 {len(data) // 1024} KB 超过上限"
                              f"（msg_log.media_max_kb={self.max_bytes // 1024}），"
                              "不存入历史（历史里只保留文字标记）")
            return ""
        code = self.code_of(data)
        suffix = _suffix_for(data, hint)
        target = self.dir / f"{code}{suffix}"
        with self._lock:
            try:
                if target.exists() and target.stat().st_size == len(data):
                    # 已经有同一份内容：只刷新 mtime（LRU 淘汰看它），不重复写盘
                    try:
                        os.utime(target, None)
                    except OSError:
                        pass
                    return code
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)
            except OSError as e:
                self._log("warning", f"[历史媒体] 保存失败（历史里看不到这张图）: {e}")
                return ""
            self._prune_locked()
        return code

    def _prune_locked(self) -> None:
        """按 max_files 淘汰最久未使用的文件（调用方已持锁）。"""
        if not self.max_files:
            return
        try:
            files = [p for p in self.dir.iterdir() if p.is_file() and p.suffix != ".tmp"]
        except OSError:
            return
        if len(files) <= self.max_files:
            return
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        for p in files[:len(files) - self.max_files]:
            try:
                p.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 读取
    def load(self, code: str) -> bytes:
        """按引用码读回图片字节（读不到返回空 bytes）。"""
        p = self.path_of(code)
        if p is None:
            return b""
        try:
            return p.read_bytes()
        except OSError as e:
            self._log("debug", f"[历史媒体] 读取失败: {e}")
            return b""

    def stats(self) -> dict:
        """诊断用：文件数与占用字节。"""
        try:
            files = [p for p in self.dir.iterdir() if p.is_file()]
        except OSError:
            return {"files": 0, "bytes": 0, "dir": str(self.dir)}
        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return {"files": len(files), "bytes": total, "dir": str(self.dir),
                "max_files": self.max_files, "max_kb": self.max_bytes // 1024}


def make_media_store(cfg: dict, logger=None) -> MediaStore:
    """按 config.yaml -> msg_log 构造（默认开启，全部字段可省略）。"""
    ml = (cfg.get("msg_log") or {})
    return MediaStore(
        directory=ml.get("media_dir") or None,
        enabled=bool(ml.get("media", True)),
        max_files=int(ml.get("media_max_files", 500) or 0),
        max_kb=int(ml.get("media_max_kb", 4096) or 0),
        logger=logger,
    )
