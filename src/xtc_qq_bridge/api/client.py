# -*- coding: utf-8 -*-
"""小天才云端 HTTP API 客户端（实验性，默认不启用）。

设计原则
--------
1. **默认关闭**：`enabled=False` 时任何方法都直接返回"未启用"，不发网络请求。
2. **不碰现有链路**：本模块只被新代码引用，ADB 路径完全不受影响。
3. **凭据不落日志**：`HttpAccount.__repr__` 已脱敏；异常信息里也不带凭据。
4. **失败可诊断**：返回结构化的 `ApiResult`，把"网络失败/签名失败/业务失败"
   分开，便于定位（签名错误通常表现为业务码异常或 401）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .crypto import (SUCCESS_CODES, HttpAccount, ImooCrypto,
                     build_signed_request, dumps_compact)

__all__ = ["ApiError", "ApiResult", "ImooApiClient", "DEFAULT_BASE"]

# 核心 API 的 base。注意：`api.watch.okii.com` 虽然能解析，但据 imoore 项目的
# 静态分析它只是 HttpConstants.HOST_NAME_ARRAYS[1]，**不是可用 base**；
# 真正承载账号/设备/群聊等接口的是 `watch.okii.com`。
DEFAULT_BASE = "https://watch.okii.com"

# 实测不需要设备 token 就能调通的接口。只放确认过的，宁可少放。
# `/server/domain/list` 用 GET 时返回三套环境的域名表（见文档 §4.3.0），
# 因此可以拿它做"网络与签名链路是否通"的自检。
_TOKEN_FREE_PATHS = frozenset({"/server/domain/list"})


class ApiError(RuntimeError):
    """调用失败。`kind` 区分失败层级，便于排错。"""

    def __init__(self, kind: str, message: str, *, code: str = "", url: str = "") -> None:
        super().__init__(f"[{kind}] {message}")
        self.kind = kind          # disabled / network / http / crypto / business
        self.code = code
        self.url = url


@dataclass
class ApiResult:
    """一次调用的结果。`ok=False` 时 `error` 有值，`data` 为空。"""

    ok: bool
    data: Any = None
    code: str = ""
    error: str = ""
    kind: str = ""
    elapsed: float = 0.0
    raw_len: int = 0
    extra: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


class ImooApiClient:
    """带签名信封的最小 HTTP 客户端。

    只依赖标准库（urllib），不引入 requests；加密层见 `crypto.py`。
    """

    def __init__(self, account: HttpAccount | None = None, *,
                 base: str = DEFAULT_BASE, enabled: bool = False,
                 timeout: float = 10.0, logger=None) -> None:
        self.account = account or HttpAccount()
        self.base = str(base or DEFAULT_BASE).rstrip("/")
        self.enabled = bool(enabled)
        # 收敛到 [1, 300]：inf 会让 urlopen 永不超时（挂死），nan 会让比较失效。
        try:
            t = float(timeout or 10.0)
        except (TypeError, ValueError, OverflowError):
            t = 10.0
        self.timeout = t if 1.0 <= t <= 300.0 else 10.0
        self.logger = logger
        self._crypto = ImooCrypto(self.account.encrypt_key)

    # ---------------------------------------------------------------- 日志
    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(msg)

    # ---------------------------------------------------------------- 对外
    def get(self, path: str) -> ApiResult:
        """签名 GET（无请求体）。"""
        return self._send("GET", path, "")

    def post(self, path: str, body: dict | list | None = None) -> ApiResult:
        """签名 POST（请求体加密）。"""
        return self._send("POST", path, dumps_compact(body if body is not None else {}))

    def ready(self, path: str = "") -> tuple[bool, str]:
        """是否具备调用条件；返回 (可用, 原因)。

        `path` 用于判断该接口是否属于"免凭据"白名单 —— 实测
        `GET /server/domain/list` 不需要 token 就能调通（见文档 §4.3.0），
        所以不能因为它缺 token 就把这个接口也拦掉，否则连"网络与签名链路
        是否通"都无法自检。
        """
        if not self.enabled:
            return False, "api.enabled 未开启"
        if not self.account.account_id:
            return False, "缺少账号凭据（api.account_id）"
        if path and self.is_token_free(path):
            return True, ""
        if not self.account.bind_number or not self.account.chip_id:
            return False, "缺少设备凭据（api.bind_number / api.chip_id）"
        return True, ""

    @staticmethod
    def is_token_free(path: str) -> bool:
        """该接口是否不需要设备 token。

        目前只实测确认了 `/server/domain/list` 一个；其余接口在缺 token 时
        都会返回 `000003`，因此不放进白名单。
        """
        clean = str(path or "").split("?", 1)[0].rstrip("/")
        return clean in _TOKEN_FREE_PATHS

    # ---------------------------------------------------------------- 内部
    def _send(self, method: str, path: str, body: str) -> ApiResult:
        started = time.monotonic()

        ok, why = self.ready(path)
        if not ok:
            return ApiResult(ok=False, error=why, kind="disabled",
                             elapsed=time.monotonic() - started)

        url = path if path.startswith("http") else f"{self.base}{path}"
        try:
            headers, out_body = build_signed_request(self.account, method, url, body)
        except Exception as e:  # noqa: BLE001 信封构造失败（配置/密钥问题）
            return ApiResult(ok=False, error=f"请求信封构造失败: {e}", kind="crypto",
                             elapsed=time.monotonic() - started)

        req = urllib.request.Request(
            url, method=method.upper(),
            data=out_body.encode("utf-8") if out_body else None,
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read()[:200].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 读错误体失败不影响判定
                detail = ""
            # 401/403 多半是签名或凭据问题，单独指出，省得去查网络
            hint = ("（签名或凭据可能不对：检查 api.encrypt_key / chip_id）"
                    if e.code in (401, 403) else "")
            return ApiResult(ok=False, code=str(e.code),
                             error=f"HTTP {e.code}{hint} {detail}".strip(), kind="http",
                             elapsed=time.monotonic() - started)
        except Exception as e:  # noqa: BLE001 网络层失败（DNS/超时/证书）
            return ApiResult(ok=False, error=f"网络请求失败: {e}", kind="network",
                             elapsed=time.monotonic() - started)

        return self._parse(raw, url, status, started)

    def _parse(self, raw: bytes, url: str, status: int, started: float) -> ApiResult:
        """解密并解析响应。v3 响应体是 base64(AES-ECB(gzip(JSON)))。"""
        text = raw.decode("utf-8", "replace")
        elapsed = time.monotonic() - started
        plain = text
        if self._crypto.version:
            try:
                plain = self._crypto.decrypt_body(text)
            except Exception as e:  # noqa: BLE001 解密失败：可能服务端回了明文错误
                # 退回明文再试一次：很多错误响应不加密
                plain = text
                self._log("debug", f"[api] 响应解密失败，按明文解析: {e}")
        try:
            envelope = json.loads(plain)
        except Exception as e:  # noqa: BLE001
            return ApiResult(ok=False, code=str(status),
                             error=f"响应不是 JSON（{len(raw)} 字节）: {e}",
                             kind="crypto", elapsed=elapsed, raw_len=len(raw))

        code = str(envelope.get("code", envelope.get("resultCode", "")) or "")
        if code and code not in SUCCESS_CODES:
            msg = envelope.get("desc") or envelope.get("message") or ""
            return ApiResult(ok=False, code=code, error=str(msg) or "业务返回失败",
                             kind="business", elapsed=elapsed, raw_len=len(raw),
                             data=envelope.get("data"))
        return ApiResult(ok=True, code=code, data=envelope.get("data", envelope),
                         elapsed=elapsed, raw_len=len(raw))


def account_from_config(cfg: dict) -> HttpAccount:
    """把 config.yaml 的 `api:` 段转成 HttpAccount。

    容错原则：任何字段类型不对都退回默认值，**绝不抛异常** —— 这个模块是实验性的，
    配置写错不应该让主程序起不来。真正的"能不能调用"由 `ImooApiClient.ready()` 判断。
    """
    section = cfg.get("api") if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        section = {}

    def _text(key: str) -> str:
        v = section.get(key, "")
        return "" if v is None else str(v).strip()

    regist_raw = section.get("regist_id", 0)
    try:
        regist_id = 0 if isinstance(regist_raw, bool) else int(regist_raw or 0)
        # registId 在服务端按整型处理；超出 64 位无符号范围的一律视为无效配置，
        # 否则会把一个荒谬的大数拼进签名串，得到一个"看起来发了但永远失败"的请求。
        if not 0 <= regist_id <= 0xFFFFFFFFFFFFFFFF:
            regist_id = 0
    except (TypeError, ValueError, OverflowError):
        regist_id = 0

    return HttpAccount(
        bind_number=_text("bind_number"),
        chip_id=_text("chip_id"),
        account_id=_text("account_id"),
        regist_id=regist_id,
        inner_model=_text("inner_model"),
        soft_version=_text("soft_version"),
        encrypt_key=_text("encrypt_key"),
        grey=_text("grey") or "0",
        data_center=_text("data_center") or "CN_BJ",
    )


def client_from_config(cfg: dict, logger=None) -> ImooApiClient:
    """按配置构造客户端。`api.enabled` 缺省为 false（实验性功能不默认开启）。"""
    section = cfg.get("api") if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        section = {}

    enabled = section.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(enabled)

    base = section.get("base") or DEFAULT_BASE
    try:
        timeout = float(section.get("timeout", 10) or 10)
    except (TypeError, ValueError, OverflowError):
        timeout = 10.0
    # inf/nan/超大值会让 urlopen 永不超时（请求挂死、线程不释放），
    # 这里收敛到 [1, 300] 秒；nan 的比较恒为 False，用 `not (1 <= x <= 300)` 一并拦住。
    if not 1.0 <= timeout <= 300.0:
        timeout = 10.0

    return ImooApiClient(account_from_config(cfg), base=str(base),
                         enabled=enabled, timeout=timeout, logger=logger)
