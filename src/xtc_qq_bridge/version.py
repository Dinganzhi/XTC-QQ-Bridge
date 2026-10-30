# -*- coding: utf-8 -*-
"""项目元信息（版本/作者/仓库/许可证/版权）：程序入口、编译脚本、CI 都读这里。"""

__version__ = "1.0.0-beta.3"
APP_NAME = "xtc-qq-bridge"          # 可执行文件名与数据目录名（保持小写，兼容各平台路径）

PROJECT_TITLE = "XTC-QQ-Bridge"     # 对外展示名称
AUTHOR = "Dinganzhi"
REPO_URL = "https://github.com/Dinganzhi/xtc-qq-bridge"
ISSUES_URL = REPO_URL + "/issues"
LICENSE_NAME = "Apache-2.0"
COPYRIGHT = "Copyright 2026 Dinganzhi"


def about_text() -> str:
    """「关于」命令文案（QQ 侧与手表侧共用；保持精简，便于手表窄屏显示）。"""
    return (f"{PROJECT_TITLE} {__version__}\n"
            f"作者：{AUTHOR}\n"
            f"许可：{LICENSE_NAME}\n"
            f"仓库：{REPO_URL}\n"
            f"反馈：{ISSUES_URL}\n"
            f"版权：{COPYRIGHT}")
