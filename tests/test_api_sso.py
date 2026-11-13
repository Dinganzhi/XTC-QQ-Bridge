# -*- coding: utf-8 -*-
"""`xtc_qq_bridge.api.sso` 的离线测试。

原则：**不打真实网络**。真实服务器的行为在 docs/imoo-api.md 里记录，
这里只锁住可以离线验证的契约（签名算法、编码细节、错误处理）。

跑法：
    python3 -B tests/test_api_sso.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xtc_qq_bridge.api.sso import (  # noqa: E402
    APP_SECRETS,
    DEFAULT_MAC,
    LoginResult,
    SsoClient,
    SsoError,
    sign_params,
)

_checks = 0
_fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if not ok:
        _fails.append(f"{label}" + (f" — {detail}" if detail else ""))


# ------------------------------------------------------------------ 签名
def test_sign_vector() -> None:
    """真实服务器验证过的向量：这个值错了，登录一定失败。"""
    url = "https://account.okii.com/app/rsakey"
    params = {
        "appId": "11",
        "mac": DEFAULT_MAC,
        "timestamp": "2026-11-10 18:40:00",
    }
    got = sign_params(url, params, "LKyF8jThHbZfUeTP")
    check("签名与真实服务器验证过的向量一致",
          got == "2b0b412fa31ba9225b5a0d0d4ff42103", got)


def test_sign_key_order_case_insensitive() -> None:
    """key 排序必须大小写不敏感，否则大小写混排时会算错。"""
    url = "https://x/y"
    a = sign_params(url, {"B": "2", "a": "1", "C": "3"}, "s")
    # 大小写不敏感排序：a, B, C
    manual = hashlib.md5((url + "a=1##B=2##C=3" + "s").encode()).hexdigest()
    check("大小写不敏感排序", a == manual, f"{a} != {manual}")


def test_sign_no_separator_after_url() -> None:
    """url 与第一个 k=v 之间不能有分隔符 —— 这是最容易写错的地方。"""
    url = "https://x/y"
    got = sign_params(url, {"a": "1"}, "s")
    with_sep = hashlib.md5((url + "##a=1" + "s").encode()).hexdigest()
    check("url 后不加分隔符", got != with_sep, "多加了分隔符")
    check("url 后直接接 k=v",
          got == hashlib.md5((url + "a=1" + "s").encode()).hexdigest())


def test_sign_is_lowercase() -> None:
    """登录这套的 sign 是小写；核心 API 的 Eebbk-Sign 才是大写。"""
    got = sign_params("https://x/y", {"a": "1"}, "s")
    check("sign 为小写", got == got.lower(), got)
    check("sign 是 32 位十六进制", len(got) == 32 and all(c in "0123456789abcdef" for c in got))


# ------------------------------------------------------------------ 配置
def test_secret_table() -> None:
    check("appId 11 有密钥", APP_SECRETS.get("11") == "LKyF8jThHbZfUeTP")
    check("密钥都是 16 字符", all(len(v) == 16 for v in APP_SECRETS.values()),
          str({k: len(v) for k, v in APP_SECRETS.items()}))


def test_unknown_app_id_rejected() -> None:
    try:
        SsoClient(app_id="999")
    except SsoError as exc:
        check("未知 appId 被拒绝", exc.kind == "config", str(exc))
    else:
        check("未知 appId 被拒绝", False, "没有抛异常")


def test_timeout_clamped() -> None:
    for bad in (0, -1, float("inf"), float("nan"), 10 ** 9):
        c = SsoClient(timeout=bad)
        check(f"timeout={bad} 被收敛到合法区间", 1.0 <= c.timeout <= 300.0, str(c.timeout))


# ------------------------------------------------------------------ 编码
def test_aes_ecb_roundtrip() -> None:
    from xtc_qq_bridge.api.sso import _aes_ecb
    key = b"0123456789abcdef"
    for text in (b"", b"x", b"1234567890123456", b"a" * 100):
        enc = _aes_ecb(key, text, encrypt=True)
        dec = _aes_ecb(key, enc, encrypt=False)
        check(f"AES 往返 {len(text)}B", dec == text, f"{dec[:20]!r}")


def test_aes_ecb_matches_fips_vector() -> None:
    """FIPS-197 附录 C.1 的 AES-128 向量，确保不是自说自话。"""
    from xtc_qq_bridge.api.sso import _aes_ecb
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    # 用 ECB 单块加密（PKCS5 会补一块，取前 16 字节比较）
    enc = _aes_ecb(key, plain, encrypt=True)
    check("AES-128 首块符合 FIPS-197",
          enc[:16] == bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"),
          enc[:16].hex())


def test_days_must_be_string() -> None:
    """`days` 传整数会让服务端 500 —— 这条约束写进代码，防止以后"优化"成 int。"""
    import inspect
    src = inspect.getsource(SsoClient.password_login)
    check("password_login 里 days 是字符串 '-1'", '"days": "-1"' in src, "找不到")


def test_device_id_present() -> None:
    """deviceId 服务端不校验真伪，但必须存在（缺了报 010307）。"""
    import inspect
    src = inspect.getsource(SsoClient.password_login)
    check("password_login 里带了 deviceId", '"deviceId"' in src)


# ------------------------------------------------------------------ 结果
def test_login_result_masks_token() -> None:
    r = LoginResult(ticket="secret-token-value", account_id="123")
    check("LoginResult 真值为 True", bool(r))
    check("repr 不泄露 token", "secret-token-value" not in repr(r), repr(r))
    check("repr 保留 account_id", "123" in repr(r), repr(r))
    # token 是 ticket 的别名，二者必须一致
    check("token 等同 ticket", r.token == "secret-token-value", r.token)


def test_empty_login_result_is_falsy() -> None:
    check("空 LoginResult 为 False", not LoginResult())


def test_decode_plain_json() -> None:
    c = SsoClient()
    got = c._decode('{"code":"000001","data":"x"}', b"")
    check("明文 JSON 能解析", got.get("code") == "000001", str(got))


def test_decode_quoted_json_string() -> None:
    """响应有时是 JSON 字符串字面量（外层带引号），要能拆开。"""
    c = SsoClient()
    inner = json.dumps({"code": "000001", "data": "y"})
    got = c._decode(json.dumps(inner), b"")
    check("带引号的 JSON 能解析", got.get("code") == "000001", str(got))


def test_decode_encrypted() -> None:
    """服务端加密响应：base64(AES(json))，用同一个 aesKey 解。"""
    from xtc_qq_bridge.api.sso import _aes_ecb
    c = SsoClient()
    key = b"0123456789abcdef"
    payload = json.dumps({"code": "000001", "data": "z"}).encode()
    cipher = base64.b64encode(_aes_ecb(key, payload, encrypt=True)).decode()
    got = c._decode(cipher, key)
    check("加密响应能解密", got.get("code") == "000001", str(got))


def test_decode_garbage_raises() -> None:
    c = SsoClient()
    try:
        c._decode("not-json-at-all", b"")
    except SsoError as exc:
        check("无法解析时抛 SsoError", exc.kind == "parse", str(exc))
    else:
        check("无法解析时抛 SsoError", False, "没有抛异常")


# ------------------------------------------------------------------ 错误映射
def test_result_maps_010402() -> None:
    c = SsoClient()
    try:
        c._result({"code": "010402", "desc": "账号名或密码错误", "data": "5"}, "登录")
    except SsoError as exc:
        check("010402 映射为 business", exc.kind == "business")
        check("010402 解析剩余次数", exc.attempts_left == 5, str(exc.attempts_left))
        check("消息含剩余次数", "5" in str(exc), str(exc))
    else:
        check("010402 应抛异常", False)


def test_result_maps_010307() -> None:
    c = SsoClient()
    try:
        c._result({"code": "010307", "desc": "设备ID为空"}, "登录")
    except SsoError as exc:
        check("010307 有中文说明", "设备" in str(exc), str(exc))
    else:
        check("010307 应抛异常", False)


def test_result_accepts_000001() -> None:
    c = SsoClient()
    data_str = json.dumps({"token": "T", "accountId": "A"})
    r = c._result({"code": "000001", "data": data_str}, "登录")
    check("000001 返回 LoginResult", bool(r))
    # token 是 data 的原始串，不是里面的 "token" 子字段
    check("token 等于 data 原文", r.token == data_str, r.token)
    check("accountId 提取正确", r.account_id == "A", r.account_id)


def test_result_accepts_plain_token_string() -> None:
    """`data` 也可能就是裸 token 字符串，不是 JSON。"""
    c = SsoClient()
    r = c._result({"code": "000001", "data": "raw-token-abc"}, "登录")
    check("裸 token 字符串能识别", r.token == "raw-token-abc", r.token)


def test_result_empty_credentials_rejected() -> None:
    c = SsoClient()
    for phone, pwd in (("", "x"), ("1", ""), ("", "")):
        try:
            c.password_login(phone, pwd)
        except SsoError as exc:
            check(f"空凭据 ({phone!r},{pwd!r}) 被拒绝", exc.kind == "config")
        else:
            check(f"空凭据 ({phone!r},{pwd!r}) 被拒绝", False)


def test_token_login_requires_token() -> None:
    c = SsoClient()
    try:
        c.token_login("123", "")
    except SsoError as exc:
        check("空 token 被拒绝", exc.kind == "config")
    else:
        check("空 token 被拒绝", False)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 测试框架自身的容错
                _fails.append(f"{name} 抛异常：{type(exc).__name__}: {exc}")

    if _fails:
        print(f"✗ SSO 测试 {len(_fails)}/{_checks} 项失败：")
        for f in _fails:
            print(f"    - {f}")
        return 1
    print(f"✓ SSO 登录 {_checks} 项检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
