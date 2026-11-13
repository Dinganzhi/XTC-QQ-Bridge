#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小天才云端凭据采集工具（一次执行，结果可复用）。

用途
----
把"调通小天才云端 API"所需的四个值一次性采集齐，写进一个本地凭据文件，
后续所有工具都从这个文件读，不必每次重新折腾。

    python3 tools/imoo_credentials.py collect

设计原则
--------
1. **凭据只落本地**：默认写到 `~/.config/xtc-bridge/imoo-credentials.json`，
   权限 0600。这个路径不在仓库里，`git status` 永远看不到它。
2. **不打印明文**：终端只显示"有没有 / 多长 / 前 4 位"，完整值只在文件里。
3. **逐项独立**：某一项拿不到不影响其它项，缺失的项明确标出来源和原因。
4. **可重复执行**：已有凭据会复用，不会每次重登。

能自动拿到的
------------
- `account_id`：账号密码登录（`/app/loginV2`），纯 HTTP，不需要手机。
- `bind_number`：ADB 读 App 界面「更多 → 宝贝个人中心 → 手表绑定号」。
- `sign` / `device_id`：登录响应附带，调试用。

拿不到的（需要额外条件，工具会明确提示）
----------------------------------------
- `chip_id`（BRP 里的 `token`）：App 私有 SharedPreferences，无 root 读不到。
- `encrypt_key` 的三段真实值：服务端下发，不在 APK 明文里。

用法
----
    # 全自动采集（需要手机连着 ADB）
    python3 tools/imoo_credentials.py collect --phone 138xxxx --password '***'

    # 只更新某几项（其余保留）
    python3 tools/imoo_credentials.py set --chip-id <值>

    # 看当前状态（脱敏）
    python3 tools/imoo_credentials.py show

    # 用已采集的凭据探一次 API，确认能不能用
    python3 tools/imoo_credentials.py verify
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 允许直接 `python3 tools/xxx.py` 运行（把 src 加进 sys.path）
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

CRED_PATH = Path.home() / ".config" / "xtc-bridge" / "imoo-credentials.json"

# 凭据文件里的字段。顺序即展示顺序。
FIELDS = (
    ("account_id", "账号 ID", "登录响应"),
    ("mobile_id", "mobileId", "BRP 的 accountId 用它"),
    ("bind_number", "手表绑定号", "ADB 读 App 界面"),
    ("chip_id", "设备 token", "= 登录响应 data 原始串"),
    ("encrypt_key", "加密密钥", "客户端随机生成，见文档 §7.8"),
    ("sign", "登录签名", "登录响应（调试用）"),
    ("device_id", "会话 deviceId", "登录响应（调试用）"),
    ("phone", "登录手机号", "用户提供"),
)


