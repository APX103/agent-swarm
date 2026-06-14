# Agent 调度优化 — 超级完整目标 (GOAL)

- **状态**：已批准（按推荐方案 A 执行）→ 执行中
- **模式**：goal 模式，3 轮迭代（轮次计数见 `iteration-progress.md`）
- **关联**：设计基线见本文「锁定决策」；进度勾选见 `iteration-progress.md`

---

## 0. 目标声明

把 Agent Swarm 的**调度**与**外部 Agent 接入**做成：

1. **调度逻辑健壮**（goal 1）：失败重试+故障转移、健康预检、超时+背压、skill 智能选择。
2. **外部 Agent 接入省心且健壮**（goal 2）：修 gateway↔registry 对接 bug、注册即自动建 adapter、/invoke 接熔断、补真正的 A2A adapter、内置编排器统一 dispatch 到外部 worker。
3. **编排器可插拔**（goal 3）：整个 plan→dispatch→review→finalize 循环可被外部「调度 Agent」接管；配置指定 + 自动回退（日志/事件显式标注）。

用**现有技术栈**、**不破坏公开 API**、**不删测试**、**防御式/错误显式传播/资源上下文管理/日志完整/类型提示**。

---

## 1. 锁定的设计决策

| 决策点 | 选择 |
|---|---|
| 架构方案 | **A 双接缝分层**：`Dispatcher`（worker 调度）+ `OrchestratorBackend`（编排循环）协议 + `OrchestratorResolver` |
| 编排器选择 | `config.orchestrator.provider`（默认 builtin）+ 外部不可用/熔断时**自动回退内置**，日志与事件显式标注（不静默吞错） |
| 内置 dispatch | 统一到 Docker 容器 + registry 外部 worker（同候选池） |
| goal 1 范围 | 失败重试+故障转移 / 健康预检 / 超时+背压 / skill 智能选择 **全部做** |
| 约束 | 项目重构宪法：无新依赖、不破坏公开 API、只重构+被要求功能、不删测试、防御式/显式/可测/KISS |

---

## 2. 整体成功标准 (DoD)

- [ ] `pytest tests/` 全绿；测试**只增不删**。
- [ ] 公开 HTTP API 不变：`/api/chat`、`/api/tasks*`、`/api/v1/agents/*` 行为向后兼容（新增字段/端点允许）。
- [ ] 无新第三方依赖（仅 `requirements*.txt` 现有栈）。
- [ ] 三轮全部完成；每轮有对应测试，并在 `iteration-progress.md` 勾选 + 写变更记录。
- [ ] 关键操作有日志；错误显式传播（无 bare `except` 静默吞错）；资源用 `try/finally` 或 `async with` 关闭；新代码带类型提示。

---

## 3. 任务全集（按轮；每项含 impl 实现要点 + verify 验证标准）

### Round 1 — 健壮性地基（goal 2 健壮性 · 零行为变更 · 最低风险）

#### R1.1 修复 gateway ↔ AgentRegistry 对接
- **impl**
  - `register`：组装 **dict** 传 `registry.register(dict)`（当前用关键字参数 → TypeError）。
  - `heartbeat`：`ok = await registry.heartbeat(agent_id)`（返 bool）；`ok`→200+`next_heartbeat_in`，`not ok`→404；移除对 `KeyError` 的依赖（registry 不抛）。
  - `deregister`：`await registry.deregister(agent_id)`（安全 no-op）→200；registry 不可用→503。
  - gateway Pydantic schema 与 `registry.models` 对齐（capabilities 等可选字段）。
- **verify**
  - 新增 `tests/test_gateway_registry_integration.py`：`AgentRegistry` + FakeRedis 跑 register→heartbeat→deregister 全链路，断言状态码与字段。
  - 修正 `test_e2e_external_agent.py` 中与真实 registry 契约不符的 stub（对齐真实 `AgentRegistry`，不删用例）。
  - `pytest tests/test_registry.py tests/test_e2e_external_agent.py tests/test_gateway_registry_integration.py -v` 全绿。

#### R1.2 注册时自动建 adapter
- **impl**：gateway `register` 成功后调 `adapter_manager.register_from_info(agent_id, info)`；**先验证+创建 adapter**，失败（未知协议等）→400 且不写 registry；成功→registry + adapter 都注册。
- **verify**
  - 测试：register openai agent → **立即** `/invoke` 成功（无需额外 `register_from_info`）。
  - 测试：register 未知 protocol → 400，且 `list_agents` 不含该 agent。
  - `test_e2e_external_agent.py` 的 invocation 用例改为「仅 register 即可 invoke」。

