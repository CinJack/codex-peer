# Codex Peer

Codex Peer 是一个本地 Codex 技能和命令行工具，用于启动新的 Codex worker、向已有线程发送任务，以及查看进度和回复。命令以单行 JSON 输出，便于其他代理或脚本处理。

## 环境要求

- Python 3（脚本仅使用标准库）
- 已安装并配置的 [Codex CLI](https://github.com/openai/codex)
- 如需连接 VS Code 中的现有线程，需要可访问对应的本地 Codex 会话和扩展环境

## 安装

将仓库克隆到 Codex 技能目录：

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/CinJack/codex-peer.git ~/.codex/skills/codex-peer
```

将命令所在目录加入 `PATH`：

```bash
export PATH="$HOME/.codex/skills/codex-peer/scripts:$PATH"
```

可将上述 `export` 命令加入 shell 配置文件，使其在新终端中生效。也可以直接运行 `python3 ~/.codex/skills/codex-peer/scripts/codex_monitor.py`，无需修改 `PATH`。

## 快速开始

启动一个新的 worker 并等待任务完成：

```bash
codex-peer spawn --name worker1 '检查当前项目并总结发现' --cwd "$PWD" --wait 1800
```

向该 worker 发送后续任务：

```bash
codex-peer ask --thread worker1 '继续检查测试覆盖情况' --wait 1800
```

同时启动多个 worker，并查看状态：

```bash
codex-peer spawn --name review '检查代码质量' --cwd "$PWD"
codex-peer spawn --name tests '运行项目测试' --cwd "$PWD"
codex-peer fleet
codex-peer wait --thread review --wait 1800
codex-peer wait --thread tests --wait 1800
```

如果等待超时，任务可能仍在运行；使用 `codex-peer wait --thread worker1 --wait 60` 继续等待，无需重新发送任务。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `spawn` | 启动新的 Codex worker |
| `fleet` | 查看已命名 worker 的状态 |
| `ask` / `send` | 向线程发送消息，分别支持等待或立即返回 |
| `wait` | 等待线程完成或产生回复 |
| `status` / `read` | 查看线程状态或增量读取事件 |
| `threads` / `use` | 查找或选择已有线程 |
| `history` / `event` | 查询历史记录或展开事件 |
| `interrupt` / `doctor` | 中断线程或检查连接 |

运行 `codex-peer --help` 查看完整选项，或阅读 [SKILL.md](SKILL.md) 了解代理工作流程和输出字段。

## 测试

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```
