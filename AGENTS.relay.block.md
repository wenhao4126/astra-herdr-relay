<!-- ASTRA-HERDR-RELAY-V050-BEGIN -->
## Astra Herdr Relay v0.5.0

### Fixed role boundary
- Astra is the global orchestrator/brain. Astra decides what work is needed, which execution role is needed, the task, and when an agent should be released.
- Relay is deterministic Observer + Bridge + State Machine + controlled execution layer.
- Herdr is the agent/tab runtime.
- Codex is the execution worker.
- Jev is Semantic Sensor during work and Reviewer after work. Jev is NOT the global orchestrator and never directly controls Herdr.

### 子代理任务包
- 新任务使用 `schema_version=2` 完整 JSON 包；v1 仅兼容旧式任务校验/派发，不得作为新版最终证据凭证。同步 `delegate` 仅兼容 v1，收到 v2 返回 `V2_ASYNC_REQUIRED`，必须改用 `dispatch-task` 或 `dispatch-opaque`，不降级任务版本。Hook 原生 `spawn_agent` 只从 `message` 解析 v1/v2 任务包 JSON；普通文本明确返回 `TASK_PACKAGE_REQUIRED`。Relay 重定向成功后仍拒绝原生工具，不能重试隐藏的原生派发。
- 任务包含 `task_id`、`revision`、`goal`、`scope.allowed/forbidden`、`behavior.inputs/outputs/errors/compatibility`、`acceptance`、`evidence_policy`、`docs`、`required_doc_ids`、`working_directory`。Hook 要求 `working_directory` 等于 Hook 当前真实目录；需要跨目录时必须使用 `dispatch-task --task-file` 明确指定。
- Context7 由 Agent 显式查询，`record-doc --json-file <file>` 计算资料摘要并登记来源、摘录、版本和状态。记录是 Agent 声明，Relay 核对字段、状态、项目版本与内容摘要，不证明来源语义真实。任务列在 `required_doc_ids` 的资料必须存在且匹配；无外部资料的纯本地任务须在 `evidence_policy.no_external_docs_reason` 写原因。参考 `examples/context7-typesafe-score-choice.json` 与 `examples/task-v2-context7.json`。
- 对同一任务文件依次运行 `validate-task`、`evaluate-task`、`validate-task --require-approved`，再使用完全相同的包派发。示例资料先运行 `astra-relay record-doc --json-file examples/context7-typesafe-score-choice.json`，将输出摘要填入任务文件后再评估。
- 显式创建并派发时使用 `astra-relay dispatch-task --task-file /tmp/relay-task.json --role fixer --session "$ASTRA_HERDR_SESSION" --request-id req-task-example`。Jev 门禁的 Score 权重为 20/20/25/15/20、总分至少 95；五项 Choice 阻断都必须是 `absent` 且置信度至少 0.95。Jev 仅接收任务引用的资料摘录，不调用 Context7。
- 任务完成后由主代理维护显式交付文件清单，运行 `capture-artifact --task-file ... --files-file ... --runtime-task-id <dispatch task_id>`。清单应包含交付的未跟踪文件；新增交付文件要更新清单并重新捕获。摘要仅覆盖明确列出的文件，不证明整个仓库正确。
- 用 `record-evidence --json-file <file>` 登记外部测试/构建/lint/Review/OCR 报告。Relay 校验报告路径与摘要，不执行报告文件、不证明报告内容正确。测试、构建、lint 必须为 `passed`；Review 必须来自不同作者身份和独立 session。`validate-stage --stage review` 不要求先有 Review 结果。
- final 基础门禁始终要求当前快照、任务策略中的证据、独立 Review 和 Relay 本地 Jev 结果。v2 OCR 只在任务策略明确列出 `ocr_review` 时必需；旧全局最终审查开关仅影响 v1 兼容路径。final Jev 摘要还绑定快照完整作者实例身份，作者/session/tab/pane/可得 Herdr agent-session 变化使旧判定失效。运行 `evaluate-final --task-file ...` 后再 `validate-stage --stage final`；Jev PASS/ACCEPT_WITH_NOTES 交给主代理决策，绝不自动宣称任务整体完成。任务/制品/证据摘要变化会使旧 Jev 结果失效。
- `record-final-evidence` 对 v2 只存兼容 legacy 数据，不可放行新 final 阶段。当前实现补齐的是本地资料登记、快照、报告完整性和阶段协议；不代表完整 Context7 语义核验或完整 Relay 架构已经完成。
- Jev 评估通过后，原生 Hook 调用 Relay 创建并派发可见 Herdr 代理，随后仍拒绝原生 `spawn_agent`。收到 `RELAY_REDIRECT_OK` 后不得重试原生工具。
- 角色使用 `agent_type` 中的 `worker|fixer|reviewer|tester|auditor`，或在 `task_name` 前缀角色。模型参数读取 `model`，推理参数读取 `reasoning_effort`。

任务包示例使用 `examples/task-v2-context7.json`。Hook 场景把完整 JSON 放入 `spawn_agent.message`；CLI 可用 `/tmp/relay-task.json` 跨目录派发。三个阶段的具体命令与报告字段见仓库 README「结构化任务门禁」和「三阶段快照与证据」。

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