#### R1.3 外部 `/invoke` 接入 CircuitBreaker
- **impl**：gateway 维护 `agent_id → CircuitBreaker`（用现有 `CircuitBreaker`）；`/invoke` 经 `CB.call(adapter.invoke, ...)`；`CircuitOpenError`→503。
- **verify**：测试连续失败超阈值→`/invoke` 返 503；恢复窗口后 half-open 放行（fake adapter 控制 success/failure）。

#### R1.4 真正的 A2A adapter
- **impl**：新建 `src/adapters/a2a_adapter.py`：`A2AAdapter(AgentBackend)` 包 `A2AClient`；`invoke(task,ctx)`→`send_message(blocking=True)`→映射 `A2ATask`→`AgentResult`；`health_check`→`get_agent_card() is not None`；`name` 属性；`async close()`。`PROTOCOL_REGISTRY["a2a"]=A2AAdapter`（替换 OpenAI 冒充）。
- **verify**：测试 `A2AAdapter.invoke` 对 mock A2A 服务（respx）返回 `AgentResult(success, output, artifacts)`；`health_check` 真/假；`close` 关闭 client；`create_adapter({"protocol":"a2a","base_url":...})` 返回 `A2AAdapter` 实例。

---

### Round 2 — 统一调度（goal 1 调度优化 + goal 2 接入便利）

#### R2.1 Dispatcher 协议与数据类
- **impl**：`src/dispatcher/base.py`：`Protocol Dispatcher`；dataclass `DispatchTarget(kind:"docker"|"external", agent_type/skill, agent_id?, endpoint?)`、`DispatchRequest(task, context, shared_context, timeout?)`、`DispatchResult(success, output, artifacts, error, target, attempts[])`。
- **verify**：构造/类型测试；Protocol 结构断言。

#### R2.2 后端 DockerBackend / ExternalAgentBackend
- **impl**：`src/dispatcher/backends.py`。`DockerBackend`：封装 `pool.checkout`+`A2AClient`（迁移自现 orchestrator）。`ExternalAgentBackend`：经 `AdapterManager`+registry 取 adapter 调用。各自 `candidates(spec)` 与 `invoke(target, req)`。
- **verify**：mock pool/adapter，断言 invoke 路径与资源归还。

#### R2.3 skill 智能选择 + 候选解析
- **impl**：`Dispatcher.resolve_candidates(req)`：Docker 候选（card skills 含目标 skill/type）+ 外部候选（`registry.find_by_skill`）。选择：健康预检过滤→skill 命中度→轮询/首健康。**约定**：外部 Agent `skills` 含 agent_type id 即视为该 type 候选。
- **verify**：候选集合构造测试（Docker only / external only / 混合）；选择确定性测试。

#### R2.4 失败重试 + 故障转移
- **impl**：dispatch 内循环 candidates，失败切下一个；`max_retries`（config）；记 `attempts`；全失败→`DispatchResult(success=False)`。
- **verify**：两候选，第一失败第二成功→成功且 `attempts` 记录；全失败→`success=False`。

#### R2.5 健康预检
- **impl**：dispatch 前对候选 `health_check`（adapter.health_check / Docker 容器 ready）；跳过不健康；与 CB 联动（open 视为不健康）。
- **verify**：候选含不健康者→被跳过。

#### R2.6 超时 + 背压
- **impl**：单 dispatch `asyncio.wait_for(timeout)`；全局 `asyncio.Semaphore(max_concurrent)`；超时→失败→走重试/故障转移。
- **verify**：慢 candidate 超时触发；并发超上限被信号量限制。

#### R2.7 per-agent 熔断 + 资源泄漏防护
- **impl**：Dispatcher 维护 `target→CircuitBreaker`；invoke 经 CB；所有后端 invoke 用 `try/finally` 归还容器/关闭临时 client；迁移后天然修复现 orchestrator cleanup 不在 finally 的隐患。
- **verify**：异常路径下容器被归还（mock `pool.return_container` 被调用）；连续失败→CB open。

