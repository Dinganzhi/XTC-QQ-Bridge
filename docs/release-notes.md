# 发布说明：贡献者段落的写法

本文约定 Release Notes 里"贡献者"部分怎么写。核心是一条**混合策略**：

> 预发布看"这一轮新增了谁"（差异），同时保留"整个版本周期都有谁"（累计）；
> 正式版只保留累计。

理由：预发布阶段读者想知道"这次迭代来了哪些新面孔"，但上一个预发布的贡献者
不能因为本轮没有新提交就被抹掉；到了正式版，阶段之间的差异已无意义，只留完整名单。

---

## 一、三档策略

| 版本类型 | `New Contributors` | `All Contributors` |
|---|---|---|
| 预发布（alpha / beta / rc） | ✅ 自**上一个预发布**以来 | ✅ 自**上一个稳定版**以来 |
| 正式版（x.y.z） | ❌ 不出现 | ✅ 自**上一个稳定版**以来 |
| 历史总册 | — | 由仓库根部 [`CONTRIBUTORS.md`](../CONTRIBUTORS.md) 承担 |

`CONTRIBUTORS.md` 与 Release Notes 分工明确：

- **Release Notes** 回答"这次谁参与了"；
- **CONTRIBUTORS.md** 回答"历史上谁来过"，不受版本周期限制。

---

## 二、输出示例

### 预发布：`1.1.0-alpha.2`

```markdown
## 🎉 New Contributors（自 1.1.0-alpha.1 以来）

感谢首次为本版本线做出贡献的开发者！

- @alice made their first contribution in #101
- @bob made their first contribution in #105

## 🙌 All Contributors（自 1.0.0 以来）

感谢每一位参与本版本周期的贡献者！

- @alice
- @bob
- @carol
- @dave
```

`@carol`、`@dave` 是 alpha.1 的贡献者，本轮没有新提交，但仍在累计名单里。

### 正式版：`1.1.0`

```markdown
## 🙌 All Contributors（自 1.0.0 以来）

感谢每一位参与本版本周期的贡献者！

- @alice
- @bob
- @carol
- @dave
- @erin
```

范围是 `1.0.0..1.1.0`，覆盖整个版本周期（含期间的 alpha / beta / rc 阶段）。
不再出现 `New Contributors` 段。

---

## 三、自动化实现

脚本：[`scripts/release_contributors.py`](../scripts/release_contributors.py)

它负责三件事：

1. 从 tag 列表推断**上一个稳定版**与**上一个预发布**（同版本线内优先）；
2. 用 compare 接口取"上一个稳定版 → 当前版本"的全部提交作者（去重、剔 bot）；
3. 预发布时再调 generate-notes 接口，取其 `New Contributors` 段落的条目，
   统一改写成 `- @user made their first contribution in #101`。

bot 账号（`dependabot`、`github-actions`、`renovate`、`codecov`、`*[bot]` 等）一律剔除。

### 本地运行

```bash
# 自动推断范围，输出到标准输出
GITHUB_TOKEN=xxx python scripts/release_contributors.py \
    --repo owner/name --tag 1.1.0-alpha.2

# 追加到已有的手写说明后面
GITHUB_TOKEN=xxx python scripts/release_contributors.py \
    --repo owner/name --tag 1.1.0-alpha.2 \
    --base-body .github/release-notes/1.1.0-alpha.2.md \
    --out release_body.md

# 离线渲染（调试 / CI 无关的试验，不联网）
python scripts/release_contributors.py --tag 1.1.0-alpha.2 --offline \
    --previous-prerelease 1.1.0-alpha.1 --previous-stable 1.0.0 \
    --new-contributors alice,bob --all-contributors alice,bob,carol,dave
```

### CI 中的位置

`.github/workflows/build-release.yml` 的 `release` job 里有一个
**Compose release body（含贡献者段落）** 步骤：

- 传了 `body_path`（手写说明）→ 在其后追加贡献者段落；
- 没传 → 先用 generate-notes 的变更正文当底稿（并剔除它自带的贡献者段，避免重复），
  再追加我们生成的段落；
- 该步骤失败时（如 token 权限不足）不阻断发布，自动退回原来的手写说明或
  GitHub 自动生成的说明 —— 发布不能因为"致谢排版"而失败。

所需的 token 权限：`contents: write`（读 tag / compare / generate-notes，写 Release）。

---

## 四、维护 CONTRIBUTORS.md

新增贡献者时，往 [`CONTRIBUTORS.md`](../CONTRIBUTORS.md) 的 `All Contributors`
追加一行即可：

```markdown
- **[@用户名](https://github.com/用户名)** — 一句话说明贡献内容
```

排序规则：维护者在前，其余按**首次贡献时间**从早到晚。

同一个人的多个 git 身份合并成一条（例如维护者早年的本地身份 `xtc <xtc@local>`
与 GitHub 账号 `@Dinganzhi` 是同一个人，只在维护者条目下注明，不重复列出）。

---

## 五、测试

策略的正确性由离线测试保证，不需要联网、不需要 token：

```bash
python tests/test_release_contributors.py
```

覆盖：版本号解析、预发布判定、上一个稳定版/上一个预发布的挑选、排序
（alpha < beta < rc < 正式版）、bot 剔除、条目改写、段落剔除、两档场景的输出、
正文拼接与边界（无贡献者时不输出空标题）。
