# AI 代码审查（Claude Code）

本目录的工作流 `.github/workflows/ai-review.yml` 用 Anthropic 官方的
[`claude-code-action`](https://github.com/anthropics/claude-code-action) 在每个
pull request 上跑一次只读代码审查，把结果作为 PR 评论发出来。

**它不是合并门禁。** 分支保护里要求的 5 项检查全是 CI 和静态扫描，不包含
这个工作流。它的作用是"多一双眼睛找问题"，不替代人工判断。

---

## 当前状态：未启用（且从未实跑成功）

工作流文件已经在仓库里，但**默认整条跳过**，而且**从来没有实跑成功过**。

三重跳过条件（写在 job 的 `if` 里）：

| 条件 | 效果 |
|---|---|
| `vars.AI_REVIEW_ENABLED != 'true'` | 整个 job 不跑（默认关闭） |
| PR 来自 fork | 不跑（跨仓库 PR 拿不到 secret，也不在本仓库信任范围内） |
| PR 作者是 `dependabot[bot]` | 不跑（依赖升级不需要 AI 审查，省额度） |

> 用 `vars`（仓库变量）而不是 `secrets` 做开关，是因为 GitHub 的上下文可用性
> 表格规定 `jobs.<job_id>.if` 只能看到 `github, needs, vars, inputs` ——
> **`secrets` 在 job 级 `if` 里不可用**，写上去会在表达式求值时报错。

启用还需要另外两样：

| 需要什么 | 在哪配 | 现在状态 |
|---|---|---|
| 仓库变量 `AI_REVIEW_ENABLED` = `true` | 仓库 → Settings → Secrets and variables → Actions → Variables | ❌ 未配置 |
| `ANTHROPIC_API_KEY` secret | 同上 → Secrets | ❌ 未配置 |
| 允许 Claude 审查 PR | 仓库 → Settings → General → Features → "Allow Claude Code to review and comment on pull requests" | ❌ 未开启 |

密钥没配之前，即使把开关打开，工作流也会在第一步失败
（`validateEnvironmentVariables` 报 "Either ANTHROPIC_API_KEY ... is required"）。

> 这个工作流**不会**因为失败而挡住合并——分支保护没有把它列为 required
> status check。但它会在 Actions 页面显示为红色失败。

---

## 启用步骤

### 1. 打开开关

```sh
gh variable set AI_REVIEW_ENABLED --body true --repo perry421/perry-wechat-ai
```

### 2. 配 secret

```sh
gh secret set ANTHROPIC_API_KEY --repo perry421/perry-wechat-ai
```

会提示你粘贴密钥值。**不要**写进任何提交过的文件。

### 3. 开启 PR 审查开关

仓库 → Settings → General → Features → 勾选
"Allow Claude Code to review and comment on pull requests"。

这一步是 `claude-code-action` 往 PR 上发评论的前置条件——即使密钥配好了，
没开这个开关，审查也只会跑完但发不出评论。

### 4. 开一个测试 PR 验证

推一个分支开 PR，看 Actions 里 `AI Review / Claude 代码审查` 这一项。
第一次跑建议顺便确认：

- 评论里没有泄露密钥或真实聊天内容
- 评论确实贴在改动对应的行上
- 消耗了多少 token（用量会记在 Anthropic 控制台）

---

## 工作流做了什么、没做什么

### 做

- 检出 PR 头提交 + 拉全量历史（`fetch-depth: 0`）
- 调 Claude 读 diff 和相关文件
- 用 inline comment 把问题贴到对应行，用 `gh pr comment` 发总体结论
- 只用 `Read`/`Grep`/`Glob`/`git diff`/`gh pr view` 这类只读工具

### 不做

- **不改代码**：`allowedTools` 里没有 `Edit`/`Write`，`disallowedTools` 里再禁一次
- **不提交、不推分支**：没有 `Bash(git push:*)`
- **不批准 PR**：只给 inline comment，没有 review approve 权限
- **不碰 main**：只读 `contents: read`
- **不审 fork PR 和 Dependabot PR**：见上面的跳过条件

### 审查重点（本项目专属）

写在工作流的 `prompt` 里，针对"代替本人发微信消息"这个场景：

1. 发送安全门的**默认行为**是否被放宽
   （`PWA_ENABLE_SEND=false`、`PWA_SEND_DRY_RUN=true`、白名单为空即拒绝、
   逐条批准、频率限制、紧急停止）
2. 是否把异常原文、内部路径、聊天内容写进了 HTTP 响应或日志
3. 是否把密钥、真实聊天记录、截图、数据库文件写进代码
4. 聊天内容是否被当作**不可信输入**处理
5. 去重逻辑是否会被绕过
6. 新增测试是否真的覆盖了它声称覆盖的场景

并且明确要求：没问题就说"未发现问题"，不要凑数提风格建议。

---

## 成本

每次 PR 触发一次。默认按 Sonnet 计费，一个中等规模 PR 大约几十万 token。
`concurrency` 配了 `cancel-in-progress: true`，同一 PR 连续推送时旧的会被取消，
不会叠加消耗。Dependabot 的 PR 已经排除，不消耗额度。

如果嫌贵，两个降低成本的选项：

- **只在 `synchronize` 时跑**，去掉 `opened`：
  ```yaml
  on:
    pull_request:
      types: [synchronize]
  ```
- **只审特定路径**：
  ```yaml
  on:
    pull_request:
      types: [opened, synchronize]
      paths:
        - "pwa/**"
        - "tests/**"
  ```

---

## 依赖固定

`claude-code-action` 固定在 commit SHA
`a5a423f75c251dcbff8a74e99f83b2cb03c72bfa`（v1.0.256），不用浮动 tag。
升级时改这一行，并同步更新注释里的版本号。

### v1 的两个坑（写这个工作流时踩过）

- **`custom_instructions` 输入在 v1 已被移除。** 官方迁移指南
  （`docs/migration-guide.md`，"Migration from Deprecated Inputs"）把它列在废弃
  输入表里，替代方案是 `claude_args: "--append-system-prompt '...'"`。
  本工作流的审查指令直接放在 `prompt` 里，不需要额外 system prompt。
- **`secrets` 不能用在 `jobs.<job_id>.if`。** GitHub 上下文可用性表格里这一行的
  Context 是 `github, needs, vars, inputs`。step 级 `if` 有 `env` 但同样没有
  `secrets`。所以开关只能用 `vars`。

---

## 相关

- 安全扫描（gitleaks / pip-audit / CodeQL）→ `.github/workflows/security.yml`
- 常规 CI（pytest）→ `.github/workflows/ci.yml`
- 漏洞报告方式 → `CONTRIBUTING.md`
