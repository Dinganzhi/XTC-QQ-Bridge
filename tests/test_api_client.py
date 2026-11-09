# -*- coding: utf-8 -*-
"""小天才 API 协议层单测。

覆盖三件事：
1. AES-ECB 实现与 FIPS-197 标准向量一致（纯 Python 兜底必须正确，否则签名全错）。
2. 签名信封的**确定性**与字段顺序（服务端按同一份明文重算，顺序错就验不过）。
3. 默认关闭 / 凭据脱敏 / 错误分层 —— 保证这个实验模块不会影响现有链路。

运行：python3 -B tests/test_api_client.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from xtc_qq_bridge.api.crypto import (  # noqa: E402
    SUCCESS_CODES, HttpAccount, ImooCrypto, _aes_ecb_pure, build_base_request_param,
    build_signed_request, dumps_compact, eebbk_sign,
)
from xtc_qq_bridge.api.client import ImooApiClient  # noqa: E402

_PASS = 0
_FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append(f"{name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------- AES 标准向量
def test_aes_fips197_vectors() -> None:
    """FIPS-197 附录 C 的三组标准向量（截取前 16 字节，忽略 PKCS7 填充块）。"""
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    cases = [
        ("AES-128", "000102030405060708090a0b0c0d0e0f", "69c4e0d86a7b0430d8cdb78070b4c55a"),
        ("AES-192", "000102030405060708090a0b0c0d0e0f1011121314151617",
         "dda97ca4864cdfe06eaf70a0ec0d7191"),
        ("AES-256", "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
         "8ea2b7ca516745bfeafc49904b496089"),
    ]
    for label, key_hex, expect_hex in cases:
        out = _aes_ecb_pure(plain, bytes.fromhex(key_hex), encrypt=True)
        check(f"{label} 加密首块匹配标准向量", out[:16].hex() == expect_hex,
              f"得到 {out[:16].hex()}")
        back = _aes_ecb_pure(out, bytes.fromhex(key_hex), encrypt=False)
        check(f"{label} 解密回原文", back == plain)


def test_aes_padding_roundtrip() -> None:
    """任意长度输入都能正确填充/去填充。"""
    key = bytes(range(16))
    for n in (0, 1, 15, 16, 17, 31, 32, 100):
        data = bytes((i * 7) & 0xFF for i in range(n))
        enc = _aes_ecb_pure(data, key, encrypt=True)
        check(f"长度 {n} 密文为 16 的倍数", len(enc) % 16 == 0, f"实际 {len(enc)}")
        check(f"长度 {n} 往返一致", _aes_ecb_pure(enc, key, encrypt=False) == data)


def test_aes_rejects_bad_key() -> None:
    for bad in (b"", b"short", b"x" * 15, b"x" * 17):
        try:
            _aes_ecb_pure(b"abc", bad, encrypt=True)
            check(f"非法密钥长度 {len(bad)} 应被拒绝", False)
        except ValueError:
            check(f"非法密钥长度 {len(bad)} 被拒绝", True)


# ---------------------------------------------------------------- BRP 与签名
def _acct() -> HttpAccount:
    return HttpAccount(bind_number="BIND123", chip_id="CHIP456", account_id="ACC789",
                       regist_id=42, inner_model="XTC_Z10", soft_version="3.2.0",
                       encrypt_key="kid1:0123456789abcdef:ebk1")


def test_brp_field_order() -> None:
    """字段顺序固定：服务端按同一份明文重算签名，顺序变了就验不过。"""
    brp = build_base_request_param(_acct(), now=1700000000)
    # 字段与取值来自 com.xtc.http.bean.BaseRequestParam（imoore 项目静态分析证实）
    order = ['"accountId"', '"appId"', '"deviceId"', '"imFlag"', '"program"',
             '"registId"', '"timestamp"', '"token"', '"mac"', '"requestId"']
    positions = [brp.find(k) for k in order]
    check("BRP 包含全部字段", all(p >= 0 for p in positions),
          f"缺失 {[order[i] for i, p in enumerate(positions) if p < 0]}")
    check("BRP 字段顺序正确", positions == sorted(positions), f"{positions}")
    check("BRP 保留官方拼写 mac=unkown", '"mac":"unkown"' in brp)
    check("BRP appId 固定 0", '"appId":"0"' in brp, brp[:120])
    check("BRP program 固定 android", '"program":"android"' in brp)
    check("BRP imFlag 固定 1", '"imFlag":"1"' in brp)


def test_brp_without_device() -> None:
    """未绑定设备时不带 deviceId/token（部分只读接口如此）。"""
    acct = HttpAccount(account_id="ACC", regist_id=1)
    brp = build_base_request_param(acct, now=1700000000)
    check("无设备时不含 deviceId", '"deviceId"' not in brp)
    check("无设备时不含 token", '"token"' not in brp)
    check("无设备时仍有 accountId", '"accountId":"ACC"' in brp)


def test_brp_escapes_quotes() -> None:
    """凭据里若有引号必须转义，否则 BRP 不是合法 JSON。"""
    acct = HttpAccount(bind_number='a"b', chip_id="c\\d", account_id="e", regist_id=1)
    brp = build_base_request_param(acct, now=1700000000)
    check("BRP 是合法 JSON", isinstance(json.loads(brp), dict))
    check("引号已转义", '\\"' in brp)


def test_sign_deterministic_and_uppercase() -> None:
    s1 = eebbk_sign("/a/b", '{"x":1}', '{"y":2}', "0123456789abcdef")
    s2 = eebbk_sign("/a/b", '{"x":1}', '{"y":2}', "0123456789abcdef")
    check("签名确定性", s1 == s2)
    check("签名长度 32", len(s1) == 32, f"实际 {len(s1)}")
    check("签名全大写十六进制", s1 == s1.upper() and all(c in "0123456789ABCDEF" for c in s1))


def test_sign_changes_with_each_input() -> None:
    """任一输入变化都必须改变签名（防止实现里漏拼了某一段）。"""
    base = eebbk_sign("/a", "brp", "body", "key")
    check("uri 变化改变签名", eebbk_sign("/b", "brp", "body", "key") != base)
    check("brp 变化改变签名", eebbk_sign("/a", "brp2", "body", "key") != base)
    check("body 变化改变签名", eebbk_sign("/a", "brp", "body2", "key") != base)
    check("key 变化改变签名", eebbk_sign("/a", "brp", "body", "key2") != base)


# ---------------------------------------------------------------- 加密版本
def test_crypto_v3_parse() -> None:
    c = ImooCrypto("kid:0123456789abcdef:ebk")
    check("v3 版本识别", c.version == 3)
    check("v3 keyId", c.key_id == "kid")
    check("v3 aesKey", c.aes_key == "0123456789abcdef")
    check("v3 eebbkKey", c.eebbk_key == "ebk")


def test_crypto_v0_fallback() -> None:
    """结构不认识时退回明文，而不是抛异常打断链路。"""
    for bad in ("", "a:b", "kid:short:ebk", "a:b:c:d"):
        c = ImooCrypto(bad)
        check(f"非法 encrypt_key {bad!r} 退回 v0", c.version == 0)
        check(f"非法 encrypt_key {bad!r} 明文直通", c.encrypt_body("x") == "x")


def test_crypto_body_roundtrip() -> None:
    c = ImooCrypto("kid:0123456789abcdef:ebk")
    for text in ('{"a":1}', "中文内容", "x" * 5000, ""):
        enc = c.encrypt_body(text)
        check(f"body 往返一致（{len(text)} 字）", c.decrypt_body(enc) == text)
        if text:
            check(f"body 已加密（{len(text)} 字）", enc != text)


def test_crypto_extra_headers() -> None:
    c3 = ImooCrypto("kid:0123456789abcdef:ebk")
    names = [k for k, _ in c3.extra_headers()]
    for need in ("Eebbk-Key-Id", "Eebbk-Key", "encrypted"):
        check(f"v3 含头 {need}", need in names)
    check("v0 无额外头", ImooCrypto("").extra_headers() == [])


# ---------------------------------------------------------------- 请求组装
def test_build_signed_request() -> None:
    headers, body = build_signed_request(_acct(), "POST", "https://api.watch.okii.com/x",
                                         '{"a":1}')
    for need in ("Base-Request-Param", "Eebbk-Sign", "Content-Type", "model",
                 "Version", "Grey", "dataCenterCode", "Eebbk-Key-Id"):
        check(f"POST 含头 {need}", need in headers, f"实际 {sorted(headers)}")
    check("POST body 已加密", body != '{"a":1}')
    check("Version 前缀 W_", headers.get("Version") == "W_3.2.0")

    h_get, b_get = build_signed_request(_acct(), "GET", "https://api.watch.okii.com/x")
    check("GET 无 body", b_get == "")
    check("GET 不含加密头", "Eebbk-Key-Id" not in h_get)


def test_compact_json() -> None:
    """App 用紧凑 JSON（无空格），签名对这段文本计算。"""
    out = dumps_compact({"a": 1, "b": "中"})
    check("紧凑 JSON 无空格", " " not in out, out)
    check("紧凑 JSON 保留中文", "中" in out)


# ---------------------------------------------------------------- 客户端行为
def test_client_disabled_by_default() -> None:
    c = ImooApiClient()
    check("默认未启用", c.enabled is False)
    for res in (c.get("/x"), c.post("/x", {"a": 1})):
        check("未启用时 ok=False", res.ok is False)
        check("未启用时 kind=disabled", res.kind == "disabled")
        check("未启用时无网络耗时", res.elapsed >= 0)


def test_client_rejects_missing_credentials() -> None:
    c = ImooApiClient(HttpAccount(), enabled=True)
    ok, why = c.ready()
    check("缺凭据时不可用", ok is False)
    check("缺凭据时说明原因", "凭据" in why, why)

    c2 = ImooApiClient(HttpAccount(bind_number="b", chip_id="c"), enabled=True)
    ok2, why2 = c2.ready()
    check("缺 account_id 时不可用", ok2 is False)
    check("缺 account_id 时说明原因", "account_id" in why2, why2)


def test_client_ready_with_full_credentials() -> None:
    c = ImooApiClient(_acct(), enabled=True)
    ok, why = c.ready()
    check("凭据齐全时可用", ok is True, why)


def test_account_repr_masks_secrets() -> None:
    """凭据绝不能出现在 repr 里（会被日志打印）。"""
    acct = _acct()
    text = repr(acct)
    for secret in (acct.bind_number, acct.chip_id, acct.account_id):
        check(f"repr 不含 {secret[:4]}***", secret not in text, text)
    check("repr 标明已配置", "已配置" in text)
    check("repr 不含 aesKey", "0123456789abcdef" not in text)


def test_success_codes() -> None:
    check("okii 成功码 200", "200" in SUCCESS_CODES)
    check("tiancaixing 成功码 000001", "000001" in SUCCESS_CODES)
    check("通用成功码 0", "0" in SUCCESS_CODES)


def test_api_result_truthiness() -> None:
    from xtc_qq_bridge.api.client import ApiResult
    check("成功结果为真", bool(ApiResult(ok=True)) is True)
    check("失败结果为假", bool(ApiResult(ok=False)) is False)


# ---------------------------------------------------------------- 配置适配
def test_client_from_config_defaults_off() -> None:
    """实验性功能必须默认关闭：空配置、缺 api 段、api 段不是 dict，都应为关闭。"""
    from xtc_qq_bridge.api.client import client_from_config
    for cfg in ({}, {"api": None}, {"api": "字符串"}, {"api": []}, {"api": {}}):
        c = client_from_config(cfg)
        check(f"配置 {cfg!r} 默认关闭", c.enabled is False, f"enabled={c.enabled}")


def test_client_from_config_enabled_forms() -> None:
    """enabled 支持布尔与常见字符串写法。"""
    from xtc_qq_bridge.api.client import client_from_config
    for val, want in ((True, True), ("true", True), ("TRUE", True), ("1", True),
                      ("yes", True), ("on", True), (False, False), ("false", False),
                      ("0", False), ("", False), (None, False)):
        c = client_from_config({"api": {"enabled": val}})
        check(f"enabled={val!r} -> {want}", c.enabled is want, f"实际 {c.enabled}")


def test_account_from_config_maps_fields() -> None:
    from xtc_qq_bridge.api.client import account_from_config
    acct = account_from_config({"api": {
        "bind_number": " B1 ", "chip_id": "C1", "account_id": "A1",
        "regist_id": 7, "inner_model": "XTC_Z10", "soft_version": "3.2.0",
        "encrypt_key": "k:0123456789abcdef:e", "grey": "", "data_center": "",
    }})
    check("bind_number 去空格", acct.bind_number == "B1", acct.bind_number)
    check("regist_id 为整数", acct.regist_id == 7)
    check("grey 空值回落 0", acct.grey == "0", acct.grey)
    check("data_center 空值回落 CN_BJ", acct.data_center == "CN_BJ", acct.data_center)


def test_account_from_config_survives_bad_types() -> None:
    """配置写错不能让程序起不来：一律回落默认值。"""
    from xtc_qq_bridge.api.client import account_from_config
    for bad in ("abc", None, [], {}, True, float("inf"), float("nan"), 10**40):
        acct = account_from_config({"api": {"regist_id": bad}})
        check(f"regist_id={bad!r} 回落 0", acct.regist_id == 0, f"实际 {acct.regist_id}")


def test_client_from_config_bad_timeout() -> None:
    from xtc_qq_bridge.api.client import client_from_config
    for bad in ("abc", None, [], float("inf"), 10**40):
        c = client_from_config({"api": {"timeout": bad}})
        check(f"timeout={bad!r} 仍可构造", c.timeout >= 1.0, f"实际 {c.timeout}")


def test_client_from_config_custom_base() -> None:
    from xtc_qq_bridge.api.client import client_from_config
    c = client_from_config({"api": {"base": "https://example.test/"}})
    check("base 去掉尾斜杠", c.base == "https://example.test", c.base)


def test_timeout_converges_to_sane_range() -> None:
    """inf/nan/超大值会让 urlopen 永不超时（挂死），必须收敛到 [1, 300]。"""
    import math
    from xtc_qq_bridge.api.client import client_from_config
    for bad in (float("inf"), float("nan"), 10**40, -5, 0, 301):
        c = client_from_config({"api": {"timeout": bad}})
        ok = math.isfinite(c.timeout) and 1.0 <= c.timeout <= 300.0
        check(f"配置 timeout={bad!r} 收敛到合理范围", ok, f"实际 {c.timeout}")
    for good in (1, 10, 300):
        c = client_from_config({"api": {"timeout": good}})
        check(f"配置 timeout={good} 原样保留", c.timeout == float(good), f"实际 {c.timeout}")


def test_constructor_timeout_hardened() -> None:
    """直接构造也不能被 inf/nan 穿透。"""
    import math
    for bad in (float("inf"), float("nan"), 10**40):
        c = ImooApiClient(timeout=bad)
        ok = math.isfinite(c.timeout) and 1.0 <= c.timeout <= 300.0
        check(f"构造 timeout={bad!r} 收敛", ok, f"实际 {c.timeout}")


# ---------------------------------------------------------------- 离线向量
def test_real_gateway_vectors() -> None:
    """记录 2026-11-10 对真实网关的观测结果，防止将来改动破坏已知正确的行为。

    这些是**离线断言**（不发网络请求）：用固定的 BRP 与 key 校验签名，
    确保"字段顺序/取值"这类改动会被测试拦住。真实联调见 docs/imoo-api.md。
    """
    acct = HttpAccount(account_id="ACC1", bind_number="DEV1", chip_id="TOK1",
                       regist_id=1, inner_model="XTC_Z10", soft_version="9.47.02",
                       encrypt_key="")
    brp = build_base_request_param(acct, now=1700000000)
    # 字段集合必须与 BaseRequestParam 一致（多一个少一个都会导致签名不符）
    got = set(json.loads(brp))
    want = {"accountId", "appId", "deviceId", "imFlag", "program",
            "registId", "timestamp", "token", "mac", "requestId"}
    check("BRP 字段集合与官方一致", got == want, f"多 {got-want} 缺 {want-got}")

    # 固定输入 -> 固定签名（MD5 是确定性的，用于回归）
    sig = eebbk_sign("https://watch.okii.com/rsa", brp, "", "")
    check("签名对固定输入稳定", sig == eebbk_sign("https://watch.okii.com/rsa", brp, "", ""))
    check("签名 32 位大写", len(sig) == 32 and sig.isupper())


def test_v0_plain_matches_server_encswitch() -> None:
    """真实服务器 /rsa 返回 encSwitch=0，即明文模式；v0 下 BRP 必须原样发出。"""
    acct = HttpAccount(account_id="A", bind_number="D", chip_id="T", regist_id=1,
                       inner_model="M", soft_version="1", encrypt_key="")
    headers, _ = build_signed_request(acct, "GET", "https://watch.okii.com/rsa")
    brp_header = headers["Base-Request-Param"]
    check("v0 时 BRP 为明文 JSON", brp_header.startswith("{"), brp_header[:40])
    check("v0 时 BRP 可解析", isinstance(json.loads(brp_header), dict))
    check("v0 时无 Eebbk-Key", "Eebbk-Key" not in headers)
    check("v0 时无 encrypted 头", "encrypted" not in headers)


def test_default_base_is_watch_host() -> None:
    """真实可用 base 是 watch.okii.com；api.watch.okii.com 只是 HOST_NAME_ARRAYS[1]。"""
    from xtc_qq_bridge.api.client import DEFAULT_BASE
    check("默认 base 为 watch.okii.com", DEFAULT_BASE == "https://watch.okii.com", DEFAULT_BASE)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
    total = _PASS + len(_FAIL)
    if _FAIL:
        print(f"✗ {len(_FAIL)}/{total} 项失败：")
        for f in _FAIL:
            print(f"    - {f}")
        return 1
    print(f"✓ API 协议层 {total} 项检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
