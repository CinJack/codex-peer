# Codex Peer

让 Claude、Gemini、Antigravity 等外部 AI 代理通过本地命令启动 Codex 会话、给 Codex 分配任务并读取回复。你可以继续和原来的代理对话，由它负责与 Codex 协作。

```text
你 → Claude / Gemini 等代理 → codex-peer → Codex 会话
                              ← 进度和回复 ←
```

例如，你可以让 Claude 检查项目，同时请 Codex 独立审查代码，再由 Claude 汇总结果。Codex Peer 也支持连接本机已有的 Codex VS Code 线程。

## 开始之前

- 外部代理需要能够运行本地 shell 命令，并与 Codex 处于同一台机器或共享同一运行环境。
- 安装并登录 [Codex CLI](https://github.com/openai/codex)，确认 `codex` 命令可以使用。
- 安装 Python 3；Codex Peer 的脚本不需要额外的 Python 包。
- 连接已有 VS Code 线程时，还需要能访问该环境中的 Codex 会话数据和扩展。

**权限提示：**新建会话和通过 CLI 恢复会话时，本工具调用 Codex 的 `--dangerously-bypass-approvals-and-sandbox` 参数，Codex 将不受审批和沙箱限制。请只在你信任的环境中运行，并确认外部代理及其任务具有相应权限。

## 安装

在外部代理能够访问的终端中运行：

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/CinJack/codex-peer.git ~/.codex/skills/codex-peer
export PATH="$HOME/.codex/skills/codex-peer/scripts:$PATH"
codex-peer --help
```

如需在以后打开的终端中使用，将 `export PATH=...` 加入 shell 配置文件。如果外部代理没有继承这个 `PATH`，可以让它直接运行：

```bash
python3 ~/.codex/skills/codex-peer/scripts/codex_monitor.py --help
```

## 让外部代理联系 Codex

打开 Claude、Gemini 或其他能执行本地命令的代理，在对话中发送类似下面的指令。把项目路径和任务换成你的实际需求：

> 请阅读 `~/.codex/skills/codex-peer/SKILL.md`，使用其中的 codex-peer 工具启动一个新的 Codex 会话，让 Codex 审查 `/path/to/project` 中的代码。等待它完成后，结合你的发现给我一份汇总。

外部代理会调用 `codex-peer` 启动 Codex、等待结果并读取回复。你也可以继续对外部代理说“请让 Codex 检查测试”或“问 Codex 为什么建议这样修改”，让它向同一个会话追问。

如果你更希望自己在终端操作，也可以直接执行：

```bash
codex-peer spawn --name reviewer '审查这个项目并总结发现' --cwd /path/to/project --wait 1800
codex-peer ask --thread reviewer '请进一步检查测试覆盖情况' --wait 1800
```

`reviewer` 是本地会话别名，可以自行修改。如果等待超时，会话可能仍在运行；继续等待即可，不要重新发送同一任务：

```bash
codex-peer wait --thread reviewer --wait 60
```

## 连接已有 Codex 会话

如果你希望外部代理联系 VS Code 中已经打开的 Codex 线程，可以告诉它：

> 请阅读 `~/.codex/skills/codex-peer/SKILL.md`，找到标题包含“项目审查”的已有 Codex 线程，询问它目前的进度，并把回复告诉我。

也可以在终端中查找和选择线程：

```bash
codex-peer threads --query '项目审查'
codex-peer use <线程UUID>
codex-peer ask '请总结当前进度' --wait 1800
```

默认建议为新任务启动新会话；需要接续现有上下文时再选择已有线程。线程数据必须能从运行工具的环境访问。

## 常用操作

| 目的 | 命令 |
| --- | --- |
| 查看已命名的 worker | `codex-peer fleet` |
| 查看某个会话的状态 | `codex-peer status --thread reviewer` |
| 读取新消息和工具事件 | `codex-peer read --thread reviewer` |
| 等待任务完成 | `codex-peer wait --thread reviewer --wait 60` |
| 检查线程连接 | `codex-peer doctor --thread reviewer` |
| 查看所有命令和选项 | `codex-peer --help` |

命令输出为单行 JSON，供外部代理解析。完整工作流程、多个 worker 的用法及返回状态说明见 [SKILL.md](SKILL.md)。

## 测试

在仓库目录中运行：

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```
