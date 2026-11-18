# -*- coding: utf-8 -*-
"""IM 通道账号一键工具：签票 / 注册 / 登录 / 自检 / 写回凭据。

用法：

    # 看当前账号状态（不联网）
    python3 tools/im_account.py status

    # 纯代码把票据续上并自检（不需要小天才 App）
    python3 tools/im_account.py login

    # 一键配置：签票 -> 注册 -> 登录 -> 同步校验 -> 写回凭据
    python3 tools/im_account.py provision

    # 首次接入新账号时补参数（其余沿用现有凭据）
    python3 tools/im_account.py provision --phone 1xx --password 'xxx' \\
        --device-id <SSO设备身份> --watch-id <手表ID> --sender-id <手机侧imAccountId>

需要哪些参数、哪些能复用，见 docs/imoo-api.md 的"参数分类"一节：

    App 常量     im_app_key / sdk_version / platform / program / app_id / im_flag
                 所有账号通用，不用改。
    设备级       im_device_id_tlv / im_device_id_brp / im_mac / im_machine_id
                 描述跑 App 的那台机器，换账号可沿用。
    账号级       mobile_id（=账号的手表 ID）、im_sender_account_id、
                 im_regist_id、im_account_token、im_sso_device_id
                 每个账号独立，其中 **im_sso_device_id 只能从客户端取一次**。

为什么 im_sso_device_id 必须从客户端取：它必须等于该账号绑定过的设备身份。
实测用随机 32 位十六进制签票，SSO 会照常发票，但 IM 注册返回 102400；
换成客户端用过的那一个值，注册立刻 200。取到之后就能一直自签票，不再需要 App。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from xtc_qq_bridge.api.sso import SsoClient, SsoError          # noqa: E402
from xtc_qq_bridge.im.session import ImSession, _load_cfg      # noqa: E402

CRED = pathlib.Path.home() / ".config" / "xtc-bridge" / "imoo-credentials.json"

# 这些字段必须存在，缺任何一个都无法完成登录/收发
REQUIRED = (
    "im_app_key", "im_sdk_version", "im_sdk_version_name", "im_platform",
    "im_program", "im_app_id", "im_im_flag", "im_device_id_tlv", "im_device_id_brp",
    "im_mac", "im_machine_id", "mobile_id", "im_sender_account_id",
    "im_regist_id", "im_account_token", "im_sso_device_id", "phone", "password",
)

# 换账号时可沿用、不需要重填的字段
SHARED = ("im_app_key", "im_sdk_version", "im_sdk_version_name", "im_platform",
          "im_program", "im_app_id", "im_im_flag", "im_device_id_tlv",
          "im_device_id_brp", "im_mac", "im_machine_id")


def _load(path: pathlib.Path = CRED) -> dict:
    if not path.exists():
        raise SystemExit(f"找不到凭据文件：{path}\n请先用 provision 生成，或从备份恢复。")
    return json.loads(path.read_text("utf-8"))


def _save(cred: dict, path: pathlib.Path = CRED) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)          # 里面有账号密码，必须只本人可读
    except OSError:
        pass


def _mask(value: str, keep: int = 4) -> str:
    text = str(value or "")
    if len(text) <= keep:
        return "***"
    return text[:keep] + "***" + text[-2:]


def cmd_status(args: argparse.Namespace) -> int:
    cred = _load()
    print("凭据文件：", CRED)
    print(f"  修改时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(CRED.stat().st_mtime))}")
    print()
    print("账号级（每个账号独立）：")
    print(f"  phone                {_mask(cred.get('phone'))}")
    print(f"  mobile_id（手表）     {cred.get('mobile_id')}")
    print(f"  im_sender_account_id {cred.get('im_sender_account_id')}")
    print(f"  im_sso_device_id     {cred.get('im_sso_device_id')}")
    print(f"  im_regist_id         {cred.get('im_regist_id')}")
    token = str(cred.get("im_account_token") or "")
    issued = ""
    try:
        issued = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(json.loads(token)["timestamp"])))
    except Exception:
        pass
    print(f"  im_account_token     {'有' if token else '缺'}（签发于 {issued or '未知'}）")
    print()
    print("设备级 / App 常量（换账号可沿用）：")
    for key in SHARED:
        print(f"  {key:<22}{cred.get(key)}")
    missing = [k for k in REQUIRED if not cred.get(k)]
    print()
    if missing:
        print("缺失字段：", ", ".join(missing))
        return 1
    print("字段齐全 ✅")
    return 0


def _mint_ticket(cred: dict) -> str:
    """用 SSO 重新签一张 IM 票据。这是能长期脱离客户端的关键：票据会过期，但可以自签。

    `im_n_device_id` 首次生成后写回 cred 并复用：每次换新值等于伪装成另一台设备，
    可能被服务端判成「换设备登录」要求二次验证。
    """
    phone = str(cred.get("phone") or "")
    password = str(cred.get("password") or "")
    device = str(cred.get("im_sso_device_id") or "")
    if not phone or not password:
        raise SystemExit("凭据里缺 phone / password，无法续票")
    if not device:
        raise SystemExit("凭据里缺 im_sso_device_id，无法续票（需从客户端取一次）")
    n_dev = str(cred.get("im_n_device_id") or "")
    if not n_dev:
        n_dev = os.urandom(16).hex()
        cred["im_n_device_id"] = n_dev
    try:
        res = SsoClient().password_login(phone, password,
                                         device_id=device, n_device_id=n_dev)
    except SsoError as exc:
        raise SystemExit(f"SSO 签票失败：{exc}") from exc
    return res.ticket


def _verify(cred: dict, log=print) -> bool:
    """登录 + 同步自检。返回整体是否可用。"""
    session = ImSession(log=lambda lvl, msg: None)
    try:
        code = session.login_from_config(cred)
        log(f"  IM 登录          code={code}")
        if code != 200:
            return False
        sync_code, key = session.sync_once(cred)
        log(f"  同步自检         code={sync_code} syncKey={key}")
        # 只要有正常应答（200）就算通；没有待收消息时也可能只有 ack
        return sync_code == 200
    finally:
        session.close()


def cmd_login(args: argparse.Namespace) -> int:
    cred = _load()
    ticket = _mint_ticket(cred)
    cred["im_account_token"] = ticket
    print("已签出新票据，开始自检：")
    ok = _verify(cred)
    if ok:
        _save(cred)
        print("\n自检通过 ✅ 票据已写回凭据（全程未使用小天才 App）")
        return 0
    print("\n自检失败 ❌ 票据未写回")
    return 2


def cmd_provision(args: argparse.Namespace) -> int:
    cred = _load() if CRED.exists() else {}
    # 命令行给的值优先，其余沿用现有凭据
    for field, value in (("phone", args.phone), ("password", args.password),
                         ("im_sso_device_id", args.device_id),
                         ("mobile_id", args.watch_id),
                         ("im_sender_account_id", args.sender_id)):
        if value:
            cred[field] = int(value) if field == "im_sender_account_id" else value
    # 设备身份三者通常同源，缺哪个补哪个
    if args.device_id:
        for key in ("im_device_id_tlv", "im_device_id_brp"):
            if not args.keep_device_ids:
                cred[key] = args.device_id

    missing = [k for k in ("phone", "password", "im_sso_device_id", "mobile_id",
                           "im_sender_account_id") if not cred.get(k)]
    if missing:
        print("缺少账号级参数：", ", ".join(missing))
        print()
        print("这些只能从客户端取一次（之后永久不需要）：")
        print("  im_sso_device_id     App 登录时 loginV3 请求里的 deviceId")
        print("  mobile_id            账号绑定的手表 ID（App 数据库 MobileAccount.mobileId）")
        print("  im_sender_account_id 手机侧 imAccountId（同一张表 MobileAccount.imAccountInfo）")
        print()
        print("取法见 docs/imoo-api.md 的“从客户端取参数”一节；取到后用 --device-id/")
        print("--watch-id/--sender-id 传进来即可。")
        return 1

    print("1) 签票：", end="", flush=True)
    ticket = _mint_ticket(cred)
    cred["im_account_token"] = ticket
    print("成功")

    session = ImSession(log=lambda lvl, msg: None)
    try:
        print("2) 注册：", end="", flush=True)
        code, regist_id = session.register(cred)
        if code == 200 and regist_id:
            cred["im_regist_id"] = regist_id
            print(f"code={code} registId={regist_id}")
        else:
            print(f"code={code}（沿用原有 registId={cred.get('im_regist_id')}）")
    finally:
        session.close()

    print("3) 自检：")
    if not _verify(cred):
        print("\n配置失败 ❌")
        return 2

    _save(cred)
    print(f"\n配置完成 ✅ 已写回 {CRED}")
    print("  注册/登录/同步全部通过，之后不需要再启动小天才 App。")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """导出一份可分享的脱敏摘要，便于排查问题。

    `chip_id` 是一整块 SSO 票据（内含账号名、手机号、签名），必须整体隐去；
    `account_id` / `sign` / `device_id` 同理。
    """
    cred = _load()
    sensitive = ("password", "phone", "account_id", "sign", "device_id",
                 "chip_id", "im_sender_account_id", "mobile_id")
    safe = {}
    for key, value in cred.items():
        if key in sensitive:
            text = str(value or "")
            safe[key] = f"<{key} {len(text)} 字符，已隐去>"
        elif key == "im_account_token":
            safe[key] = f"<票据 {len(str(value))} 字符，已隐去>"
        elif key == "im_sso_device_id":
            safe[key] = _mask(value, keep=6)
        else:
            safe[key] = value
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IM 通道账号一键工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看当前账号状态（不联网）")

    sub.add_parser("login", help="纯代码签票并自检（不需要 App）")

    p = sub.add_parser("provision", help="一键配置：签票/注册/登录/自检/写回凭据")
    p.add_argument("--phone")
    p.add_argument("--password")
    p.add_argument("--device-id", help="SSO 设备身份（im_sso_device_id）")
    p.add_argument("--watch-id", help="账号绑定的手表 ID（mobile_id）")
    p.add_argument("--sender-id", help="手机侧 imAccountId")
    p.add_argument("--keep-device-ids", action="store_true",
                   help="不改动 im_device_id_tlv / im_device_id_brp")

    sub.add_parser("export", help="导出脱敏摘要")

    args = parser.parse_args()
    return {"status": cmd_status, "login": cmd_login,
            "provision": cmd_provision, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
