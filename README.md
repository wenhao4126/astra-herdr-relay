> 源码目录：**v0.5.0-fixed-20260923，2026-09-27 可靠性优化**。同层的 `.tar.zst` 是旧发行包，尚未重新打包。

# Astra Herdr Relay v0.5.0 修复版

本发行包支持首次安装和从已有 v0.5.0 增量升级。升级保留现有配置及完整运行状态，包括密钥、Agent 注册表、任务、Review、事件和锁。安装器不会删除这些目录，也不会修改 Herdr 或 Codex 本体。

代码统一读取 Herdr 返回的 `agent_status`，并兼容旧的 `status`、`state` 字段。当前目录还修复了注册表损坏时的静默覆盖、暂时查询失败导致的误删、跨任务 Sensor 缓存和 Hook 映射并发问题。`sync` 与守护进程统一兼容 Herdr 的两种 Agent 列表返回形状，并核对已保存的 tab/pane 身份；异常列表结构不会被当作空列表，同名新实例不会结算旧任务或污染旧任务的 Sensor 状态。创建失败且没有标签的记录也可以通过显式释放清理。异步任务增加了由 Astra 显式调用的审查入口。Jev 的 PASS、REWORK、DEEP_REVIEW、BLOCKED 判定门槛保持不变。

## Frozen architecture

- **Astra**: global orchestrator. Decides which role is needed, the task, and when an agent should be released.
- **Relay**: deterministic execution/state bridge. Creates a Herdr **tab**, starts the requested agent, delegates work, monitors state, invokes Jev, and executes explicit release.
- **Herdr**: runtime/UI. One child agent = one tab, so the user can click a tab and inspect that agent full-screen.
- **Codex**: worker/fixer/reviewer/tester/auditor execution agent.
- **Jev**: Semantic Sensor during work + Reviewer after work. Jev never directly creates/stops Herdr agents and never becomes the global orchestrator.

## Agent lifecycle

Relay executes deterministic transitions only:

`CREATING -> IDLE -> BUSY -> IDLE`

Astra may explicitly request release:

`IDLE -> STOPPING -> STOPPED`

Relay has **no idle TTL and no autonomous cleanup**. If Astra forgets to release an agent, the user can close the Herdr tab manually; `astra-relay sync` then marks the registry entry `REMOVED`.

## Herdr tab creation

v0.5.0 uses the current Herdr CLI surface:

1. `herdr tab create ... --label <agent> --no-focus`
2. parse `.result.tab.tab_id` and `.result.root_pane.pane_id`
3. `herdr agent start <name> --kind codex --pane <pane_id>`；指定模型时追加 `-- --model <model>`

This gives each child agent its own full-screen-switchable Herdr tab.
启动代理可能改变 Herdr 焦点；Relay 会记录启动前焦点，只有焦点仍在新建标签时才恢复，若用户已主动切换则不干预。

Astra 可显式调用 `astra-relay start-review-pair --cwd /path/to/repo`，一次创建固定名称的 Codex worker（`codex-worker-luna`，模型由用户自行选择）和 OpenCode 日常 reviewer（`opencode-reviewer`）。Relay 不向 OpenCode 传递模型参数；用户在 OpenCode 中自行选择模型。注册表记录角色、kind、工作目录和空模型元数据。若名称已占用或任一创建失败，命令以非零状态返回已创建项与失败项，不会删除成功创建的代理；修复冲突后由 Astra 再决定是否调用。

## Jev

Sensor states:
`PROGRESSING STUCK BLOCKED OFF_TASK WAITING COMPLETED UNKNOWN`

Deterministic runtime mappings avoid unnecessary API calls:
- Herdr `done` -> `COMPLETED`
- Herdr `blocked` -> `BLOCKED`

Post-work Reviewer routes:
`PASS REWORK DEEP_REVIEW BLOCKED`

Ordinary REWORK is bounded by `ASTRA_RELAY_MAX_REWORKS=2`. `DEEP_REVIEW` remains a boundary state; Luna/Astra deep-stage automation is intentionally not implemented in this release.

Probability validation rejects booleans (`true`/`false`) even though Python normally treats bool as int. Explicit implementation-defect/missing-requirement signals can force REWORK.

### 可选的最终审查门

将 `ASTRA_RELAY_FINAL_REVIEW_ENABLED=1` 写入 Relay 配置后，Astra 才会在显式调用 `finalize <task_id>` 时进入最终审查门。运行中的 Jev Sensor 和 v0.5.0 旧 Reviewer 均保持原行为。最终审查先取得独立 OCR 的 `ReviewResult`，再将其作为证据交给 Jev 判断。OCR 不启动 Agent、不修改代码，也不接收 Claude 的结论。

日常代码审查由 Astra 显式调用 `astra-relay start-review-pair --cwd <项目路径>`：命令复用 registry 中活跃的固定 Codex worker 与 OpenCode reviewer，只为缺失或已停止的代理创建标签。OpenCode 模型由用户自行设置，Relay 不传 `--model`；返回的 `reused` 将模型状态标记为 `unverified`。任一代理未进入 `IDLE` 都不会报告 pair 完成。单独启动该 reviewer 时使用：

