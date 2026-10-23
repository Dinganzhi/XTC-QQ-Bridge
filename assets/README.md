# assets/ —— 仓库自带的二进制资源

| 文件 | 用途 |
|---|---|
| `keyboardservice-debug.apk` | ADBKeyBoard 输入法（约 18 KB）。设备上没有输入法时，桥接用它在**不联网**的前提下完成文本注入 |

- 运行时会自动寻找：`<根>/assets/<名字>` → `<根>/<名字>`（兼容手动放置到根目录的情况）。
- 打包（Nuitka）时该文件会随包带上：见 `packaging/nuitka/build.py` 的 `DATA_FILES`。
- 图标等其它静态资源也放这里，代码里用 `runtime_paths.assets_dir()` 取目录。
