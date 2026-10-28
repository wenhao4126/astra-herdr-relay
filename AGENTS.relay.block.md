<!-- ASTRA-HERDR-RELAY-V050-BEGIN -->
## Astra Herdr Relay v0.5.0

### Fixed role boundary
- Astra is the global orchestrator/brain. Astra decides what work is needed, which execution role is needed, the task, and when an agent should be released.
- Relay is deterministic Observer + Bridge + State Machine + controlled execution layer.
- Herdr is the agent/tab runtime.
- Codex is the execution worker.
- Jev is Semantic Sensor during work and Reviewer after work. Jev is NOT the global orchestrator and never directly controls Herdr.

### 子代理任务包
- 任务必须先写成 `schema_version=1` 的 JSON 任务包，`spawn_agent.message` 传入该 JSON 的完整字符串；普通自由文本会返回 `TASK_PACKAGE_REQUIRED`。
- 任务包固定包含 `schema_version`、`task_id`、`revision`、`goal`、`scope.allowed`、`scope.forbidden`、`behavior.inputs/outputs/errors/compatibility`、`acceptance`、`evidence_policy`、`docs`、`working_directory`。工作目录必须等于 Hook 当前真实目录；需要其他目录时，通过 CLI `dispatch-task --task-file` 明确指定。
- 对同一份任务包先运行 `astra-relay validate-task --task-file /tmp/relay-task.json`、`astra-relay evaluate-task --task-file /tmp/relay-task.json`，再运行 `astra-relay validate-task --task-file /tmp/relay-task.json --require-approved`。Hook 的 `message` 必须与已批准任务包完全一致。
- 显式创建并派发时，使用 `astra-relay dispatch-task --task-file /tmp/relay-task.json --role fixer --session "$ASTRA_HERDR_SESSION" --request-id req-task-example`。五维 Score 权重为 20/20/25/15/20、总分至少 95；五项 Choice 阻断都必须是 `absent` 且置信度至少 0.95。`docs` 只绑定任务包中声明的来源、版本和 SHA-256；此版本未实现完整 Context7 拉取、校验和资料快照协议。
- Jev 评估通过后，原生 Hook 调用 Relay 创建并派发可见 Herdr 代理，随后仍拒绝原生 `spawn_agent`。收到 `RELAY_REDIRECT_OK` 后不得重试原生工具。
- 角色使用 `agent_type` 中的 `worker|fixer|reviewer|tester|auditor`，或在 `task_name` 前缀角色。模型参数读取 `model`，推理参数读取 `reasoning_effort`。

任务包示例（将工作目录替换为目标真实目录）：

```json
{
  "schema_version": 1,
  "task_id": "relay-example",
  "revision": 1,
  "goal": "实现约定变更并验证结果",
  "scope": {"allowed": ["src/", "tests/"], "forbidden": ["真实配置"]},
  "behavior": {
    "inputs": ["本任务包"],
    "outputs": ["实现结果和验证证据"],
    "errors": ["不符合约定时明确失败"],
    "compatibility": ["遵守现有接口约定"]
  },
  "acceptance": ["指定验收项通过"],
  "evidence_policy": ["记录执行命令及结果"],
  "docs": [],
  "working_directory": "/home/wen/projects/astra-herdr-relay-next"
}
```

当 Astra 需要执行代理时，先通过上述 Jev 门禁批准完整任务包，再走 Relay 重定向；不得直接创建隐藏的 Codex 内部代理。Relay 在专用 Herdr 标签中启动代理并派发同一任务包。一个子代理对应一个可在 Herdr 检查的标签。

Execution workers must not recursively create hidden execution subagents. If more help is required, surface that need to Astra so Astra can request another Relay-managed Herdr agent.

### Lifecycle
- Relay may CREATE/START/DELEGATE/MONITOR/SEND/WAIT/RELEASE only as deterministic execution of an explicit orchestration request or fixed Relay policy.
- Astra decides when an agent is no longer needed and requests RELEASE_AGENT.
- Relay then performs `IDLE -> STOPPING -> STOPPED` and closes the corresponding Herdr tab.
- Relay MUST NOT autonomously release idle agents, MUST NOT use idle TTL cleanup, and MUST NOT decide that an agent should be killed.
- If Astra forgets to release, the user may manually close/release the Herdr agent/tab. Relay only synchronizes the missing agent as REMOVED; it must not recreate it automatically.

### Jev Semantic Sensor
During work, Relay observes Herdr and may invoke Jev to classify:
`PROGRESSING`, `STUCK`, `BLOCKED`, `OFF_TASK`, `WAITING`, `COMPLETED`, `UNKNOWN`.

Deterministic runtime facts should stay deterministic and avoid unnecessary Jev calls, including Herdr `done -> COMPLETED` and `blocked -> BLOCKED`.

Jev Sensor informs Relay/Astra what is happening. Jev never invents shell commands, creates agents, releases agents, changes goals, or becomes the orchestrator.

### Jev post-work Reviewer
After a delegated task has actually settled, Relay sends only task-scoped evidence to Jev Reviewer.
Review outcomes: `PASS`, `REWORK`, `DEEP_REVIEW`, `BLOCKED`.

Ordinary REWORK is capped at 2 attempts. Strong DEEP_REVIEW bypasses ordinary rework. BLOCKED is separate.
Jev/API failure fails closed to DEEP_REVIEW.

`DEEP_REVIEW` is intentionally a boundary state in v0.5.0. Do NOT automatically add Luna/Astra deep-stage repair from this rule block.

### Isolation and safety
- Never review whole terminal history when the current `[RELAY TASK <task_id>]` marker cannot be found.
- Never final-review a task that is still running or whose output has not settled.
- Only one active delegated task may hold a given Relay-managed agent lock.
- Passive daemon observation during an active delegate is SENSOR-ONLY: never duplicate dispatch, review, rework, or forwarding.
- Preserve the distinction: Agent runtime COMPLETED does not by itself mean the parent orchestration task is globally DONE.
<!-- ASTRA-HERDR-RELAY-V050-END -->
