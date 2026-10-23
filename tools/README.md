# tools/ —— 独立小工具

这里的脚本**不参与主程序运行**，只在排查问题、实机验证、一次性迁移时使用。
共享主项目代码时统一 `sys.path.insert(0, "src")` 后 `from xtc_bridge... import ...`。

| 工具 | 用途 |
|---|---|
| `dump_ui.py` | 打印当前界面控件（填 UI 映射表用） |
| `ui_report.py` | 把当前界面控件导出成文本文件（避免控制台中文乱码） |
| `live_probe.py` | 实机一键体检：连接 / 界面 / 登录态 / 联系人 / 读取 / 注入（不发真实消息） |
| `selftest.py` | 环境自检（不依赖配置文件） |
| `bubble_dump_probe.py` | 打印聊天窗口里每个气泡的原始字段 + 列表可视区 / 是否被裁 |
| `emoji_clip_probe.py` | 复现"气泡被列表裁掉"并验证自动滚进可视区后能取到原图 |
| `emoji_live_probe.py` | 验证表情取图：抠气泡截图 -> 逐候选比像素 -> 导出挑中的图 |
| `image_live_probe.py` | 验证照片转发：定位图片气泡 -> 取原图 / 截图并导出 |
| `photo_diag.py` | 照片转发失败的现场诊断：逐张打印候选被哪道门拦下 |
| `dump_compare_probe.py` | 对比"完整 dump / 压缩 dump"的耗时与节点 |
| `profile_probe.py` | 打印各速度档位下实际生效的参数值 |
| `forward_audit_probe.py` | 从日志审"转发顺序 / 重复 / 发晚了" |
| `find_unused.py` | 静态扫描：未使用的 import / 只赋值不读的属性 |
| `migrate_history_labels.py` | 一次性迁移：把历史记录里的相对时间标签换成绝对标签 |

测试用例在 `tests/`，构建脚本在 `packaging/nuitka/`，开发脚本在 `scripts/`。