# --------------------------------------------------------------------- 存取
def load_creds() -> dict:
    """读凭据文件；不存在或损坏都返回空 dict，不抛异常。"""
    try:
        with open(CRED_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_creds(creds: dict) -> None:
    """原子写入，权限 0600。先写临时文件再 rename，避免写一半断电留下坏文件。"""
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CRED_PATH.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(CRED_PATH.parent), prefix=".cred-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(creds, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CRED_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mask(value: str) -> str:
    """脱敏显示：只留前 4 位和长度。空值明确显示"未采集"。"""
    s = str(value or "")
    if not s:
        return "未采集"
    if len(s) <= 6:
        return f"{s[:2]}…（{len(s)} 位）"
    return f"{s[:4]}…（{len(s)} 位）"


# --------------------------------------------------------------------- ADB
def adb(*args: str, timeout: int = 25) -> tuple[bool, str]:
    """跑一条 adb 命令。返回 (成功, 输出)。任何异常都转成失败而不是抛出。"""
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"


def pick_device(serial: str = "") -> str:
    """选一台在线设备。指定 serial 时校验它确实在线。"""
    ok, out = adb("devices")
    if not ok:
        raise RuntimeError(f"adb 不可用：{out.strip()[:120]}")
    online = [ln.split()[0] for ln in out.splitlines()[1:]
              if len(ln.split()) >= 2 and ln.split()[1] == "device"]
    if not online:
        raise RuntimeError("没有在线设备。先在手机上打开「无线调试」或用 USB 连上。")
    if serial:
        if serial not in online:
            raise RuntimeError(f"指定的设备 {serial} 不在线；当前在线：{online}")
        return serial
    return online[0]


def read_bind_number(serial: str) -> str:
    """从 App 界面读绑定号。

    走的是「更多 → 宝贝个人中心 → 手表绑定号」这一页，页面上有一行
    `绑定号:xxxxxxxxxxxxxxxx`。全程只用 uiautomator dump，不点击、不改状态。
    """
    # 先把 App 拉到前台（幂等，不会丢数据）
    adb("-s", serial, "shell", "am", "start", "-n", "com.xtc.watch/.MainActivity")
    for _ in range(3):
        # 直接搜当前界面的全部文本节点，命中就返回，省得一路点进去
        ok, out = adb("-s", serial, "shell", "uiautomator", "dump", "/sdcard/_cred_ui.xml")
        if not ok or "dumped" not in out:
            continue
        ok, xml = adb("-s", serial, "shell", "cat", "/sdcard/_cred_ui.xml")
        if not ok:
            continue
        m = re.search(r"绑定号[:：]\s*([a-z0-9]{8,32})", xml)
        if m:
            return m.group(1)
        # 没命中说明还没进到那一页；这里不自动点击（避免误触其它 App），
        # 由调用方提示用户手动打开该页。
        break
    return ""


# --------------------------------------------------------------------- 登录
def login(phone: str, password: str) -> dict:
    """账号密码登录，返回 account_id / sign / device_id / token。

    `token`（也就是 BRP 里的 chip_id）**不是**响应里的某个子字段，
    而是 `data` 这一整个 JSON 字符串。依据是脱壳后的代码：

        com/xtc/account/service/impl/Gibraltar.java:193-194
            r = Gabon(loginResponse.data, SSOLoginResult.class);
            r.setTicket(loginResponse.data);      // ticket = data 原始串
        com/xtc/account/service/Gibraltar.java:24
            m.setToken(r.getTicket());

    所以这里直接把原始串存成 token，别再拆子字段去试。
    失败抛 RuntimeError。
    """
    from xtc_qq_bridge.api.sso import SsoClient, SsoError

    try:
        r = SsoClient().password_login(phone, password)
    except SsoError as e:
        # 把"密码错 / 网络错 / 参数错"分开报，便于用户自己判断
        hint = ""
        if e.kind == "credentials":
            hint = f"（还可尝试 {e.attempts_left} 次）" if e.attempts_left is not None else ""
        raise RuntimeError(f"登录失败：{e.message or e.kind}{hint}") from e

    raw = r.raw or {}
    return {
        "account_id": str(raw.get("accountId", "") or ""),
        "sign": str(raw.get("sign", "") or ""),
        "device_id": str(raw.get("deviceId", "") or ""),
        # 关键：ticket 就是 data 原始串
        "chip_id": r.ticket,
        "mobile_id": r.mobile_id,
    }


# --------------------------------------------------------------------- 命令
def cmd_show(_args) -> int:
    creds = load_creds()
    print(f"凭据文件：{CRED_PATH}")
    print(f"            {'存在' if CRED_PATH.exists() else '不存在'}\n")
    print(f"  {'字段':<14}{'含义':<16}{'来源':<22}当前值")
    print(f"  {'-'*14}{'-'*16}{'-'*22}{'-'*26}")
    for key, label, source in FIELDS:
        print(f"  {key:<14}{label:<16}{source:<22}{mask(creds.get(key))}")
    missing = [k for k, _, _ in FIELDS[:4] if not creds.get(k)]
    if missing:
        print(f"\n  还缺：{', '.join(missing)}")
        if "chip_id" in missing:
            print("  chip_id 需要 root 才能从 App 私有存储取出，见文档 §7.4。")
    return 0


def cmd_collect(args) -> int:
    creds = load_creds()
    changed = False

    # 1) 账号密码登录 —— 不需要手机，先做这个
    phone = args.phone or creds.get("phone", "")
    password = args.password or os.environ.get("XTC_PASSWORD", "")
    if phone and password:
        print("[1/2] 账号登录…")
        try:
            got = login(phone, password)
            for k, v in got.items():
                if v and creds.get(k) != v:
                    creds[k] = v
                    changed = True
            creds["phone"] = phone
            print(f"      ✓ account_id = {mask(creds.get('account_id'))}")
        except RuntimeError as e:
            print(f"      ✗ {e}")
    else:
        print("[1/2] 跳过登录（没给 --phone/--password，也没设 XTC_PASSWORD）")

    # 2) 绑定号 —— 需要手机
    print("[2/2] 读手表绑定号…")
    if args.no_adb:
        print("      - 跳过（--no-adb）")
    else:
        try:
            serial = pick_device(args.serial)
            bind = read_bind_number(serial)
            if bind:
                if creds.get("bind_number") != bind:
                    creds["bind_number"] = bind
                    changed = True
                print(f"      ✓ bind_number = {mask(bind)}")
            else:
                print("      - 没读到。请在手机上打开「更多 → 宝贝个人中心 → 手表绑定号」"
                      "那一页后重跑，或直接用 --bind-number 指定。")
        except RuntimeError as e:
            print(f"      - {e}")

    if args.bind_number:
        if creds.get("bind_number") != args.bind_number:
            creds["bind_number"] = args.bind_number
            changed = True
        print(f"      ✓ bind_number = {mask(args.bind_number)}（命令行指定）")
    if args.chip_id:
        creds["chip_id"] = args.chip_id
        changed = True
        print(f"      ✓ chip_id = {mask(args.chip_id)}（命令行指定）")

    if changed:
        save_creds(creds)
        print(f"\n已写入 {CRED_PATH}（权限 600）")
    else:
        print("\n没有新内容，文件未改动")
    return 0


def cmd_set(args) -> int:
    creds = load_creds()
    mapping = {
        "account_id": args.account_id,
        "mobile_id": args.mobile_id,
        "bind_number": args.bind_number,
        "chip_id": args.chip_id,
        "encrypt_key": args.encrypt_key,
        "phone": args.phone,
    }
    n = 0
    for k, v in mapping.items():
        if v is not None:
            creds[k] = v
            n += 1
            print(f"  {k} = {mask(v)}")
    if n:
        save_creds(creds)
        print(f"\n已更新 {n} 项 -> {CRED_PATH}")
    else:
        print("没有指定任何字段")
    return 0


def cmd_verify(_args) -> int:
    """用已采集的凭据调一次 API，确认能不能用。"""
    from xtc_qq_bridge.api.client import ImooApiClient
    from xtc_qq_bridge.api.crypto import HttpAccount

    creds = load_creds()
    acct = HttpAccount(
        account_id=creds.get("account_id", ""),
        bind_number=creds.get("bind_number", ""),
        chip_id=creds.get("chip_id", ""),
        encrypt_key=creds.get("encrypt_key", ""),
    )
    print(f"凭据：{acct}\n")

    client = ImooApiClient(account=acct, enabled=True)
    # 域名列表不需要 token，先确认网络与签名链路是通的
    r = client.get("/server/domain/list")
    print(f"GET /server/domain/list -> {'✓' if r.ok else '✗'} code={r.code or '-'} "
          f"{'' if r.ok else r.error[:80]}")
    if r.ok and isinstance(r.data, list):
        print(f"  返回 {len(r.data)} 套环境")

    if not creds.get("chip_id"):
        print("\nchip_id 未采集，其余接口都会失败 —— 这是当前唯一的硬性缺口。")
        return 0

    r2 = client.get("/mobileinit/authid")
    print(f"GET /mobileinit/authid  -> {'✓' if r2.ok else '✗'} code={r2.code or '-'} "
          f"{'' if r2.ok else r2.error[:80]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="小天才云端凭据采集（结果写到 ~/.config，不进仓库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[-1] if "用法" in __doc__ else "")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="查看当前凭据（脱敏）")
    p_show.set_defaults(func=cmd_show)

    p_col = sub.add_parser("collect", help="采集凭据（登录 + 读绑定号）")
    p_col.add_argument("--phone", help="登录手机号")
    p_col.add_argument("--password", help="登录密码（也可用环境变量 XTC_PASSWORD）")
    p_col.add_argument("--serial", default="", help="adb 设备序列号（多设备时指定）")
    p_col.add_argument("--bind-number", help="直接指定绑定号，跳过 ADB")
    p_col.add_argument("--chip-id", help="直接指定 token")
    p_col.add_argument("--no-adb", action="store_true", help="完全不碰手机")
    p_col.set_defaults(func=cmd_collect)

    p_set = sub.add_parser("set", help="单独设置某一项")
    p_set.add_argument("--account-id")
    p_set.add_argument("--mobile-id", help="mobileId，BRP 的 accountId 用它")
    p_set.add_argument("--bind-number")
    p_set.add_argument("--chip-id", help="设备 token，等于登录响应 data 的原始串")
    p_set.add_argument("--encrypt-key", help="客户端随机生成的 16 位 AES 密钥")
    p_set.add_argument("--phone")
    p_set.set_defaults(func=cmd_set)

    p_ver = sub.add_parser("verify", help="用当前凭据调一次 API")
    p_ver.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
