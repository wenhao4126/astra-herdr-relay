# 2026-09-27 可靠性优化（源码目录）

- 权威确认启动宽限期后的代理不存在时，未结算任务转交 `HUMAN_REVIEW`，仅在任务实例与锁归属匹配时清理任务锁，并清除 REMOVED 记录中的当前任务与操作令牌。
- Final Gate 接受首选 `development_review` 并兼容旧 `claude_review`；固定 pair 复用时校验 agent kind。

- 固定日常 reviewer 改为 `opencode-reviewer`（`kind=opencode`），不指定模型，由用户在 OpenCode 设置；Codex worker 保持不变。

- 固定日常 reviewer 改为 `claude-review-deepseek`，模型为 `deepseek-flash[1m]`；Codex worker 配置保持不变。

- `start-review-pair` 复用 registry 中活跃的固定代理；模型元数据缺失时标为 `unverified`，未就绪代理不计为 pair 完成。
- OCR 归一化接受官方 `complete` 和旧版 `success`，部分状态不能作为 PASS。

- 新增 Astra 显式调用的 `start-review-pair`，按固定模型创建 Codex worker 与 Claude reviewer，记录模型和工作目录；部分失败返回已创建项，不清理代理。
- 启动代理后按需恢复启动前 Herdr 焦点；用户已切换标签时不夺回焦点。

- 最终审查门新增 Jev 判定的 `ACCEPT_WITH_NOTES` 与明确的 `review_outcome`；阻塞问题仍按评分进入返工，OCR 异常或不完整结果保守处理。旧版 Reviewer 和 Sensor 不变。
- README 补充 Astra 在 Herdr 显式创建 Claude reviewer、收集结果、提交四类完成证据及返工交接流程；注明会话、前台标签与首次启动受阻处理。
- 注册表损坏时明确报错并保留原文件；Herdr 临时查询失败不再当作 Agent 删除证据。
- Sensor 缓存绑定任务和实例，守护进程复用 Agent 列表快照并隔离单个 Agent 的观察错误。
- Hook 映射采用进程锁和原子写入，按会话保存别名，并协调创建与派发的超时预算。
- 异步任务新增 Astra 显式调用的 `finalize <task_id>` 审查入口；Agent 回复须带当前任务完成标记和实际结果，旧任务文本或单纯派发回显不进入审查。审查结果持久化，`REWORK` 不自动派发。
- 显式释放可安全中断正在审查的任务：清除操作令牌，将该任务转为 `HUMAN_REVIEW`，只释放属于它的锁。
- `sync` 与守护进程统一解析 Herdr Agent 列表，拒绝异常响应结构；只有名称及已记录的 tab/pane 身份一致时才结算任务或调用 Sensor。同名新实例不会释放旧任务锁。
- 创建失败且没有 Herdr 标签的代理可显式释放；Hook 拒绝原生 `Agent` 调用，并从顶层事件读取会话身份以生成代理名称。
- 同层旧 `.tar.zst` 尚未更新；本次源码未安装到用户运行环境。

# v0.5.0-fixed-20260923

- 统一读取 Herdr 的 `agent_status`，并兼容旧的 `status`、`state` 字段；修复 Sensor、同步委派和异步结算误判 `unknown` 的问题。
- 新发行包使用安全的 `install.sh`，支持首次安装和保留配置、密钥、任务及运行状态的增量升级；旧破坏性安装脚本不进入新包。
- Jev 的 PASS、REWORK、DEEP_REVIEW、BLOCKED 判定门槛未变。真实运行安装与部署后 E2E 留在下一阶段。

# v0.5.0-Herdr-0.9.1-fix

- Prevent daemon reconciliation from racing agents in CREATING state.
- Use Herdr agent-list name/pane snapshots before declaring an agent REMOVED.
- A transient `agent get` failure alone no longer proves removal.
- Roll back the newly-created Herdr tab if `agent start` fails.
- Hook no longer retries every spawn error as a second agent.
- Clean installer now merges the PreToolUse redirect into `~/.codex/hooks.json` while preserving unrelated hooks.
- Adds a 45-second startup grace for Herdr agent initialization.

# Changelog

## 安装生命周期批次

- 新增 `uninstall.sh`、`rollback.sh` 与 Python `uninstall`、`rollback`、`list-backups` 命令。
- 成功安装创建权限受限的所有权账本和 SHA-256 程序快照；回滚必须指定当前版本链中的备份 ID。
- 卸载保留配置、密钥、registry、任务、锁、事件和备份；只移除哈希匹配的受管程序及 Relay 自有 Hook/AGENTS 片段。
- 三种变更共用活动任务/进程预检、维护排空和失败恢复；不操作 Herdr 标签，不执行 systemd 全局 reset。
- 新增临时 root、fake systemctl/proc 生命周期回归；未执行真实安装。

## 0.5.0
- Clean baseline after the v0.2-v0.4 upgrade chain.
- Added deterministic Astra -> Relay `SPAWN_AGENT` behavior via `astra-agent-spawn`.
- One Herdr child agent per Herdr tab.
- Added explicit Astra-triggered `RELEASE_AGENT` behavior via `astra-agent-release`; no autonomous TTL cleanup.
- Preserved Jev Semantic Sensor and post-work Reviewer responsibilities.
- Added per-agent exclusive lock.
- Added strict scoped-output failure behavior.
- Added strict numeric probability validation that rejects booleans.
- Added explicit defect guardrails in review routing.
- Fresh install removes old Relay runtime/state while preserving `secret.env`; Herdr/Codex untouched.
- Deep Review remains a state boundary; no Luna/Astra deep-stage automation yet.
# 0.5.0-fixed (unreleased)

- Added an opt-in OpenCodeReview final evidence gate to the explicit task
  finalization path. OCR JSON is normalized and failures fail closed.
- Kept the existing runtime Jev Semantic Sensor path unchanged and separate
  from the post-completion Jev Reviewer.
- Added `record-final-evidence` and `READY_FOR_FINAL` for Astra-supplied review,
  test, build, and lint results; final review attempts carry across rework tasks.
- Matched OCR v1.12.9 statuses, passed OCR errors through Jev with a
  fail-closed result, omitted raw OCR output from persisted results, and
  rejected repositories whose Git root is above the requested review path.
