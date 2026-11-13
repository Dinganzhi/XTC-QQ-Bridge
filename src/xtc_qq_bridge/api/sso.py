# -*- coding: utf-8 -*-
"""小天才登录（SSO）信封：`/app/*` 那一套签名与加密。

与核心 API 的信封不同
--------------------
核心 API（`watch.okii.com`）用 `Base-Request-Param` + `Eebbk-Sign` 头；
登录（`account.okii.com/app/*`）用**表单参数里塞一个 sign**，两者算法不一样：

    核心 API:  Eebbk-Sign = MD5(url + BRP明文 + body明文 + aes_key)  大写
    登录 SSO:  sign       = MD5(url + "k1=v1##k2=v2" + secret)       小写

登录这套的要点（每一条都踩过坑）：

1. `sign` 是对**完整 URL**（含 scheme 与 host）算的，不是路径。
2. 参数按 key **大小写不敏感**排序，用 `##` 连接，url 与第一个参数之间**没有**分隔符。
3. `timestamp` 格式是 `yyyy-MM-dd HH:mm:ss`（24 小时制）。
4. `days` 必须是**字符串** `"-1"`。传整数会触发服务端 500
   （`Optional int parameter 'days' is present but cannot be translated...`）。
5. `deviceId` 可以填任意值，服务端不校验它是不是真机的。
6. 请求体是 `base64(AES-128-ECB-PKCS5(JSON))`，`aesKey` 是 16 个 ASCII 字符。
7. `EncryptKey` 头是 `base64(RSA-1024-PKCS1v1.5(aesKey))`，公钥来自 `POST /app/rsakey`。
8. 响应可能是明文 JSON，也可能是带引号的 base64 密文 —— 两种都要处理。

`/app/rsakey` 本身只签名、不加密，响应里 `data` 是 base64 的 X.509 SPKI。

出处
----
协议结构参考 [dachrisch/imoore](https://github.com/dachrisch/imoore)（MIT）
的 `docs/signing-and-crypto.md` 与 `docs/authentication.md`，
appId 与 secret 的对应关系来自该项目从原生库提取的公开结果。
本模块的实现与结论已在真实服务器上验证（见 `docs/imoo-api.md`）。

免责声明：仅用于自己账号的互操作性研究。凭据不要提交到版本库。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# 国内包（com.xtc.watch）的 appId 与对应的签名密钥。
# 这份表来自 imoore 项目对 librubbish.so 的静态提取，已在真实服务器验证。
APP_SECRETS = {
    "11": "LKyF8jThHbZfUeTP",
    "31": "CXy5qhZZ0qHkjvgh",
    "33": "5sacCZns3z9sTcCX",
    "35": "NQIiQkcCIGhfn2s9",
    "37": "thqHjysVOkGETPoY",
    "48": "5sacCZns3z9sTcCX",   # 与 33 相同（客户端代码如此），服务端报"签名无效"
}

DEFAULT_APP_ID = "11"
# 客户端常量里写死的 mac 兜底值；不是用户凭据。
DEFAULT_MAC = "64ba24f2457d405dbf40346f96d80934"

DEFAULT_SSO_BASE = "https://account.okii.com"

# 登录相关的返回码。含义来自实测与 imoore 的记录。
SSO_CODES = {
    "000001": "成功",
    "000002": "服务端内部异常",
    "010106": "验证码服务不可用（缺 reCAPTCHA token）",
    "010307": "设备 ID 为空",
    "010402": "账号名或密码错误",
}


class SsoError(RuntimeError):
    """登录流程失败。`code` 是服务端返回码，`attempts_left` 仅 010402 时有值。"""

    def __init__(self, kind: str, message: str, *, code: str = "",
                 attempts_left: int | None = None) -> None:
        super().__init__(f"[{kind}] {message}" + (f" (code={code})" if code else ""))
        self.kind = kind
        self.code = code
        self.attempts_left = attempts_left


@dataclass
class LoginResult:
    """登录成功后拿到的东西。

    `ticket` 是核心 API 的会话令牌 —— 它就是**整个 `data` JSON 原文**，
    不是其中某个子字段。依据（脱壳后的真实代码）::

        com/xtc/account/service/impl/Gibraltar.java:193-194
            r = Gabon(loginResponse.data, SSOLoginResult.class);
            r.setTicket(loginResponse.data);
        com/xtc/account/service/Gibraltar.java:24
            m.setToken(r.getTicket());

    `token` 保留为 `sign` 的别名，仅为兼容旧调用；真正的 `token` 请用 `ticket`。
    `raw` 保留原始响应，便于排查字段变化。
    """

    ticket: str = ""          # 核心 API 的 token（= data 原文）
    account_id: str = ""      # 登录态里的 accountId，同时也是 authId
    sign: str = ""            # SSO 签名，不是核心 API 的 token
    device_id: str = ""       # 登录会话的 deviceId（服务端原样回显客户端上报值）
    mobile_id: str = ""       # mobileId，BRP 的 accountId 要用它
    raw: dict = field(default_factory=dict)

    @property
    def token(self) -> str:
        """核心 API 的 token（等同于 `ticket`）。"""
        return self.ticket

    def __bool__(self) -> bool:
        return bool(self.ticket)

    def __repr__(self) -> str:
        # 不要把 token 打进日志
        return (f"LoginResult(ticket={'***' if self.ticket else ''!r}, "
                f"account_id={self.account_id!r})")


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def sign_params(url: str, params: dict, secret: str) -> str:
    """算出登录请求的 `sign`。

    顺序：url + (按 key 大小写不敏感排序的 k=v，用 ## 连接) + secret，
    取 MD5 小写。url 与第一个参数之间不加分隔符 —— 这一点很容易写错。
    """
    kv = sorted((f"{k}={v}" for k, v in params.items()), key=str.lower)
    return _md5_hex(url + "##".join(kv) + secret)


def _aes_ecb(key: bytes, data: bytes, *, encrypt: bool) -> bytes:
    """AES-128-ECB + PKCS5。优先用 cryptography，没有则退回纯实现。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:  # pragma: no cover - 环境相关
        from .crypto import _aes_ecb_pure
        return _aes_ecb_pure(data, key, encrypt)
    if encrypt:
        pad = 16 - len(data) % 16
        data = data + bytes([pad]) * pad
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(data) + enc.finalize()
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    if out:
        n = out[-1]
        if 1 <= n <= 16 and out.endswith(bytes([n]) * n):
            out = out[:-n]
    return out


def _rsa_encrypt(data: bytes, der_b64: str) -> bytes:
    """用 `/app/rsakey` 返回的 SPKI 公钥做 PKCS1v1.5 加密。"""
    der = base64.b64decode(der_b64)
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise SsoError("crypto", "登录需要 cryptography 库（RSA 包裹 aesKey）") from exc
    pub = serialization.load_der_public_key(der)
    return pub.encrypt(data, padding.PKCS1v15())


@dataclass
class SsoClient:
    """登录客户端。只做 `/app/*` 那套信封，不涉及核心 API。

    典型用法：

        c = SsoClient()
        r = c.password_login("13800138000", "密码")
        if r:
            print(r.account_id)      # 之后作为 authId
    """

    base: str = DEFAULT_SSO_BASE
    app_id: str = DEFAULT_APP_ID
    mac: str = DEFAULT_MAC
    timeout: float = 15.0
    logger: object | None = None
    _pubkey: str = ""

    def __post_init__(self) -> None:
        if not (1.0 <= float(self.timeout) <= 300.0):
            self.timeout = 15.0
        if self.app_id not in APP_SECRETS:
            raise SsoError("config", f"未知 appId={self.app_id!r}，可用：{sorted(APP_SECRETS)}")

    # ---------------------------------------------------------------- 底层
    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            try:
                getattr(self.logger, level)(msg)
            except Exception:  # noqa: BLE001 日志失败不能影响主流程
                pass

    @property
    def secret(self) -> str:
        return APP_SECRETS[self.app_id]

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _post(self, path: str, params: dict, *, encrypt: bool,
              aes_key: bytes = b"") -> tuple[dict, bytes]:
        """发一个 `/app/*` 请求，返回 (解析后的 JSON, 本次用的 aes_key)。"""
        url = self.base.rstrip("/") + path
        signed = dict(params)
        signed["sign"] = sign_params(url, signed, self.secret)

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Charset": "utf-8",
            "App-Brand": "xtc",
            "User-Agent": "okhttp/3.12.0",
        }
        if encrypt:
            key = aes_key or os.urandom(8).hex().encode("ascii")[:16]
            plain = json.dumps(signed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            body = base64.b64encode(_aes_ecb(key, plain, encrypt=True))
            headers["EncryptType"] = "1"
            headers["EncryptKey"] = base64.b64encode(
                _rsa_encrypt(key, self._get_pubkey())).decode("ascii")
        else:
            key = b""
            body = urllib.parse.urlencode(signed).encode("utf-8")

        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as exc:
            raise SsoError("http", f"{path} 返回 HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 网络层各种异常统一归为 network
            raise SsoError("network", f"{path} 请求失败：{exc}") from exc

        return self._decode(raw, key), key

    def _decode(self, raw: str, aes_key: bytes) -> dict:
        """响应可能是明文 JSON，也可能是带引号的 base64 密文。"""
        text = raw
        if text.startswith('"') and text.endswith('"'):
            try:
                text = json.loads(text)
            except Exception:  # noqa: BLE001 不是 JSON 字符串就按原样处理
                pass
        if aes_key:
            try:
                plain = _aes_ecb(aes_key, base64.b64decode(text), encrypt=False)
                return json.loads(plain.decode("utf-8"))
            except Exception:  # noqa: BLE001 解不开就当明文
                pass
        try:
            return json.loads(text)
        except Exception as exc:
            raise SsoError("parse", f"响应无法解析：{raw[:120]!r}") from exc

    def _get_pubkey(self) -> str:
        """取并缓存 `/app/rsakey` 的 SPKI 公钥（base64）。"""
        if self._pubkey:
            return self._pubkey
        data, _ = self._post("/app/rsakey", {
            "appId": self.app_id, "mac": self.mac, "timestamp": self._now(),
        }, encrypt=False)
        if str(data.get("code")) != "000001" or not data.get("data"):
            raise SsoError("pubkey", f"取公钥失败：{data.get('desc')}", code=str(data.get("code")))
        self._pubkey = str(data["data"])
        return self._pubkey

    # ---------------------------------------------------------------- 对外
    def password_login(self, phone: str, password: str, *,
                       area_code: str = "86") -> LoginResult:
        """账号密码登录。成功返回 LoginResult，失败抛 SsoError。

        注意 `days` 必须是字符串 "-1"；传整数会让服务端 500。
        """
        if not phone or not password:
            raise SsoError("config", "手机号与密码都不能为空")
        payload = {
            "userName": phone,
            "areaCode": area_code,
            "password": password,
            "appId": self.app_id,
            "mac": self.mac,
            "timestamp": self._now(),
            "days": "-1",
            # 服务端不校验 deviceId 是否真机，但必须存在
            "deviceId": os.urandom(8).hex(),
        }
        self._log("info", f"登录请求：{phone[:3]}****{phone[-2:]}")
        data, _ = self._post("/app/loginV2", payload, encrypt=True)
        return self._result(data, "登录")

    def token_login(self, account_id: str, token: str) -> LoginResult:
        """用已有 token 续期，避免重复登录。"""
        if not token:
            raise SsoError("config", "token 不能为空")
        payload = {
            "userId": account_id,
            "token": token,
            "appId": self.app_id,
            "mac": self.mac,
            "timestamp": self._now(),
            "deviceId": os.urandom(8).hex(),
        }
        data, _ = self._post("/app/tokenLogin", payload, encrypt=True)
        return self._result(data, "token 续期")

    def _result(self, data: dict, what: str) -> LoginResult:
        code = str(data.get("code", ""))
        if code != "000001":
            desc = data.get("desc") or SSO_CODES.get(code, "未知错误")
            left = None
            if code == "010402":
                try:
                    left = int(data.get("data"))
                except (TypeError, ValueError):
                    left = None
                if left is not None:
                    desc = f"{desc}（剩余尝试 {left} 次）"
            raise SsoError("business", f"{what}失败：{desc}", code=code, attempts_left=left)

        raw = data.get("data")
        # data 是一个 JSON 字符串，里面是账号信息；这个字符串本身就是 ticket
        inner: dict = {}
        if isinstance(raw, str):
            ticket = raw          # ← token 就是 data 原文，不做任何拆分
            try:
                inner = json.loads(raw)
            except Exception:  # noqa: BLE001 不是 JSON 就当纯字符串
                inner = {"token": raw}
                ticket = raw
        elif isinstance(raw, dict):
            inner = raw
            ticket = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        else:
            inner, ticket = {}, ""

        account_id = str(inner.get("accountId") or inner.get("userId") or "")
        return LoginResult(
            ticket=ticket,
            account_id=account_id,
            sign=str(inner.get("sign") or ""),
            device_id=str(inner.get("deviceId") or ""),
            mobile_id=str(inner.get("mobileId") or ""),
            raw=inner or data)
