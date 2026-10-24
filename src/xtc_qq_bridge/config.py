# -*- coding: utf-8 -*-
"""配置读取：把 config.yaml 读成 dict（UTF-8/GBK 自适应）。"""
from __future__ import annotations

import json
import locale
from pathlib import Path


def read_text_auto(path: str) -> str:
    """读配置文本：优先 UTF-8（含 BOM），失败再按系统 ANSI（中文 Windows = GBK）读。

    中文 Windows 上 Python 的默认文件编码是 cp936，而配置模板/大多数编辑器
    保存的是 UTF-8；不显式处理会在读取时报 UnicodeDecodeError 或读成乱码。
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def load_config(path: str) -> dict:
    text = read_text_auto(path)
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        try:  # JSON 是 YAML 子集，无 pyyaml 时可用 JSON 格式配置
            return json.loads(text)
        except Exception:
            raise SystemExit("无法解析配置：请先 pip install pyyaml（或将 config.yaml 写成 JSON 格式）")
    except Exception as e:
        raise SystemExit(f"配置文件解析失败: {e}")


