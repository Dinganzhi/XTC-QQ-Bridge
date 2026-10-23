# -*- coding: utf-8 -*-
# nuitka-project: --windows-console-mode=force
"""小天才 <-> QQ 桥接 —— 仓库入口（薄封装）。

真正实现在 `src/xtc_bridge/`：这里只把 `src` 加进 `sys.path`，再转交给命令行入口。
这样既能 `python main.py` 直接跑；`pip install .` 之后还可以用 `python -m xtc_bridge` 或 `xtc-qq-bridge` 命令。

常用参数：
  python main.py --check                # 环境自检
  python main.py --debug dump-ui        # 打印当前界面控件
  python main.py --debug adb-info       # 打印 adb / 输入法 / 剪贴板诊断
  python main.py --once                 # 轮询一轮后退出
  python main.py --paths                # 打印运行路径
  python main.py --catchup [屏数]       # 一次性补发积压消息
  python main.py --install-plugin       # 安装/更新 AstrBot 插件
  python main.py                        # 正常启动桥接
"""
from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    """把 <repo>/src 放进 sys.path，并移除仓库根目录本身。

    移除根目录的原因：仓库里有个 `packaging/`（放 Nuitka 构建脚本），而标准库/第三方
    同名包 `packaging` 被 Nuitka、pip 等广泛使用 —— 根目录留在 sys.path 上会遮蔽它。
    """
    here = Path(__file__).resolve().parent
    src = here / "src"
    cleaned = []
    for p in sys.path:
        try:
            if Path(p or ".").resolve() == here:
                continue
        except Exception:  # noqa: BLE001 无法解析的路径原样保留
            pass
        cleaned.append(p)
    sys.path[:] = cleaned
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


_bootstrap()

from xtc_bridge.cli.main import main  # noqa: E402

if __name__ == "__main__":
    main()
