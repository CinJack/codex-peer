---
name: codex-peer
description: Let Gemini, Antigravity, or another local agent spawn a fresh fleet of Codex workers or talk to a Codex VS Code thread and inspect its progress through CLI commands. Use for multi-agent Codex fleets, delegating parallel tasks to Codex, live follow-ups, thread history, or waiting for Codex replies in a shared local environment.
---

# Codex Peer

用 `codex-peer` 命令组建全新的 Codex 进程阵列（或与现有 VS Code 面板中的 Codex 线程通信），所有命令输出单行 JSON。若命令不可用，改用 `python3 <本 skill 目录>/scripts/codex_monitor.py`。

## 默认原则：自己组建全新 Codex 进程阵列，不要复用老旧会话

每次接手新任务时，**默认用 `spawn` 拉起全新的 Codex worker（或按模块并行拉起多个 worker 组成阵列）**，避免旧会话的残留上下文污染或干扰用户正在查看的历史面板。只有用户明确要求介入某个已有面板会话时，才用 `threads` / `use` 连接旧会话。

### 1. 单个全新 Codex 进程（拉起并等待完成）
```bash
codex-peer spawn --name worker1 '你的任务说明' --wait 1800
```
- 自动创建全新 Codex 会话、注册别名 `worker1` 并设为当前默认线程。
- 默认 `--until done`：本轮完成才返回，`reply.event.text` 即最终结果。
- 后续追问可直接：`codex-peer ask --thread worker1 '补充要求' --wait 1800`（即使该会话未在 VS Code 面板中打开，也会自动通过后台 `exec resume` 无缝续跑）。

### 2. 多进程并行阵列（Fleet 模式）
需要并行处理多个子任务时，省略 `--wait`（默认 `--wait 0` 立即返回），一次拉起多个命名 worker：
```bash
codex-peer spawn --name math_audit '检查公式推导...'
codex-peer spawn --name code_bench '运行基准测试...' --cwd /workspaces/bayes_env
codex-peer fleet                                  # 一键巡检整个阵列的状态与最新回复
codex-peer wait --thread math_audit --wait 1800   # 等待指定 worker 完成
codex-peer wait --thread code_bench --wait 1800
```
- 展开新任务前可用 `codex-peer fleet --clean` 清空旧阵列别名表（不会删除底层历史日志）。

### 3. 省算力铁律
- **支持后台命令的环境（如 Antigravity `run_command`）**：带 `--wait 1800` 的命令进入后台后，**立即结束本轮等待系统完成通知**，严禁循环轮询 `status` / `read` / `fleet`。
- **禁止超时后重发**：`timeout` 表示仍在运行，按返回中的 `next` 执行 `codex-peer wait --thread 名称 --wait 60`，切勿重新 `ask` / `send`。
- **多行长指令**：用标准输入传入 `codex-peer spawn --name w1 - --wait 1800 <<'EOF' ... EOF`。

## 返回结果速查

| 字段 | 含义 / 下一步 |
|---|---|
| `delivery: accepted` | 已启动或实时送达（`operation` 为 `spawn` 新建、`resume` 后台续跑、`start`/`steer` 面板 IPC） |
| `delivery: queued` | 目标线程正在运行且未在 VS Code 面板打开，消息已入 Codex 队列 |
| `delivery: unconfirmed` | 投递结果未知。先 `codex-peer read` 核对是否出现 `[Gemini:ID]`，**不要直接重发** |
| `reply.state: observed` | 已拿到结果；`event.kind` 为 `task_complete` 表示完成，`turn_aborted` 表示中断 |
| `reply.state: timeout` | 仍在运行；`progress` 为最近发言或工具操作，按 `next` 继续 `wait` |
| `reply.state: idle` | Codex 已空闲且无排队消息，无需继续等待 |

## 命令一览

| 命令 | 用途 |
|---|---|
| `spawn [--name 别名] '任务' [--wait 秒]` | 拉起全新 Codex 进程（`--cwd` 指定目录，`--model` 指定模型） |
| `fleet [--clean]` | 查看所有命名 worker 的状态与最新回复；`--clean` 清空阵列注册表 |
| `ask [--thread 别名/UUID] '消息' --wait 1800` | 向指定 worker 发送后续指令并等待本轮完成（自动选择面板 IPC 或后台 `resume`） |
| `send [--thread 别名/UUID] '消息'` | 只发送不等待（fire-and-forget） |
| `wait [--thread 别名/UUID] [--after ID]` | 继续等待指定 worker 完成 |
| `status [--thread 别名/UUID]` | 查看单个线程状态、最后发言、最后工具、排队消息（`--full` 附日志路径） |
| `read [--thread 别名/UUID] --wait 20` | 增量读取新增对话与工具操作；`--reader 名称` 区分不同读取者游标 |
| `history --kind messages --limit 5` | 查看最近对话；`--kind tools --query error` 搜索工具记录 |
| `event AT` | 按事件的 `at` 字节偏移展开单条全文 |
| `interrupt [--thread 别名/UUID]` | 中断面板中正在运行的轮次 |
| `threads [--query 关键词]` / `use 别名或UUID` | 列出或切换已有会话（仅在需介入已有会话时使用） |
| `doctor [--thread 别名/UUID]` | 检查连接；`live:true` 表示该会话已在 VS Code 面板打开 |
