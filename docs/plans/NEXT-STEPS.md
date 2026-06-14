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

### 14. docker e2e 冒烟（始终没跑过）
- `docker-compose up -d` + 构建 worker 镜像 → `curl /api/chat` 真实任务（builtin 经 Dispatcher→Docker）；注册 mock 外部 Agent → `/invoke`；`provider=external` 指向 mock 调度 Agent → 演示自动回退。
- 规模：M（人工，需 Docker daemon + key）。

### 15. 端到端流式集成测试
- `/api/chat` → orchestrator → dispatcher → mocked worker（流式）→ 断言 `agent_progress` 事件序列。
- 文件：`tests/test_e2e_streaming.py`（新建）。规模：M。

---

## 明天怎么接
1. 读 `docs/plans/iteration-progress.md`（每 wave 状态 + commit）+ 本文件。
2. 选一个 P1 开做（建议 **指标** 或 **Redis 持久化**）；仍按 TDD + 每 wave 一 commit + 完成即合并 main 的节奏。
3. 测试命令：`.venv/bin/python -m pytest tests/`（venv 见上方）。