#### R2.8 BuiltinOrchestrator 改调 Dispatcher
- **impl**：`_tool_dispatch_agent`/`_tool_dispatch_agents_parallel` 改调 `Dispatcher.dispatch(...)`；**保留工具签名与返回格式**（向后兼容 LLM 工具契约）；移除内联 pool/a2a 逻辑。
- **verify**：`test_orchestrator.py` 现有用例通过；新增 Dispatcher 注入的 orchestrator 集成测试。

---

### Round 3 — 可插拔编排器（goal 3）

#### R3.1 OrchestratorBackend 协议
- **impl**：`src/orchestrator/base.py`：`Protocol execute(task_id, tenant_id, user_message, event_callback=None)->str`。现 `Orchestrator` 类满足协议（**保持类名**，向后兼容 import）。
- **verify**：Protocol 断言；`Orchestrator` 实例满足协议。

#### R3.2 OrchestratorResolver（config + 自动回退）
- **impl**：`src/orchestrator/resolver.py`：按 `config.orchestrator.provider` 选；`external`→`ExternalOrchestrator`；`execute` 包装 try/except+CB：失败/CircuitOpen→log+emit("orchestrator_fallback")→跑 builtin；`fallback=false`→直接抛。
- **verify**：provider=builtin→builtin；external+健康→external；external 失败+fallback=true→回退且事件发出；fallback=false→抛错。

#### R3.3 ExternalOrchestrator（A2A）
- **impl**：`src/orchestrator/external.py`：包 `A2AClient` 指向外部调度 Agent；`execute`→`send_message(blocking=True)`→返回 summary；超时/错误显式抛。进度：基础版 blocking（流式入 backlog）。
- **verify**：mock A2A 调度 Agent→`execute` 返回其 summary；失败→抛异常供 Resolver 回退。

#### R3.4 接入 /api/chat
- **impl**：`main.py` 组装 Resolver（注入 builtin orchestrator + config）；`api/routes.py` `chat()` 改调 `resolver.execute(...)`（**保持签名/返回**）。不破坏 `TaskResponse`。
- **verify**：`test_api.py` `/api/chat` 用例通过；新增 external provider 路由测试。

#### R3.5 config 扩展
- **impl**：`config.py` 加 `OrchestratorConfig(provider, external{agent_id,endpoint,timeout}, fallback, dispatcher{max_retries,dispatch_timeout,max_concurrent,health_precheck})`；`default.yaml` 加对应段（默认 provider=builtin，零行为变更）。
- **verify**：缺省→provider=builtin；加载测试；`.env` 覆盖。

---

## 4. 系列稳定性 backlog（本轮 3 轮之外，后续 wave 统一完成）

> 用户指示：能想到的稳定性功能都放进目标，可能不在这一波，但系列内统一完成。
- 优雅降级 L1-L3（external 不可用 → Docker → 缓存 → 显式错误）
- 死信 / 失败任务记录与重放
- 幂等性（重复 dispatch 去重）
- 可观测性：结构化日志 + 任务 trace ID + 指标（延迟 / 失败率 / 队列深度）
- 限流（per agent / per tenant）
- 取消与超时传播（取消长时 dispatch）
- 启动期配置校验 + 自检（pool / redis / llm 可达性 fail-fast）
- 周期触发 `registry.health_sweep`（TTL 清理方法已有，需调度）
- 非阻塞 A2A + 流式进度（P2）
- ExternalOrchestrator 流式进度事件（polling tasks/get）

---

## 5. 验证策略（整体）

- **单元**：每个新模块对应 `test_*.py`，mock 外部（httpx respx / FakeRedis / fake pool）。
- **集成**：gateway↔registry↔adapter 全链路；orchestrator→Dispatcher→backend；resolver 回退。
- **回归**：`pytest tests/` 全绿（当前 38+，只增不删）。
- **手动（每轮结束）**：`docker-compose up` → `/api/chat` 跑真实任务（builtin）；注册 mock 外部 agent → `/invoke`；provider=external 指向 mock 调度 agent → `/api/chat` 走外部 + 回退演示。

---

## 6. 执行顺序与依赖

```
R1（无依赖，先修 bug）→ R2（依赖 R1 的 adapter/registry 可用）→ R3（依赖 R2 的 Dispatcher 供 builtin 用）
```
每轮结束：在 `iteration-progress.md` 勾选 + 变更记录 + 跑全量 `pytest tests/`。

---

## 变更记录
- 2026-06-14：目标文档创建（按推荐方案 A 定稿，用户授权按推荐执行）。进入 Round 1。
