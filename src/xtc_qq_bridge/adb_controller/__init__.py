# -*- coding: utf-8 -*-
"""ADB 控制层（platform / controller / errors 三个模块）。"""
from .platform import *  # noqa: F401,F403
from .errors import AdbError
from .controller import ADBController
from .controller import _silent_logger  # noqa: F401  兼容旧的模块级访问
