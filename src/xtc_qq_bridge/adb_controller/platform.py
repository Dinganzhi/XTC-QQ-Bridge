# -*- coding: utf-8 -*-
"""ADB 探测与平台适配：adb 路径查找、常见端口、容器/模拟器启动、输入法与剪贴板。

支持环境（Windows / Linux / macOS 均可）：
- Windows：雷电（LDPlayer）、MuMu 等模拟器
- Linux：Waydroid、Genymotion、Android-x86 / 夜神 / 逍遥等提供 adb 端口的模拟器
- macOS：Genymotion、Android Studio 模拟器、真机
- 任何能通过 `adb connect` 接入的 Android 实例（含真机 USB / 网络调试）

设备查找顺序：**已有的在线设备** -> 环境变量 ADB_SERIAL -> config 指定的端口
-> 常见端口（5555 / 16384 / 62001 …）-> 多次重试。任何情况下都不会覆盖已连接的
设备去抢端口。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .errors import AdbError

# ---------------------------------------------------------------- 常见安装路径
# 常见 LDPlayer 安装目录（Windows 模拟器，内含 adb.exe）
_LDPLAYER_DIRS = [
    "D:\\leidian\\LDPlayer14", "D:\\leidian\\LDPlayer9", "D:\\leidian\\LDPlayer",
    "C:\\leidian\\LDPlayer14", "C:\\leidian\\LDPlayer9", "C:\\leidian\\LDPlayer",
    "D:\\LDPlayer\\LDPlayer14", "D:\\LDPlayer\\LDPlayer9",
    "C:\\LDPlayer\\LDPlayer14", "C:\\LDPlayer\\LDPlayer9",
]

# MuMu 模拟器 adb 位置（MuMu 15: nx_main\adb.exe；MuMu 12: shell\adb.exe；MuMu 6: vmonitor\bin）
_MUMU_ADB_CANDIDATES = [
    "D:\\Program Files\\Netease\\MuMu\\nx_main\\adb.exe",
    "D:\\Program Files\\Netease\\MuMu\\emulator\\nemu\\vmonitor\\bin\\adb_server.exe",
    "D:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\adb.exe",
    "D:\\Program Files\\Netease\\MuMuPlayer-12.0\\vmonitor\\bin\\adb_server.exe",
    "C:\\Program Files\\Netease\\MuMu\\nx_main\\adb.exe",
    "C:\\Program Files\\Netease\\MuMuPlayer-12.0\\shell\\adb.exe",
]

# Windows 上常见的 platform-tools adb 位置
_WIN_ADB_CANDIDATES = [
    "%LOCALAPPDATA%\\Microsoft\\WindowsApps\\adb.exe",   # winget 安装的 platform-tools
    "%LOCALAPPDATA%\\Android\\Sdk\\platform-tools\\adb.exe",
    "%USERPROFILE%\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe",
    "%USERPROFILE%\\platform-tools\\adb.exe",
    "%USERPROFILE%\\scoop\\shims\\adb.exe",
    "%USERPROFILE%\\scoop\\apps\\adb\\current\\adb.exe",
    "C:\\platform-tools\\adb.exe",
    "C:\\adb\\adb.exe",
    "C:\\ProgramData\\chocolatey\\bin\\adb.exe",
]

# Linux / macOS 上常见的 adb 位置（~ 与 $HOME 会被展开）
_UNIX_ADB_CANDIDATES = [
    "~/Android/Sdk/platform-tools/adb",
    "~/android-sdk/platform-tools/adb",
    "~/Library/Android/sdk/platform-tools/adb",          # macOS
    "/usr/lib/android-sdk/platform-tools/adb",           # Debian/Ubuntu 包
    "/usr/local/lib/android/sdk/platform-tools/adb",
    "/opt/android-sdk/platform-tools/adb",
    "/opt/android/platform-tools/adb",
    "/usr/bin/adb",
    "/usr/local/bin/adb",
    "/snap/bin/adb",
]

# 模拟器/容器常见的 adb 端口
#   5555   = 通用 TCP/IP 调试（Genymotion、Android-x86、真机网络调试、雷电/MuMu）
#   16384  = MuMu；7555 = 部分雷电实例；21503 = 逍遥；62001/62025 = 夜神
#   5556   = Waydroid 多实例；6520 = Android 容器常见端口
_EMULATOR_COMMON_PORTS = [5555, 16384, 7555, 21503, 62001, 62025, 5556, 6520]

# uiautomator dump 的落盘目录候选（按可写性排序）。
#   /sdcard 未挂载或 scoped storage 拦截时会写不进去（表现为 "cat: ... No such file"），
#   /data/local/tmp 对 shell 用户一定可写，作为兜底。
_DUMP_DIRS = ("/sdcard", "/data/local/tmp", "/storage/emulated/0")

# 抢到窗口焦点但**不代表 App 退到后台**的叠加层（输入法、系统弹窗等）。
# 判断"App 是否占着屏幕"时要放行这些包，否则键盘一弹就误判未登录/不在前台。
_SYSTEM_OVERLAY_MARKERS = (
    "inputmethod", "adbkeyboard", "com.android.systemui", "permissioncontroller",
    "packageinstaller", "android.system", "com.android.internal",
)

# 没有在线设备时可尝试自动拉起的模拟器/容器（Linux 优先 Waydroid）：
#   命令 -> (可执行文件候选, 参数列表)
_LAUNCHERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # Linux（Waydroid 官方建议 wayland；会话是 X11 时加 -X）
    "waydroid": (("waydroid",), ("session", "start")),
    "waydroid-x": (("waydroid",), ("session", "start", "-X")),
    "genymotion": (("genymotion-player", "genyshell"), ("-n",)),
    # Windows
    "ldplayer": (("dnplayer.exe",), ()),
    "mumu": (("MuMuPlayer.exe",), ()),
    "bluestacks": (("HD-Player.exe",), ()),
}

ADBKEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

ADBKEYBOARD_PKG = "com.android.adbkeyboard"

# APK 只从项目目录里的捆绑文件安装（keyboardservice-debug.apk / ADBKeyBoard.apk），
# 不做任何联网下载。
ADBKEYBOARD_APK_NAMES = ("keyboardservice-debug.apk", "ADBKeyBoard.apk")

# 文本注入相关常量
_CODEPOINT_CHUNK = 200          # ADB_INPUT_CHARS 每批码点数（命令行长度限制）

_TEXT_RETRY_SLEEP = 0.35        # 广播后等待 IME 提交文本

# 启动 App 时的"硬失败"特征：命令自己就报错了，换下一套策略即可，请勿再等前台
_LAUNCH_FAIL_RE = re.compile(
    r"(?mi)^\s*(Error|Exception)\b|\bError type \d|does not exist"
    r"|SecurityException|Permission Denial|not exported|Unable to (resolve|find)")

CLIPBOARD_PKG = "com.android.clipper"   # Android 10 及以下可用（若已安装）

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- 工具函数
def _no_window_kwargs() -> dict:
    """子进程参数：Windows 隐藏控制台窗口，类 Unix 起独立会话（避免随终端退出被杀）。"""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def find_adb(explicit: str = "") -> str:
    if explicit and Path(explicit).exists():
        return explicit
    env = os.environ.get("ADB_PATH", "")
    if env and Path(env).exists():
        return env
    if IS_WINDOWS:
        for d in _LDPLAYER_DIRS:
            p = Path(d) / "adb.exe"
            if p.exists():
                return str(p)
        for p in _MUMU_ADB_CANDIDATES:
            if os.path.exists(p):
                return p
        raw_candidates = _WIN_ADB_CANDIDATES
    else:
        raw_candidates = _UNIX_ADB_CANDIDATES
    for raw in raw_candidates:
        p = Path(os.path.expandvars(raw)).expanduser()
        try:
            if p.exists():
                return str(p)
        except OSError:
            continue
    w = shutil.which("adb")
    if w:
        return w
    if IS_WINDOWS:
        raise AdbError("未找到 adb.exe：请在 config.yaml 的 adb.path 指定，或设置环境变量 ADB_PATH，"
                       "或将 adb 加入 PATH（可用 winget install Google.PlatformTools）")
    raise AdbError("未找到 adb：请在 config.yaml 的 adb.path 指定，或设置环境变量 ADB_PATH，"
                   "或安装 platform-tools（Debian/Ubuntu: sudo apt install adb；"
                   "macOS: brew install --cask android-platform-tools；"
                   "通用: 下载 platform-tools 并把 adb 加入 PATH）")


def platform_tag() -> str:
    """当前平台标签，用于日志/提示。"""
    if IS_WINDOWS:
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


def _unauthorized_tip(serial: str) -> str:
    if serial.startswith(("emulator-", "127.0.0.1", "localhost")):
        return "请在该模拟器/容器窗口里点击『允许 USB 调试』"
    if IS_WINDOWS:
        return "请在手机上允许 USB 调试（或 adb kill-server 后重试）"
    return ("请在手机上允许 USB 调试；若列表里根本无法看到设备，"
            "通常需要配置 udev 规则（如 sudo usermod -aG plugdev $USER + "
            "/etc/udev/rules.d/51-android.rules 后重新插拔）")


def _environment_hints() -> list[str]:
    """没有在线设备时，按平台给出针对性的排查建议。"""
    hints: list[str] = []
    if IS_WINDOWS:
        hints.append("模拟器请确认已开启 ADB 调试，并把 adb.port 设成它显示的端口"
                     "（通用 5555、MuMu 16384、夜神 62001、逍遥 21503）")
        hints.append("若端口被占用，管理员执行 netsh int ipv4 add excludedportrange "
                     "protocol=tcp startport=<端口> numberofports=1 后重启")
        return hints

    # Linux / macOS
    if sys.platform == "darwin":
        hints.append("macOS：adb 端口通常为 5555（Genymotion / 真机网络调试），"
                     "Android Studio 模拟器会显示为 emulator-5554 之类的序列号")
        hints.append("macOS：装 adb 用 `brew install --cask android-platform-tools`；"
                     "真机需先在手机上允许 USB 调试")
        return hints

    hints.append("请确认目标已启动且开启了 ADB 调试；adb 端口通常为 5555"
                 "（夜神 62001、逍遥 21503、Waydroid 5555/5556、Genymotion 5555）")
    if waydroid_present():
        hints.append("检测到 Waydroid：先 `waydroid session start`（Wayland 会话）或 "
                     "`waydroid session start -X`（X11 会话），再 "
                     "`adb connect 127.0.0.1:5555`；也可设 adb.auto_launch_emulator: true "
                     "让桥接自动拉起")
    hints.append("Linux：装 adb 用 `sudo apt install adb`（或 android-tools-adb）；"
                 "真机若 `adb devices` 为空，通常要配 udev 规则："
                 "`echo 'SUBSYSTEM==\"usb\", ATTR{idVendor}==\"<厂商ID>\", MODE=\"0666\", "
                 "GROUP=\"plugdev\"' | sudo tee /etc/udev/rules.d/51-android.rules`，"
                 "再 `sudo udevadm control --reload-rules && sudo udevadm trigger`；"
                 "用户需在 plugdev 组（sudo usermod -aG plugdev $USER 后重新登录）")
    hints.append("若通过 SSH/容器运行，注意 X11/Wayland 显示与 /dev/kvm 权限"
                 "（容器内运行模拟器需要 --device /dev/kvm）")
    return hints


def waydroid_present() -> bool:
    """本机是否装了 Waydroid（Linux 上的 Android 容器）。"""
    if IS_WINDOWS or sys.platform != "linux":
        return False
    return bool(shutil.which("waydroid"))


def available_launchers() -> list[str]:
    """本机可用的模拟器/容器启动器（供“没有在线设备时自动拉起”使用）。"""
    found = []
    for name, (bins, _args) in _LAUNCHERS.items():
        if not IS_WINDOWS and name in ("ldplayer", "mumu", "bluestacks"):
            continue
        if IS_WINDOWS and name.startswith("waydroid"):
            continue
        for b in bins:
            if shutil.which(b):
                found.append(name)
                break
    return found


def launch_emulator(name: str = "") -> bool:
    """尝试拉起模拟器/Android 容器（只在用户显式开启 adb.auto_launch_emulator 时调用）。

    name 留空则按可用性顺序挑第一个（Linux 优先 Waydroid）。
    """
    order = [name] if name else (["waydroid", "waydroid-x", "genymotion"] if not IS_WINDOWS
                                 else ["ldplayer", "mumu", "bluestacks"])
    for key in order:
        bins, args = _LAUNCHERS.get(key, ((), ()))
        for b in bins:
            exe = shutil.which(b)
            if not exe:
                continue
            try:
                subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **_no_window_kwargs())
                return True
            except OSError:
                continue
    return False


def set_clipboard(adb: "ADBController", text: str) -> bool:
    """写入设备剪贴板并**回读校验**。

    部分环境的剪贴板与宿主机剪贴板是共享/桥接的：`cmd clipboard set-text`
    若静默失败，随后的 KEYCODE_PASTE 会粘出宿主机剪贴板里的旧内容。
    因此这里必须回读确认，写不进去就直接放弃剪贴板方案。
    """
    from .controller import ADBController as _Ctl   # 延迟导入：避免与 controller 循环依赖
    quoted = _Ctl._sh_quote(text)
    cmds = [f"cmd clipboard set-text {quoted}", f"cmd clipboard set {quoted}"]
    # Android 10 及以下没有 cmd clipboard，若用户装了 Clipper 可走它的广播
    if CLIPBOARD_PKG in adb.try_shell(f"pm list packages {CLIPBOARD_PKG}"):
        cmds.append(f"am broadcast -a clipper.set -e text {quoted}")
    for cmd in cmds:
        try:
            adb.shell(cmd, timeout=15)
        except AdbError:
            continue
        time.sleep(0.2)
        cur = adb.get_clipboard()
        if cur is not None and cur == text:
            return True
    return False


__all__ = [__name for __name in list(globals()) if not __name.startswith('__')]
