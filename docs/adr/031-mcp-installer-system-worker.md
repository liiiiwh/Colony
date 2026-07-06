# ADR-031 · 专职 MCP Installer 系统 worker（把 MCP 安装从 Builder 上下文剥离）

**Status**: Accepted (2026-07-01)
**Revises**: ADR-028 D2（Builder 自己 run_shell 装 MCP）、ADR-030 D1（Builder 经审批 run_shell 装 MCP）

## Context

真实运行反复出现同一类失败：让 Builder super「一句话造一个能发小红书的 super」，它把 super + 7 个 worker（含 `xhs_publisher`）都建出来、报告"构建完成",但**整个 MCP 安装环节被跳过**——`shell_audit_log` 空、`mcp_servers` 空、`xhs_publisher` 的 MCP 绑定为 NONE。它明明在方案里写了"依赖 xhs-mcp 安装 + 扫码",BUILD 阶段却直接不调 `run_shell`/`mcp_server_register`/`mcp_ensure_ready`,建了个空壳发帖 worker。

根因是**架构层面的**:把「装第三方 MCP」这段多步、脆弱的流程(consent → clone/build/launch → register(startup_command) → bind → readiness/QR)交给 Builder 用一连串 LLM tool call 内联编排。Builder 的 BUILD turn 本就要塞 30+ tool call(建 super/worker/mission/schedule/aux-model...),MCP 这段"嫌烦就略过",而 build 照样"成功"。之前给现路径打的补丁(假 ready 硬失败、startup_command 强校验)拦不住——因为 super **压根不调**那些工具。用户明确指出:这套做得过度复杂、脆弱。

## Decision

**新增系统级 `MCP Installer` worker**(`capability='mcp_installer'`, `kind='worker'`, `is_system=True`, `category='installer'`),把 MCP 安装从 Builder 上下文彻底剥离:

- **Builder 只委派**:BUILD 时建完需要 MCP 的 worker 后,`invoke_worker(capability:mcp_installer, goal={mcp, bind_to_agent_id, target_project_id})`。Builder 协议里整段 MCP 手搓删除,`run_shell`/`mcp_server_register`/`mcp_ensure_ready`/`agent_mcp_bind` **不再绑给 Builder**——它连工具都没有,想跳也无从跳、想手搓也不能。上下文和出错面都小一圈。
- **Installer 专职执行**:一份专注协议(故意短,只做一件事)在自己上下文里跑确定性安装链——①`request_approval` 征得安装同意(shell 门据此放行,ADR-030)→ ②`run_shell` clone+build+启动 → ③`mcp_server_register(startup_command=...)` → ④`agent_mcp_bind` 绑给目标 worker → ⑤`mcp_ensure_ready(target_project_id=...)` 探活 + 登录时弹 QR 卡到目标会话 → ⑥返回 `{mcp_server_id, ready, awaiting_user}`。它**只**为安装 MCP 跑 shell,不做别的副作用。
- **技能授予**:seed 系统 agent 走**显式绑定**(不靠 scope 自动绑),给 installer 绑 run_shell + mcp_server_register + agent_mcp_bind + mcp_ensure_ready + clawhub_* + request_approval + memory_append。`kind='worker'` 故 `invoke_worker`(`_resolve_worker` 过滤 kind='worker')能直接解析——无需改派发逻辑,与 approval_judge 同模式。
- **介入时机**:build 期装(用户选定)。build 出来的 worker 即刻可用,只剩用户扫码;QR 卡落到新 super 的会话。

## Considered alternatives
- **只修现路径 bug（假 ready 硬失败 + startup_command 强校验）**：已做(ADR-030 补修),但拦不住"super 根本不调 MCP 工具"。留作纵深防御,非充分解。
- **协议硬门（build 时缺 MCP 就不让收尾）**：仍靠 Builder LLM 执行安装,脆弱面没搬走。否。
- **确定性配方目录（平台完全接管安装,不经 agent）**：最彻底,但要维护 curated 配方 + 大改。用户选了"专家 worker"路线——保留 agent 灵活性(任意 ClawHub/repo)的同时把复杂度装进一个专注上下文。可作为将来进一步简化方向。
- **installer 用 `kind='installer'`（scope 自动拿 builder 技能）**：seed 走显式绑定不用 scope;且 `_resolve_worker` 只认 kind='worker'。用 kind='worker'+显式绑更省事、无需动派发。

## Consequences
- Builder 上下文显著变瘦(删掉整段 MCP 手搓 + 4 个工具),BUILD turn 更聚焦、更少截断,不再"嫌烦跳过"MCP。
- MCP 安装有了单一、专注、可复用的执行体;失败面集中、可观测(它自己的会话/审计)。
- 需要 MCP 的 build 会多一次 `invoke_worker(mcp_installer)`(一次子 agent 运行的开销),换取可靠性。
- 仍未做:平台确定性配方(installer 仍由 LLM 按协议执行安装链,比裸 Builder 稳很多但非 100% 确定性);xiaohongshu-mcp 真跑仍需用户扫码。
