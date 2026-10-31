# -*- coding: utf-8 -*-
"""日志工具：控制台 + 滚动文件双输出。

控制台编码兜底（重要）：
中文 Windows 的控制台默认是 GBK（cp936），而日志文本里可能带有 GBK 无法表示的字符
（例如某些 emoji、日文标点、特殊符号）。此时 `StreamHandler` 会在
`stream.write()` 抛 `UnicodeEncodeError`，logging 会打印一句 "--- Logging error ---"
后**整条日志丢失**——表现就是"控制台无法看到收到的命令/消息"。
这里统一把标准输出/错误设成 `errors="replace"`（保留原编码，只把无法表示的字符换成 ?），
日志再也不会因为个别字符而整条消失。
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 每次启动生成的日志文件名：<主干>-YYYYmmdd-HHMMSS.log（例如 bridge-20261018-194217.log）
_RUN_LOG_RE = re.compile(r"^(?P<stem>.+?)-\d{8}-\d{6}(?:-\d+)?\.log(?:\.\d+)?$")


def _resolve(name: str) -> Path:
    """相对路径按"可写数据目录"解析（Nuitka onefile 下日志写在 exe 旁边）。"""
    p = Path(name)
    try:
        # 注意：本模块在 xtc_qq_bridge.utils 下，runtime_paths 在上一级包里，
        # 写成 `from . import runtime_paths` 必然 ImportError，于是静默退化成
        # "按当前工作目录解析"—— 从别的目录启动时日志就写到那里去了。
        from .. import runtime_paths
        return runtime_paths.log_path(name)
    except Exception:  # noqa: BLE001 单独使用本模块时不强依赖
        return p if p.is_absolute() else Path.cwd() / p


def run_log_path(base: str = "logs/xtc-qq-bridge.log", keep: int = 0) -> Path:
    """为**本次启动**生成一个新的日志文件路径。

    为什么每次启动一个新文件（用户要求）：桥接通常长时间挂在后台，单个日志会越来越长，
    出问题时很难定位"这次启动之后发生了什么"。按启动时间分文件后：
      * 文件名直接给出启动时刻（`bridge-20261018-194217.log`）；
      * 每次启动只写自己的文件，**默认不删除任何旧日志**（`keep=0`）；
        需要自动清理时才把 `logging.keep_runs` 设成正数（保留最近 N 次启动）；
      * 文件名里的 `-2`、`-3` 用于同一秒内重复启动的去重。
    """
    path = _resolve(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{stem}-{stamp}.log")
    n = 1
    while candidate.exists():                      # 同一秒内重复启动：加序号
        n += 1
        candidate = path.with_name(f"{stem}-{stamp}-{n}.log")
    if keep > 0:
        _prune_run_logs(path.parent, stem, keep)
    return candidate


def _prune_run_logs(folder: Path, stem: str, keep: int) -> None:
    """只保留最近 keep 个"本次启动"日志（滚动产生的 .1/.2 后缀一起算）。

    仅在 `logging.keep_runs > 0` 时调用；默认的 0 表示**永不清理**。
    """
    if keep <= 0:
        return
    try:
        runs: dict[str, list[Path]] = {}
        for p in folder.glob(f"{stem}-*.log*"):
            m = _RUN_LOG_RE.match(p.name)
            if not m or m.group("stem") != stem:
                continue
            # bridge-20261018-194217.log / .log.1 -> 同一组
            group = re.sub(r"\.log(?:\.\d+)?$", "", p.name)
            runs.setdefault(group, []).append(p)
        if len(runs) <= keep:
            return
        for group in sorted(runs)[:-keep]:
            for p in runs[group]:
                try:
                    p.unlink()
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 清理失败不影响启动
        pass


def make_console_tolerant() -> None:
    """让普通 print() 在非 UTF-8 控制台上也不会因为中文而崩。

    为什么需要：日志走的是 StreamHandler（已用 _tolerant_stream 兜底），但程序里
    还有大量 print()（横幅、--verify 输出、编译脚本进度）。在 Windows CI 的
    cp1252 控制台上，print 中文会直接抛
        UnicodeEncodeError: 'charmap' codec can't encode characters ...
    导致编译/自检整个失败（实测：Windows 两个平台的 CI 编译就是这么挂的）。
    这里只改 errors（保留原编码），失败就静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")   # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 老版本/被替换的流：只能放弃
            pass


def _tolerant_stream(stream):
    """让标准流在遇到无法编码的字符时用 ? 代替，而不是抛异常丢日志。"""
    if stream is None:
        return None
    try:
        stream.reconfigure(errors="replace")   # Python 3.7+：保留原编码，只改错误处理
        return stream
    except Exception:  # noqa: BLE001 老版本/被替换的流：退化为包装器
        pass

    class _Tolerant:
        def __init__(self, raw):
            self._raw = raw

        def write(self, data):
            try:
                return self._raw.write(data)
            except UnicodeEncodeError:
                enc = getattr(self._raw, "encoding", None) or "utf-8"
                return self._raw.write(data.encode(enc, "replace").decode(enc, "replace"))

        def flush(self):
            try:
                self._raw.flush()
            except Exception:  # noqa: BLE001
                pass

        def __getattr__(self, item):
            return getattr(self._raw, item)

    return _Tolerant(stream)


def setup_logger(level: str = "INFO", file: str | None = None,
                 name: str = "xtc-qq-bridge", console: bool = True) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:  # 已初始化过
        return logger

    try:
        logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    except Exception:
        logger.setLevel(logging.INFO)

    fmt = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    if console:
        ch = logging.StreamHandler(_tolerant_stream(sys.stdout))
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    if file:
        try:
            # 相对路径按"可写数据目录"解析：Nuitka onefile 下日志写在 exe 旁边，
            # 而不是退出即被删除的临时解包目录（见 runtime_paths.py）
            try:
                from . import runtime_paths
                file = str(runtime_paths.log_path(file))
            except Exception:  # noqa: BLE001 单独使用本模块时不强依赖
                pass
            from pathlib import Path
            Path(file).parent.mkdir(parents=True, exist_ok=True)  # 自动创建日志目录
            fh = RotatingFileHandler(file, maxBytes=5 * 1024 * 1024, backupCount=3,
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception as e:  # 日志文件无法打开不致命
            logger.warning(f"日志文件不可用({file}): {e}")

    return logger


