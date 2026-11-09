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


def _as_mapping(obj, path: str) -> dict:
    """配置必须是"键值映射"。返回非 dict 时给出明确报错。

    为什么必须拦：yaml.safe_load 对 `- a\n- b` 这类内容返回 list，对 `hello`
    返回 str。下游全部是 cfg.get(...)，拿到非 dict 会抛
    `AttributeError: 'list' object has no attribute 'get'` —— 用户完全看不出
    是自己把 config.yaml 写成了列表，只会以为程序坏了。
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    raise SystemExit(
        f"配置文件格式不对：{path} 的顶层必须是「键: 值」的映射，"
        f"当前是 {type(obj).__name__}。请对照 config.example.yaml 检查"
        "（常见原因：开头多了 `- ` 列表符号，或整份文件只是一行文本）。")


def load_config(path: str) -> dict:
    text = read_text_auto(path)
    try:
        import yaml
        return _as_mapping(yaml.safe_load(text), path)
    except ImportError:
        try:  # JSON 是 YAML 子集，无 pyyaml 时可用 JSON 格式配置
            return _as_mapping(json.loads(text), path)
        except SystemExit:
            raise
        except Exception:
            raise SystemExit("无法解析配置：请先 pip install pyyaml（或将 config.yaml 写成 JSON 格式）")
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"配置文件解析失败: {e}")


