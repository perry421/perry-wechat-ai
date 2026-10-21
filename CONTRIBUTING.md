# 贡献指南

感谢你愿意参与。这个项目会**代替本人向真实联系人发送微信消息**，所以改动
的门槛比普通开源项目高一些——安全相关的默认行为不能被悄悄改掉。

## 开发环境

```sh
git clone https://github.com/perry421/perry-wechat-ai.git
cd perry-wechat-ai
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

`.env` 里**不要**填真实密钥参与提交；本地测试用 Mock 即可。

## 跑测试

```sh
.venv/bin/python -m pytest tests/ -v
```

测试全部使用 `MockObserver` / `MockProvider`：

- **不连接真实微信**
- **不访问模型端点**
- **不向任何联系人发送消息**

CI 跑的是同一套命令。真实链路（真窗口截图、OCR 坐标、真实发送）只能在
macOS 上手工验证，不要试图在 CI 里模拟。

## 提交前必须确认

- [ ] `pytest` 全部通过
- [ ] 没有提交 `.env`、真实聊天记录、截图、SQLite 数据库
- [ ] 没有改动发送安全门的默认行为（见下）
- [ ] 新配置项已同步写进 `.env.example` 和 README

## 发送安全门是不可随便改的

这套机制是本项目的核心，任何改动必须**保持默认值不变**：

| 机制 | 默认 |
|---|---|
| `PWA_ENABLE_SEND` | `false`（完全没有发送能力） |
| `PWA_SEND_DRY_RUN` | `true`（只填输入框，不按回车） |
| 逐条批准 | 每条草稿单独 approve |
| 会话白名单 | 空列表 = 拒绝一切发送 |
| 发送频率限制 | 60 秒 5 条 |
| 发送前核验 | 登录状态 / 当前会话 / 触发消息仍在上下文 |

如果你的改动会放宽其中任何一条，请在 PR 描述里明确写出来，并说明为什么
这个放宽是必要的。**"顺手放宽一下"不会被合并。**

## 提交信息

用中文或英文都可以，风格保持一致：

```
fix(observer): 语音条「转文字」按钮不再被当成消息

微信 4.1.15 的语音条按钮底色 (41,41,42) 落在对方气泡色容差内，
导致 OCR 把按钮文字当成对方消息。加入 UI_BUTTON_TEXTS 词表过滤。
```

类型前缀：`feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `deps` / `ci`。

## 分支与 PR

- 从 `main` 切分支：`feat/xxx`、`fix/xxx`
- PR 描述要填完整模板，尤其是**测试证据**和**未验证的部分**
- CI（pytest + gitleaks + pip-audit + CodeQL）必须全绿才可合并

## 报告安全问题

发现安全漏洞请**不要**开公开 Issue，直接用 GitHub 的
[私密漏洞报告](https://docs.github.com/en/code-security/security-advisories/guidelines/reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)。