```bash
astra-agent-spawn --role reviewer --kind opencode --cwd /path/to/repo --name opencode-reviewer
astra-relay dispatch-opaque opencode-reviewer --prompt-file /tmp/opencode-review-task.txt
astra-agent-status opencode-reviewer
herdr agent read opencode-reviewer --source recent-unwrapped --lines 400
```

`dispatch-opaque` 返回真实任务编号，Astra 应在 reviewer 停止后用该编号收集审查结果，核对当前任务标记和实际发现，再整理成 `development_review` 的 `ReviewResult`。reviewer 发现阻塞问题时，Astra 将具体发现交回 Codex 修复，修复后重新取得独立审查结果；Relay 不会自动派发修复。多个 Herdr 会话并存时，在配置中设置 `ASTRA_HERDR_SESSION` 指向用户正在看的会话；创建标签使用 `--no-focus`，不会抢用户当前输入焦点。reviewer 首次启动若返回 `BLOCKED` 或 `agent_not_ready`，标签会保留，需用户在该标签完成首次授权或初始化，再由 Astra 确认其已就绪。

最终审查前，Astra 将开发 Review、测试、构建、检查的四份 PASS 结果写入 JSON 文件，执行 `astra-relay record-final-evidence <task_id> --json-file <file>`。Review 字段优先使用 `development_review`；旧调用仍可使用 `claude_review`：

```json
{"development_review":{"status":"PASS"},"test_result":{"status":"PASS"},"build_result":{"status":"PASS"},"lint_result":{"status":"PASS"}}
```

任务进入 `READY_FOR_FINAL` 后，Astra 对已停止的工作任务调用 `finalize <task_id>`。Relay 只校验这四份证据，不代替项目运行测试或构建。Jev 根据 OCR 发现和任务证据输出明确的 `ReviewOutcome`：`PASS`、`ACCEPT_WITH_NOTES`、`REWORK`、`DEEP_REVIEW` 或 `BLOCKED`。`ACCEPT_WITH_NOTES` 表示 Jev 接受工作且保留非阻塞发现；有 OCR finding 本身不会自动触发该结果或返工。`PASS` 和 `ACCEPT_WITH_NOTES` 对应任务完成，后者的 notes 保存在 `review_outcome`。阻塞发现由 Jev 判断是否需要返工，Astra 决定后续安排。

OCR 官方 JSON 的 `complete` 与旧版 `success` 是完整状态；`partial`、`completed_with_warnings`、`completed_with_errors` 均不能 PASS，`failed`、`skipped`、超时、非零退出及 JSON 错误均保守转为 `DEEP_REVIEW`。不保存 OCR 标准输出、标准错误或原始 JSON。OCR 要求待审查路径就是独立 Git 根；本目录位于上层 Obsidian Git 仓库内，在此运行真实 OCR 会被拒绝。`ASTRA_RELAY_OCR_TIMEOUT_SECONDS` 和 `ASTRA_RELAY_FINAL_REVIEW_MAX_REWORKS` 分别限制调用时长和最终审查次数。关闭开关时仍走原 v0.5.0 Reviewer 路径。

## 安装与升级

```bash
cd astra-herdr-relay-v0.5.0-fixed-20260923
./install.sh --check
./install.sh
./rollback.sh list-backups
./rollback.sh --backup-id <backup-id> --check
./rollback.sh --backup-id <backup-id>
./uninstall.sh --check
./uninstall.sh
```

上面的命令使用当前源码目录；不要从同层旧 `.tar.zst` 解压后安装本次优化。安装会修改用户目录中的程序和服务，执行前先确认 Relay 没有活动任务。当前源码的修改没有自动安装到本机。

`--check` 只运行预检，不修改目标目录。`uninstall.sh` 与 `rollback.sh` 也支持 `--root <目录>` 和 `--check`；默认 root 是当前用户的 HOME。`rollback.sh` 必须给出 `--backup-id`，有效 ID 先由 `rollback.sh list-backups` 列出。只有当前安装账本对应的回滚点适用于当前版本链；工具不会猜测备份或接受任意路径。

每次成功安装都会在 `~/.local/state/astra-herdr-relay/install-ledgers/` 保存私有安装账本，并在 `install-backups/<backup-id>/` 保存受管程序原快照。卸载只在程序 SHA-256 仍与账本一致时删除本 Relay 安装的程序与 unit；用户修改过的受管文件会让操作在停服务前拒绝。卸载移除账本记录的 Relay Hook 规则和 AGENTS 标记块，保留其他 Hook、用户文档、配置、密钥、运行状态、任务锁、事件、registry 和备份。当前版本不提供 purge。

没有新账本的旧安装需先由本发行包成功升级一次，之后再执行卸载或回滚；账本不能确认所有权时会拒绝操作。升级检测到旧 Relay user unit 时会将其标为兼容升级，卸载会移除 Relay 程序，回滚仍可恢复升级前快照。

