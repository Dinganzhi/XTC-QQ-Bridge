# -*- coding: utf-8 -*-
"""小天才（imoo）云端 HTTP API 客户端。

实验性模块：**默认不启用**，只有显式配置 `api.enabled: true` 才会被调用。
现有功能（ADB 界面自动化）完全不依赖它，两条路径互不影响。

为什么单独一个包
----------------
当前桥接靠 ADB 读界面、点发送，慢（一次 dump 约 3 秒）且依赖界面状态。
小天才手机端与手表之间实际走的是自家云端 HTTP 接口，若能直接调用，
发消息就从"操作界面"变成"一次 HTTPS 请求"。

协议来源与致谢
--------------
本包的签名信封（`Base-Request-Param` + `Eebbk-Sign` + AES-ECB 请求体）与
TLV/IM 封装格式，参考了 GitHub 上的公开项目 **imoo-client**
（作者 HIbingai，MIT 许可）：
    https://github.com/HIbingai/imoo-client

该项目的 `crypto.py` / `transport.py` / `tlv.py` 描述了协议结构；本包在其基础上
重写为与本项目风格一致、无第三方加密依赖（改用标准库 + 可选 cryptography）的实现，
并补充了抓包验证结论（见 `docs/imoo-api.md`）。协议本身的归属仍属于小天才官方，
本项目仅做互操作性研究。

重要限制（实测结论，2026-11）
----------------------------
* 手机 App 与手表的**消息通道**走 `im3.okii.com:8002` 的裸 TCP 私有协议，
  **不经过 HTTP 代理**，因此 mitmproxy 抓不到；HTTP 侧只有账号/配置/日志接口。
* App 的 `network_security_config` 只信任 `system` 证书（`targetSdk=36`），
  未 root 时安装用户 CA 也**无法解密**其 HTTPS 流量。
* 因此本包的请求格式来自上述公开项目，而非本次抓包逆向；抓包只用于
  确认域名、端口与协议类型。

安全边界
--------
* 凭据（bind_number / chip_id / account_id / encrypt_key）**只从配置读取**，
  绝不写入日志、异常信息或任何落盘文件。
* 所有对外方法在 `enabled=False` 时立即返回"未启用"，不发起任何网络请求。
"""
from __future__ import annotations

from .client import ApiError, ApiResult, ImooApiClient
from .crypto import HttpAccount, ImooCrypto

__all__ = [
    "ApiError",
    "ApiResult",
    "HttpAccount",
    "ImooApiClient",
    "ImooCrypto",
]
