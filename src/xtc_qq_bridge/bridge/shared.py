# -*- coding: utf-8 -*-
"""消息桥接调度层：轮询小天才新消息 -> 转发（当前支持 log 打印 /
AstrBot 插件端点两种模式），并负责去重、回声过滤与 ADB 断线重连。

反向（QQ->小天才）由 qq_webhook.py 调用 bridge.forward_to_xiaotiancai()，
webhook.enabled=true 且 NapCat/插件回调就绪后启用。
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..utils.deduplicate import Deduplicator, EchoFilter, HistoryFilter
from ..utils import imgtool
from ..msg_log import MessageLog
from ..xiaotiancai import LOGIN_LOGGED_IN, LOGIN_NOT_LOGGED_IN, LOGIN_UNKNOWN
from .. import runtime_paths


def make_forwarder(cfg: dict, logger=None):
    """按 config.forward.mode 构造转发器。"""
    fwd = (cfg.get("forward") or {})
    mode = fwd.get("mode", "log")
    if mode == "plugin":
        pc = fwd.get("plugin", {})
        from ..plugin_client import PluginClient
        client = PluginClient(base_url=pc.get("base_url", "http://127.0.0.1:11452"),
                              token=pc.get("token", ""))
        return PluginForwarder(client, logger)
    return LogForwarder(logger)


class LogForwarder:
    """调试占位：只打印，不真正发送。"""

    def __init__(self, logger=None):
        self.logger = logger

    def send(self, target_type, target_id, message: str) -> bool:
        if self.logger:
            self.logger.info(f"[转发-占位] {target_type}:{target_id} <- {message}")
        else:
            print(f"[转发-占位] {target_type}:{target_id} <- {message}")
        return True

    def send_detail(self, target_type, target_id, message: str) -> tuple:
        return self.send(target_type, target_id, message), ""

    def send_image(self, target_type, target_id, image_b64, caption="") -> tuple:
        """log 模式：只打印不发送（表情包也一样，便于离线调试）。返回 (ok, why)。"""
        if self.logger:
            self.logger.info(f"[转发-占位] 图片 {target_type}:{target_id} "
                             f"{len(image_b64)} 字节 base64 <- {caption}")
        return True, ""

    def reply_result(self, request_id: str, message: str) -> bool:
        if self.logger:
            self.logger.info(f"[占位-回传] {request_id}: {message}")
        return True

    # xtc 侧命令需要的 QQ 数据查询：log 模式无 QQ 数据源，一律返回 None
    def qq_search(self, keyword, allow_from, allow_groups, limit=30):
        return None

    def qq_online(self, minutes, allow_from, allow_groups):
        return None

    def qq_remind(self, group_id, qq_id, text=""):
        return None


class PluginForwarder:
    def __init__(self, client, logger=None):
        self.client = client
        self.logger = logger
        self._last_err_log = float("-inf")   # 初值用 -inf：monotonic 零点任意，0.0 会误判成"刚报过"
        self._err_log_interval = 60.0  # 同一故障最多 60s 报一次，避免重复输出

    def send(self, target_type, target_id, message: str) -> bool:
        ok, detail = self.send_detail(target_type, target_id, message)
        if not ok and self.logger:
            now = time.monotonic()
            if now - self._last_err_log >= self._err_log_interval:
                self.logger.error(f"转发到 AstrBot 插件失败：{detail or '未知原因'}")
                self._last_err_log = now
        return ok

    def send_detail(self, target_type, target_id, message: str) -> tuple:
        """转发并带出真实失败原因（QQ 侧无法发送 / 超时 / 连不上插件 是三种完全不同的病）。"""
        fn = getattr(self.client, "send_detail", None)
        if fn is None:                       # 兼容旧的 client
            return self.client.send(target_type, target_id, message), ""
        return fn(target_type, target_id, message)

    def send_image(self, target_type, target_id, image_b64, caption="") -> tuple:
        """转发一张图片（表情包，base64）。返回 (ok, why)。

        注意：这个方法**必须**在包装层也暴露 —— 实机踩过：`PluginClient` 有了 send_image，
        但桥接用的是 `PluginForwarder` 包装类，包装层没有就 `getattr(..., 'send_image', None)`
        得到 None，于是每张表情都退化成文字（日志里那句"转发器不支持图片"）。
        """
        fn = getattr(self.client, "send_image", None)
        if fn is None:
            return False, "插件客户端不支持图片（plugin_client.py 过旧）"
        ok, why = fn(target_type, target_id, image_b64, caption)
        if not ok and self.logger:
            now = time.monotonic()
            if now - self._last_err_log >= self._err_log_interval:
                self.logger.error(f"表情图转发失败：{why or '未知原因'}")
                self._last_err_log = now
        return ok, why

    def reply_result(self, request_id: str, message: str) -> bool:
        return self.client.reply_result(request_id, message)

    def qq_search(self, keyword, allow_from, allow_groups, limit=30):
        return self.client.qq_search(keyword, allow_from, allow_groups, limit)

    def qq_online(self, minutes, allow_from, allow_groups):
        return self.client.qq_online(minutes, allow_from, allow_groups)

    def qq_remind(self, group_id, qq_id, text=""):
        return self.client.qq_remind(group_id, qq_id, text)



# 动态 __all__：让各子模块 `from .shared import *` 也能拿到下划线开头的内部常量
__all__ = [__name for __name in list(globals()) if not __name.startswith('__')]
