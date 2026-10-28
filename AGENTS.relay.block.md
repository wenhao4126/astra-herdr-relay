<!-- ASTRA-HERDR-RELAY-V050-BEGIN -->
## Astra Herdr Relay v0.5.0

### Fixed role boundary
- Astra is the global orchestrator/brain. Astra decides what work is needed, which execution role is needed, the task, and when an agent should be released.
- Relay is deterministic Observer + Bridge + State Machine + controlled execution layer.
- Herdr is the agent/tab runtime.
- Codex is the execution worker.
- Jev is Semantic Sensor during work and Reviewer after work. Jev is NOT the global orchestrator and never directly controls Herdr.

### Child-agent rule
- Astra may decide to use Codex native `spawn_agent`; the installed PreToolUse hook is the hard redirect boundary.
- When Astra has already chosen a child and supplies `task_name`/`agent_type` plus `message`, the hook captures that exact `message`, creates the visible Relay/Herdr agent, forwards the message as an opaque string, and denies the native internal spawn.
- After `RELAY_REDIRECT_OK`, do NOT retry native `spawn_agent`; continue using the Relay-managed Herdr agent named in the hook result.
- For best role preservation, set `agent_type` to one of `worker|fixer|reviewer|tester|auditor`, or prefix `task_name` with that role.

When Astra needs an execution child role (`worker`, `fixer`, `reviewer`, `tester`, `auditor`):
1. Do NOT create a hidden Codex/internal execution subagent.
2. Request/execute creation through Relay.
3. Relay creates a dedicated Herdr tab, starts the requested Codex agent in that tab, registers it, then delegates the task.
4. One child agent = one Herdr tab. The tab remains inspectable in Herdr.

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
