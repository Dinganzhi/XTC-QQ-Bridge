# CONTRIBUTORS

本项目的存在离不开以下每一位贡献者。感谢你们。

> 本文件是**不受版本周期限制**的完整名册：Release Notes 只覆盖"上一个稳定版到本次发布"
> 这一区间，注定有限；这里则记录项目历史上所有出过力的人。
>
> 排序方式：先维护者，再按首次贡献时间从早到晚。如需补充或更正自己的条目，
> 欢迎直接提 PR 修改本文件。

---

## Core Maintainers

- **[@Dinganzhi](https://github.com/Dinganzhi)** — 项目负责人。整体设计、IM 协议逆向、
  ADB 控制层、转发与去重、构建与发布流程。

  <sub>同一人在历史提交中使用过的其它身份：`xtc <xtc@local>`（早年本地 git 身份，
  约 26 次提交）、`Dinganzhi <d963793201@163.com>`、`Dinganzhi <115438834+Dinganzhi@users.noreply.github.com>`。
  这些身份在贡献者统计中已合并计入 @Dinganzhi，不会重复列出。</sub>

## All Contributors

- **[@Dinganzhi](https://github.com/Dinganzhi)** — 项目创建、核心实现、文档与发布
- **[@ShameimaruAyaw](https://github.com/ShameimaruAyaw)** — AstrBot 插件生命周期审查，
  修复结果回传竞态、超时任务残留与停止阻塞（#PR 见仓库提交记录）

---

## 如何被列在这里

只要你的改动进入了仓库（代码、文档、测试、设计建议均可），就会出现在上面的名单里。
新增条目的推荐格式：

```markdown
- **[@用户名](https://github.com/用户名)** — 一句话说明贡献内容
```

## 关于机器人账号

自动化账号（`dependabot`、`github-actions`、`renovate` 等，或用户名带 `[bot]` 后缀）
**不会**出现在本名册，也不会出现在 Release Notes 的贡献者段落里 —— 生成脚本
`scripts/release_contributors.py` 会统一剔除。
