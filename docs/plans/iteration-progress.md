# Agent 调度优化 — 迭代进度追踪

> 本文件用于跨会话 / 上下文压缩后仍能记住"现在到第几轮"。每次轮次推进或状态变化请更新本文件。
> **目标全集（含实现+验证）**：`2026-06-14-agent-scheduling-optimization-goal.md`
> 执行模式：原 `/loop`（每 10 分钟重复触发）已取消 → **goal 模式 · 3 轮迭代**，本文件计数。

---

## 当前状态
- **当前轮次**：`3 / 3 完成`（三轮迭代全部完成 🎉）
- **总轮次**：3
- **上次更新**：2026-06-14
- **下一步**：系列收尾——可选 docker e2e 冒烟（人工）；系列 backlog 进入后续 wave

### 澄清结论（已锁定）
1. 「外接调度Agent」= **可插拔编排器**。
2. 选择策略 = **配置指定 + 自动回退**。
3. 内置编排器 = **统一 dispatch 到外部 worker**。
4. goal 1 四项调度优化全做；其余稳定性功能入系列 backlog。
5. 架构 = **方案 A 双接缝分层**。用户授权按推荐执行，不再逐个确认。

---

## Round 1 — 健壮性地基（goal 2 健壮性，零行为变更、最低风险）·【完成】
- [x] **R1.1** 修复 gateway ↔ AgentRegistry 对接 ✓ TDD：新增 test_gateway_registry_integration.py（真实 AgentRegistry+FakeRedis 全链路）；修 gateway register(dict)/heartbeat(bool→404)/deregister(get_agent→404)；修正 test_gateway.py + test_e2e stub 到真实契约。161 passed。
- [x] **R1.2** 注册时自动建 adapter ✓ TDD：新增 test_gateway_adapter_provisioning.py；gateway 注册时按协议自动 create_adapter+register（先校验再落库，失败回滚）；未知协议 400；http 为 register-only。165 passed。
- [x] **R1.3** 外部 /invoke 接入 CircuitBreaker ✓ TDD：新增 test_gateway_invoke_circuit_breaker.py；gateway 维护 per-agent CircuitBreaker，/invoke 经 breaker.call；CircuitOpenError→503；set_deps 清空断路器状态隔离测试。167 passed。
- [x] **R1.4** 补真正的 A2A adapter ✓ TDD：新增 test_a2a_adapter.py；新建 src/adapters/a2a_adapter.py（包 A2AClient，invoke 映射 A2ATask→AgentResult，health_check 走 agent.json）；PROTOCOL_REGISTRY["a2a"]=A2AAdapter（替换 OpenAI 冒充）；更新 test_adapters.py。
- [x] **R1.V** Round 1 验证 ✓ `pytest tests/` 173 passed（156→173，只增不删）；导入无环；app 工厂 + 5 条 gateway 路由就绪。（docker /invoke 冒烟延后到 R2 验证，因 R1 不涉及 Docker 池）

## Round 2 — 统一调度（goal 1 + goal 2）·【完成】
- [x] R2.1 Dispatcher 协议 + 数据类 ✓ src/dispatcher/base.py（Protocol + DispatchTarget/Request/Attempt/Result）。
- [x] R2.2 DockerBackend / ExternalAgentBackend ✓ src/dispatcher/backends.py；DockerBackend（pool+A2A，try/finally 归还容器）；ExternalAgentBackend（registry+adapter）。资源泄漏防护验证。
- [x] R2.3 skill 智能选择 + 候选解析 ✓ Dispatcher._resolve 合并多 backend 候选。
- [x] R2.4 失败重试 + 故障转移 ✓ max_retries 切候选；max_attempts 上限。
- [x] R2.5 健康预检 ✓ health_precheck 过滤 + CB open 跳过。
- [x] R2.6 超时 + 背压 ✓ asyncio.wait_for(timeout) + Semaphore(max_concurrent)。
- [x] R2.7 per-agent 熔断 ✓ target→CircuitBreaker，按 attempt.success 记录；OPEN 短路。
- [x] R2.8 BuiltinOrchestrator 改调 Dispatcher ✓ dispatch_agent/parallel/check_status 全走 Dispatcher；保留 LLM 工具契约；main.py 注入 Docker+External 全量 Dispatcher；新增注入集成测试。
- [x] R2.V Round 2 验证 ✓ `pytest tests/` 197 passed（156→197）；app 工厂 + 路由就绪。（docker-compose /api/chat 真实冒烟为人工步骤：需 Docker daemon + 镜像 + LLM key，建议用户本地跑 `docker-compose up -d` 后 curl /api/chat）

