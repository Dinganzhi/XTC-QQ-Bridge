# -*- coding: utf-8 -*-
"""root 访问：在设备已 root（Magisk / KernelSU 等）时，经 `su` 读取 App 私有目录。

用途
----
1. **取原图更可靠**：平时只能读外部存储 `/sdcard/Android/data/<pkg>`；Android 11+
   的分区存储会让该目录不可读，且 App 也可能把同一份文件写到内部
   `/data/data/<pkg>/`。开启 root 后内部目录也能读，表情/照片原图的命中率更高。
2. **诊断**：`--check` / `--debug adb-info` 会报告 root 是否可用。

重要限制（已实测，2026-10）
--------------------------
小天才 App 的聊天数据库 `databases/weichat.db` 与 `shared_prefs/weichat*.xml`
都是**加密**的（SQLCipher / 加密 prefs，文件头不是 `SQLite format 3`）。
因此**即使 root 也读不到逐条消息的时间戳** —— 这不是权限问题，而是数据在落盘时
就被 App 加密了。消息时间仍以"标签 + 观测时间"为准（见 bridge/media.py）。
"""
from __future__ import annotations

import base64
import shlex
import threading
from pathlib import Path

DEFAULT_SU = "su -c"


class RootAccess:
    """设备 root 能力的封装（可用性探测结果会缓存）。"""

    def __init__(self, adb, cfg: dict | None = None, logger=None) -> None:
        _cfg = cfg or {}
        self.adb = adb
        self.logger = logger
        self.enabled = bool(_cfg.get("enabled", False))
        self.su = str(_cfg.get("su") or DEFAULT_SU).strip() or DEFAULT_SU
        try:
            self.timeout = max(5.0, float(_cfg.get("timeout", 30) or 30))
        except (TypeError, ValueError):
            self.timeout = 30.0
        self._available: bool | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 基础
    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(msg)

    def available(self, refresh: bool = False) -> bool:
        """`su` 是否真的能拿到 root（结果缓存；refresh=True 重新探测）。"""
        if not self.enabled or self.adb is None:
            return False
        with self._lock:
            if self._available is not None and not refresh:
                return self._available
            ok = False
            try:
                out = self.adb.shell(f"{self.su} 'id'", timeout=self.timeout) or ""
                ok = "uid=0" in out
            except Exception as e:  # noqa: BLE001 没有 su / 用户拒绝授权都算不可用
                self._log("debug", f"root 探测失败: {e}")
            self._available = ok
            if ok:
                self._log("info", "[root] 已获得 root 权限，可读取 App 私有目录")
            else:
                self._log("warning", "[root] 配置了 root.enabled，但 `su` 不可用（未授权/未 root），"
                                     "已自动退回普通权限")
            return ok

    def shell(self, cmd: str, timeout: float | None = None) -> str:
        """以 root 执行一条 shell 命令（自动加引号）。"""
        if not self.available():
            raise RuntimeError("root 不可用")
        quoted = cmd.replace("'", "'\\''")
        return self.adb.shell(f"{self.su} '{quoted}'", timeout=timeout or self.timeout) or ""

    # ------------------------------------------------------------------ 文件
    def exists(self, path: str) -> bool:
        try:
            out = self.shell(f"test -e {shlex.quote(path)} && echo yes || echo no")
        except Exception:  # noqa: BLE001
            return False
        return out.strip().endswith("yes")

    def list_files(self, directory: str, max_bytes: int | None = None,
                   min_bytes: int = 0) -> tuple[list[tuple[float, int, str]], float]:
        """列目录：[（mtime, size, path）] 与设备当前时间；与 EmojiStore 的格式一致。"""
        try:
            out = self.shell(
                f"date +%s; find {shlex.quote(directory)} -type f "
                f"-exec stat -c '%Y %s %n' {{}} \\; 2>/dev/null")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"[root] 列目录失败 {directory}: {e}")
            return [], 0.0
        rows: list[tuple[float, int, str]] = []
        now = 0.0
        for line in out.splitlines():
            line = line.strip()
            if not now and line.isdigit():
                now = float(line)
                continue
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            try:
                mtime, size = float(parts[0]), int(parts[1])
            except ValueError:
                continue
            if min_bytes and size < min_bytes:
                continue
            if max_bytes is not None and size > max_bytes:
                continue
            rows.append((mtime, size, parts[2]))
        return rows, now or 0.0

    def read_bytes(self, path: str, timeout: float | None = None) -> bytes:
        """读整个文件（base64 传输，二进制安全）。"""
        out = self.shell(f"base64 {shlex.quote(path)}", timeout=timeout or max(self.timeout, 60))
        try:
            return base64.b64decode("".join(out.split()))
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"[root] 读取文件失败 {path}: {e}")
            return b""

    def read_head(self, path: str, nbytes: int) -> bytes:
        """只读文件头若干字节（判格式/尺寸用，省流量）。"""
        try:
            out = self.shell(f"head -c {int(nbytes)} {shlex.quote(path)} | base64")
        except Exception as e:  # noqa: BLE001
            self._log("debug", f"[root] 读文件头失败 {path}: {e}")
            return b""
        try:
            return base64.b64decode("".join(out.split()))
        except Exception:  # noqa: BLE001
            return b""

    # ------------------------------------------------------------------ 诊断
    def private_roots(self, package: str) -> list[str]:
        """开启 root 时可额外搜索的 App 私有目录（内部存储）。"""
        if not package:
            return []
        base = f"/data/data/{package}"
        return [base]


def quote_ok(cmd: str) -> str:
    """给外部脚本用：单引号安全包裹（便于日志展示）。"""
    return "'" + cmd.replace("'", "'\\''") + "'"


def describe(root: RootAccess | None, package: str = "") -> list[str]:
    """`--check` / `--debug adb-info` 用的说明行。"""
    if root is None or not root.enabled:
        return ["root          : 未启用（config.yaml -> root.enabled）"]
    ok = root.available()
    lines = [f"root          : {'可用（su）' if ok else '已启用但不可用（su 未授权/未 root）'}"]
    if ok and package:
        lines.append(f"私有目录      : /data/data/{package}（可读，用于取原图）")
        lines.append("聊天数据库    : 已加密（SQLCipher），root 也读不到逐条时间")
    return lines