回滚只恢复所选安装的受管程序快照与安装前服务状态，并只替换 Relay 自有 Hook/AGENTS 片段。配置和密钥保留当前值；registry、任务、锁、事件及其他推进中的状态绝不从旧备份覆盖。三类变更都会检查活动任务和 Relay 进程，在维护门禁后排空；未知状态或活动任务会拒绝操作。失败时恢复本次操作前的受管文件与服务状态，并在私有 state 中记录错误。卸载不关闭 Herdr 标签或用户代理。

程序、账本和备份均要求普通文件及 root 内受控路径；符号链接、越界路径、哈希不符和不适用版本链都会拒绝。systemd 命令只作用于 `astra-herdr-relay.service` 的 user unit，不执行全局 reset。部署失败时安装器继续使用原有程序/service 失败恢复逻辑，不回滚运行状态目录。

发行包源码变更后，用 `python3 scripts/update_manifest.py` 按路径排序并实际重算 `MANIFEST.sha256`；安装前会校验清单内所有普通文件。

安装预检允许保留已结算的 BUSY 记录：只有任务编号、任务 JSON、代理归属、`HUMAN_REVIEW`/`BLOCKED` 结算状态和对应锁全部严格匹配时才放行，并保留 registry、task 和 lock 原样；运行中、正在转换、缺失或归属不匹配的记录仍会阻止安装。`install.sh` 在 `XDG_RUNTIME_DIR` 或 `DBUS_SESSION_BUS_ADDRESS` 缺失时，仅当 `/run/user/$(id -u)/bus` 是 socket 才推导 user bus 环境；显式设置的值始终优先。

本次代码验证使用临时 root、fake systemctl 与 fake proc；不会调用真实安装。真实安装以及 Herdr → Codex → Jev E2E 仍属于单独部署阶段。Jev 合法返回 `DEEP_REVIEW` 时不会为了得到 `DONE` 而降低门槛。

## Commands

代理启动后，由用户在 Herdr 中选择或修改模型。

```bash
astra-agent-spawn --role fixer --kind codex --cwd ~/project
astra-agent-spawn --role reviewer --cwd ~/project --parent-task-id relay-parent-1
astra-delegate codex-fixer-01 "Fix the installer bug and verify it."
astra-relay semantic-status codex-fixer-01
astra-agent-status codex-fixer-01
astra-agent-list
astra-agent-release codex-fixer-01
astra-relay sync
```

异步任务完成后，Astra 可以显式审查对应任务：

```bash
astra-relay dispatch-opaque codex-fixer-01 --prompt-file /tmp/relay-task.txt
astra-relay finalize relay-任务编号
```

`dispatch-opaque` 返回实际 `task_id`，把它传给 `finalize`。`finalize` 只审查能确认属于该任务且已停止的证据；运行中、缺少证据或实例身份无法确认时不会返回 `DONE`。`REWORK` 交回 Astra 决定，Relay 不会因这个结果自动派发新任务。守护进程只观察，不自动调用 `finalize`。

派发时 Relay 会在原始任务前加入一段固定协议说明，原始任务正文保持不变。Agent 完成后，最终回复的第一行应写 `[RELAY FINISHED <task_id>]`，下一行起写实际结果与验证证据，其中 `<task_id>` 要换成派发返回的编号。终端只有派发回显、旧任务文本或空的完成标记时，`finalize` 会交给人工核实；没有当前任务完成标记的旧任务记录也会保守处理。

## Important safety boundaries

- Relay never invents roles or decides that a new reviewer/fixer is needed. Astra does.
- Relay never autonomously kills idle agents.
- Per-agent exclusive lock prevents two tasks from being delegated concurrently to the same agent.
- Missing task marker never falls back to reviewing whole terminal history; it routes to `HUMAN_REVIEW`.
- Command errors route to `HUMAN_REVIEW` rather than blindly retrying.
- Secrets are never bundled in this archive.


旧版 `install-clean.sh` 是破坏性清理脚本，不属于此修复发行包；请使用包内 `install.sh`。

## Astra native-subagent redirect

This build uses Codex `PreToolUse` on `spawn_agent` as the hard guardrail. The hook receives
`tool_name`, `tool_use_id`, and `tool_input` before the tool executes, so Astra's original
child-agent message can be captured after Astra has decided what to delegate but before the
hidden internal child is created.

Hook 也明确拒绝匹配到的原生 `Agent` 工具调用，避免它绕过 Relay 创建隐藏代理。代理物理名称使用顶层 Hook 事件中的会话、轮次和工具调用身份生成片段，再加随机后缀；会话内别名仍指向对应的 Herdr 可见标签。

Important: current Codex hooks can block or rewrite the input of the SAME tool, but cannot
replace `spawn_agent`'s native result with an external backend result. Therefore the hook
performs the Relay→Herdr spawn/dispatch itself, then denies the native spawn and returns
`RELAY_REDIRECT_OK` to Astra. This is deliberate.
