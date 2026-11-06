# -*- coding: utf-8 -*-
"""项目元信息（版本/作者/仓库/许可证/版权）：程序入口、编译脚本、CI 都读这里。"""

__version__ = "1.0.0-rc.1"
APP_NAME = "xtc-qq-bridge"          # 可执行文件名与数据目录名（保持小写，兼容各平台路径）

PROJECT_TITLE = "XTC-QQ-Bridge"     # 对外展示名称
AUTHOR = "Dinganzhi"
REPO_URL = "https://github.com/Dinganzhi/xtc-qq-bridge"
ISSUES_URL = REPO_URL + "/issues"
LICENSE_NAME = "Apache-2.0"
COPYRIGHT = "Copyright 2026 Dinganzhi"

# 功能冻结开关：1.0.0-rc.1 起不再新增功能，只做缺陷修复与稳定性改进。
# 置为 True 时，「关于」文案与启动横幅都会多打一行提示，避免使用者期待新功能。
FEATURE_FROZEN = True


def about_text() -> str:
    """「关于」命令文案（QQ 侧与手表侧共用；保持精简，便于手表窄屏显示）。

    行数上限是 6 行（手表窄屏只有寥寥几行可见，tests 有守卫）。功能冻结的提示因此
    附在**首行版本号后面**，不额外占用一行：`XTC-QQ-Bridge 1.0.0-rc.1（功能冻结）`。
    """
    head = f"{PROJECT_TITLE} {__version__}" + ("（功能冻结）" if FEATURE_FROZEN else "")
    return (f"{head}\n"
            f"作者：{AUTHOR}\n"
            f"许可：{LICENSE_NAME}\n"
            f"仓库：{REPO_URL}\n"
            f"反馈：{ISSUES_URL}\n"
            f"版权：{COPYRIGHT}")
