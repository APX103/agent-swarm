# Next Steps — 后续路线图

> 生成于 2026-06-14 收尾时。用于明天/下次迭代接续。
> 进度明细见同目录 `iteration-progress.md`（每 wave 状态 + commit）。

## 现状快照（resume context）
- **分支**：`main` @ `aeb25f1`（两个特性分支已 `--no-ff` 合入）
- **测试**：249 passed（基线 156 → +93，全程 TDD、只增不删、无新运行时依赖、公开 HTTP API 稳定）
- **跑测试**：`uv venv --python 3.13 .venv` → `uv pip install -r requirements.txt -r requirements-orchestrator.txt pytest pytest-asyncio respx` → `.venv/bin/python -m pytest tests/`
- **关键模块**：`src/dispatcher/`（统一调度）、`src/orchestrator/`（含 resolver/external）、`src/gateway/routes.py`（外部 Agent）、`src/api/routes.py`（/api/chat + 幂等/死信/取消/背压）、`src/reliability/`（死信）、`src/observability/`（trace_id）、`src/agents/{worker,agent_loop}.py`（worker 真非阻塞+进度）
- **已完成目标**：3 轮主目标（统一 dispatch + 可插拔编排器 + 外部 Agent 健壮性）、W4–W8 稳健性、可靠投递（W9–W11）、流式进度（W12–W14）

---

## P1 — 直接价值延伸（建议下次从这里起）

### 0. 🔴 Session 持久化与多轮对话（最高优先，用户明确要求 2026-06-15）
- **现状缺口**：每次 `POST /api/chat` 都是独立的、一次性的任务。编排器 `_messages` 每次清零，无对话记忆；每个 task 独立 work folder（`tasks/{task_id}/`），无 session 概念。
- **用户期望**：
  1. **同一 session 多轮对话**：用户在同一 session 里多次发消息，编排器保留上下文（`_messages` 不清零，或做压缩后保留摘要）。
  2. **同一 session → 同一 work folder**：session 内所有轮次的 Agent 产出都落到同一个目录，不切换。
  3. **新 session → 新 work folder**：不同 session 互相隔离。
  4. **老 session ID → 老 work folder**：用老 session ID 继续对话时，复用老目录（能接着改之前的代码）。
- **设计方向**（待 brainstorm）：
  - `ChatRequest` 加 `session_id` 字段（可选，缺省 = 新建 session）。
  - `task_manager` 增加 session→work_folder 绑定（session 第一次创建时建 folder，后续复用）。
  - 编排器 `execute()` 检查 session_id：若已存在 → 加载历史 `_messages`（或压缩摘要）+ 复用 folder；若新建 → 初始化。
  - work folder 从 `tasks/{task_id}/` 改为 `sessions/{session_id}/`（或 `sessions/{session_id}/tasks/{task_id}/` 兼容多轮多任务）。
  - 对话历史压缩：超过阈值时摘要 + 保留最近 N 轮。
- **影响面**：`api/routes.py`（chat 入口）、`task_manager`（session→folder）、`orchestrator.execute()`（历史加载/复用）、`pool.checkout`（shared_dir 用 session 路径）、`config`（session TTL 等）。
- **规模**：L（涉及编排器生命周期 + 存储模型 + API 契约变更）。
- **验证标准**：同一 session 发 2 条消息 → 第 2 条能看到第 1 条的产物 + 编排器记得上文；不同 session → 隔离目录。

### 1. 可观测性·指标（metrics）
- **现状**：只有 trace_id（日志串联）；缺延迟/失败率/队列深度/每 agent 统计。
- **做法**：`src/observability/metrics.py` 一个轻量 in-memory Metrics（计数器 + 简单直方图）；在 Dispatcher 记录 dispatch 次数/成功失败/耗时/候选尝试次数；新增 `GET /api/v1/metrics`（JSON 或 Prometheus 文本）。
- **文件**：`src/observability/metrics.py`、`src/dispatcher/dispatcher.py`、`src/api/routes.py`。
- **规模**：M。

### 2. 持久化（Redis）— 生产化关键
- **现状**：幂等索引、死信、结果缓存、熔断状态、per-tenant 信号量全是 **in-memory → 仅单进程**。多 worker uvicorn 部署会失效。
- **做法**：把这些迁到 Redis（`redis` 依赖已在 requirements）。优先级：幂等索引 > 死信 > 结果缓存 > 熔断。每项加一个 Redis-backed 实现 + 保留 in-memory 作为 fallback。
- **文件**：`src/reliability/dead_letter.py`、`src/dispatcher/result_cache.py`、`src/api/routes.py`（_idempotency_index / _tenant_semaphores）、`src/dispatcher/dispatcher.py`（_breakers）。
- **规模**：L。