## Round 3 — 可插拔编排器（goal 3）·【完成】
- [x] R3.1 OrchestratorBackend 协议 + OrchestratorConfig ✓ src/orchestrator/base.py（runtime_checkable Protocol + OrchestratorConfig）；Orchestrator 类满足协议。
- [x] R3.2 OrchestratorResolver（config + 自动回退）✓ src/orchestrator/resolver.py；external 失败→日志+orchestrator_fallback 事件→回退 builtin；fallback=false 抛错。
- [x] R3.3 ExternalOrchestrator（A2A）✓ src/orchestrator/external.py；包 A2AClient，失败/无 task 显式抛供回退。
- [x] R3.4 接入 /api/chat ✓ api/routes.py set_deps(resolver=) + chat() 优先 resolver（向后兼容无 resolver 调用）；main.py 组装 resolver 注入。
- [x] R3.5 config 扩展 ✓ config.py Settings.orchestrator + default.yaml orchestrator 段（默认 builtin）+ ORCHESTRATOR_PROVIDER/EXTERNAL_ENDPOINT env 覆盖。
- [x] R3.V Round 3 验证 + 系列收尾 ✓ `pytest tests/` 207 passed；app 工厂 OK；config 加载校验通过。

---

## 每轮完成标准（DoD）
- 所有现有测试通过；新增对应单元/集成测试（只增不删）。
- 无新依赖（仅用 requirements 现有栈）。
- 不破坏公开 HTTP API（`/api/chat`、`/api/tasks*`、`/api/v1/agents/*`）。
- 关键操作有日志、错误显式传播、资源用上下文管理器关闭、新代码带类型提示。
- 轮次完成后：在本文件勾选项、更新"当前轮次"、写一行变更记录。

---

## 变更记录
- 2026-06-14：初始化。取消 `/loop`（job ebd90f53），改为 3 轮 goal 迭代；建追踪文件。
- 2026-06-14：澄清完成（4 问 + 架构方案 A 全锁定）；goal 1 四项全纳入 Round 2；稳定性功能入 backlog。
- 2026-06-14：目标全集文档定稿（`2026-06-14-agent-scheduling-optimization-goal.md`）。进入 **Round 1**，开始 R1.1。
- 2026-06-14：**R1.1 完成**（gateway↔registry 对接修复，TDD，161 passed）。开始 R1.2（注册自动建 adapter）。
- 2026-06-14：**Round 1 全部完成（R1.1–R1.4 + R1.V）**。173 passed；导入无环；app 就绪。进入 **Round 2**（统一调度），开始 R2.1。
- 2026-06-14：**Round 2 全部完成（R2.1–R2.8 + R2.V）**。197 passed。统一 Dispatcher（Docker+外部 Agent 候选池 + 重试/预检/超时/背压/per-agent 熔断 + 资源泄漏防护）落地；BuiltinOrchestrator 全量改调 Dispatcher；main.py 注入全量 Dispatcher。进入 **Round 3**（可插拔编排器），开始 R3.1。
- 2026-06-14：**Round 3 全部完成（R3.1–R3.5 + R3.V）。三轮迭代全部完成。207 passed（156→207，+51，只增不删）。** 可插拔编排器落地：OrchestratorBackend 协议 + OrchestratorResolver（config 指定 + 自动回退，事件显式标注）+ ExternalOrchestrator（A2A）+ /api/chat 路由 + config.orchestrator（默认 builtin，零行为变更）。goal 1/2/3 全部达成。提交 `def8bc4`。
- 2026-06-14：**Wave 4（可观测性）** 214 passed，提交 `c2db32b`。**Wave 5（周期 health_sweep）** 217 passed，提交 `08373aa`。系列继续推进 backlog。
- 2026-06-14：**Wave 6（启动自检）** 221 passed `6148624`；**Wave 7（per-tenant 背压）** 224 `8659711`；**Wave 8（真实取消）** 228 `309e566`。
- 2026-06-14：**可靠投递目标完成（W9 幂等性 `c395d5b` + W10 死信 `c10fb12` + W11 优雅降级缓存 `bdcfca9`）。** 242 passed（156→242，+86，只增不删）。已合并入 main（`9c3b842`）。
- 2026-06-14：**流式进度目标完成（W12 A2A poll_task `ac895f2` + W13 worker 真非阻塞+进度 `4a4c411` + W14 编排器转发 `agent_progress` `3183075`）。** 249 passed（156→249，+93）。分支 `feat/streaming-progress`，待合并。