### 3. ExternalAgentBackend 流式
- **现状**：只有 DockerBackend 转发 `on_progress`；外部 Agent（a2a/openai/mcp）不流式。
- **做法**：a2a 协议的外部 Agent 可用 `poll_task` 转发；openai/mcp 是同步 invoke，可中途无法流式（标注即可）。
- **文件**：`src/dispatcher/backends.py`。
- **规模**：S–M。

---

## P2 — 健壮性 / 正确性

### 4. 取消传播到 worker（真实成本泄漏）
- **现状**：`cancel_running` 取消的是编排器侧 asyncio 任务；但 Docker worker 内的 `call_llm` 后台任务**仍在跑**（继续烧 LLM token），worker 不知道被取消。
- **做法**：加 A2A `tasks/cancel`（或停容器）信令；worker 收到后取消后台 `_run`。
- **文件**：`src/agents/worker.py`、`src/common/a2a_client.py`、`src/api/routes.py`。
- **规模**：M。

### 5. 配置外置
- **现状**：`DispatcherConfig`（max_retries/dispatch_timeout/max_concurrent/health_precheck）、`DEFAULT_TENANT_MAX_CONCURRENT`、结果缓存 TTL/size 都是硬编码。
- **做法**：进 `config.py` + `default.yaml.example`（新增 `dispatcher:` 段），main.py 读取注入。
- **文件**：`src/config.py`、`config/default.yaml.example`、`src/main.py`。
- **规模**：S。

### 6. 多进程安全审计
- 与 P1.2 绑定。在完成 Redis 化前，文档化"单进程假设"；多 worker 部署前必须先做 P1.2。
- **规模**：随 P1.2。

---

## P3 — 技术债 / 清理（小而快）

### 7. 把 `respx` pin 进 pyproject
- 现状：测试用了 respx 但没在 `pyproject` dev extras 里声明（只是装进了 venv）。
- 文件：`pyproject.toml`。规模：XS。

### 8. 消测试告警（2 个）
- `cli_adapter.py` `proc.kill()` coroutine never awaited（timeout 路径）。
- Starlette httpx 弃用告警（装 `httpx2` 或忽略）。
- 文件：`src/adapters/cli_adapter.py`、`pyproject.toml`。规模：XS。

### 9. `DispatchRequest.shared_context` 是死字段
- 编排器把 shared_context 折进了 task，该字段没用上。要么用，要么删。
- 文件：`src/dispatcher/base.py`。规模：XS。

### 10. worker.py 缺集成测试
- 抽出的 `agent_loop` 有测试，但 worker 的 A2A 端点（`message/send` 阻塞/非阻塞、`tasks/get`）没有。用 FastAPI TestClient 跑 worker `app` + mock LLM 测端到端（含非阻塞后台 + progress 写入）。
- 文件：`tests/test_worker_a2a.py`（新建）。规模：M。

---

## P4 — 安全（上线前必看）

### 11. `/api/v1/agents/invoke` 无鉴权
- 任何人都能 invoke 已注册 Agent。加 API key / token。
- 文件：`src/gateway/routes.py`。规模：M。

### 12. worker `run_command` 任意 shell
- `subprocess(shell=True)` 在沙箱内——信任边界要写清，或加命令白名单。
- 文件：`src/agents/worker.py`。规模：S。

### 13. LLM key 管理
- `config/default.yaml` 含真实 key（已 gitignore，✓）；确保 `.env` 流程文档化、`.env.example` 同步。
- 规模：XS。

---

## P5 — 验证（人工 / 集成）

### 14. docker e2e 冒烟 ✅ 已验证（2026-06-15）
- **已跑通**：构建 `swarm-worker:latest` → orchestrator (host) + 5 worker 容器 + redis → `POST /api/chat`（"写最简单 hello world 网页"）→ plan_task → dispatch(frontend-ux-pro) → pool checkout swarm-worker-0 → worker 真非阻塞后台执行 + 进度 → 编排器 poll_task 轮询（8 次 POST）→ read/list/review → finalize。产物 `frontend/index.html` 落盘且为合法 HTML5；全程 ~27s；trace_id 贯穿日志。**这同时验证了 W12/W13/W14 流式链路在真实运行时可用**（之前只有 mock 测试）。
- **本次 E2E 暴露并已就地处理的运行时配置问题**（非代码 bug）：
  - `config/default.yaml` 模型曾是 `glm-coding-plan`（占位名，端点报 1211 模型不存在）→ 改 `glm-4.7`（coding/paas 端点 `/models` 实际可用：glm-4.5/4.6/4.7/5）。
  - `storage.shared_output_base` 指向旧机器路径 → 改本机 repo `shared_output`。
  - docker.io 不可达 → 用本地缓存 `python:3.12.12-slim`（retag 成 `python:3.12-slim`）+ `--pull=false` 离线构建。
- **还暴露 1 个真 bug（待修）**：`requirements-orchestrator.txt` 缺 `redis` → orchestrator 容器镜像会因 `import redis.asyncio` 崩溃（本次靠 host 运行绕开）。**P3.11 见下。**
- 规模：✅ done（可重复：见下方「E2E 复跑步骤」）。

### 11b. 【真 bug】orchestrator Docker 镜像缺 redis 依赖
- `requirements-orchestrator.txt` 没有 `redis`，但 `src/registry/registry.py` 顶部 `import redis.asyncio`、`main.py` 导入 AgentRegistry → orchestrator 容器一启动就 ImportError 崩溃。
- 修：`requirements-orchestrator.txt` 加 `redis>=5.0.0`（和 `requirements.txt` 对齐）。这样 `docker-compose up`（orchestrator 也进容器）才能跑。
- 文件：`requirements-orchestrator.txt`。规模：XS。

### E2E 复跑步骤（已验证可跑）
```bash
# 1. redis（registry 用）
docker rm -f swarm-redis 2>/dev/null; docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine
# 2. worker 镜像（离线，docker.io 不可达时）
docker tag python:3.12.12-slim python:3.12-slim   # 用本地缓存
docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .
# 3. orchestrator（host；先修 config/default.yaml 的 storage 路径 + model=glm-4.7）
.venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 9000
# 4. 打一个任务
curl -X POST localhost:9000/api/chat -H 'Content-Type: application/json' -d '{"message":"写最简单 hello world 网页"}'
# 5. 收尾
docker rm -f swarm-redis swarm-worker-{0..4}
```

### 15. 端到端流式集成测试
- `/api/chat` → orchestrator → dispatcher → mocked worker（流式）→ 断言 `agent_progress` 事件序列。
- 文件：`tests/test_e2e_streaming.py`（新建）。规模：M。

---

## ✅ 最高标准 E2E（2026-06-15）：生成完整可运行工程 + 验证能跑
- **已通过**：用真实 Docker worker 生成一套完整 **TODO 应用**（FastAPI 后端 `backend/main.py`+`requirements.txt` ＋ HTML 前端 `frontend/index.html`）。编排器 `plan_task → dispatch_agents_parallel(backend+frontend) → finalize`，trace 贯穿，W4 review 门确保两 Agent 都产出。
- **验证可运行**：起后端 `uvicorn main:app` → `GET /` 健康检查 200；`GET /todos` → `[]`；`POST /todos {title}` → `{"id":1,...}`；`GET /todos` → 列表持久；空 title → **HTTP 400 校验生效**。前端合法 HTML 且 `fetch` 调 `/todos`。
- 任务产物留存：`shared_output/tenants/default/tasks/e6e10d60/`。**这取代了之前"只测过 trivial 单页"的保留意见。**
- 复跑：见上方「E2E 复跑步骤」+ 改 POST 的 message 为多 Agent 工程任务即可。

### 🔔 终极 E2E（多 Agent 分工 + 联调 + 浏览器渲染验证）— 2026-06-15 ✅
- 任务 `27dac4a8`：前后端分离 TODO（FastAPI 4 个接口 + HTML 前端 CRUD）。
- **多 Agent 调度**：编排器 `plan_task → dispatch_agents_parallel(backend+frontend)`，**第三步 `dispatch_agent(frontend,"联调最小化修正/接口一致性")`** —— 主动审查两份产出并要求前端对齐后端接口（即"分离又协作"）。
- **联调对齐**：后端路由 `GET/POST /api/todos`、`PATCH/DELETE /api/todos/{id}` ＋ 模型 `{id,title,done}` ↔ 前端 fetch `${API_BASE}/todos`(GET/POST)、`/todos/${id}`(PATCH/DELETE) ＋ `API_BASE_URL=http://localhost:8000/api` —— **完全一致**。
- **后端 CRUD 实跑**：POST/PATCH(toggle)/GET(list)/DELETE(200) 全过，无错。
- **浏览器渲染验证（金标准）**：用 Chrome headless 渲染前端，预置一个 todo 后，**渲染出的 DOM 里确实包含后端返回的数据** —— 证明前端 JS 真的 fetch 后端并渲染，端到端联调成立。
- 结论：Swarm **能**做"多 Agent 分工 + 必须联调"的工程。产物 `shared_output/tenants/default/tasks/27dac4a8/`。

## 明天怎么接
1. 读 `docs/plans/iteration-progress.md`（每 wave 状态 + commit）+ 本文件。
2. 选一个 P1 开做（建议 **指标** 或 **Redis 持久化**）；仍按 TDD + 每 wave 一 commit + 完成即合并 main 的节奏。
3. 测试命令：`.venv/bin/python -m pytest tests/`（venv 见上方）。
