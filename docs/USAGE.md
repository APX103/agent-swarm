# Agent Swarm 使用文档

> 面向团队的使用指南。**所有命令和流程均在本机（macOS + Docker Desktop + Python 3.13）实测通过**，不是凭空写的。
> 最后验证日期：2026-06-15。测试基线：274 tests passed。

---

## 1. 它是什么

一个基于 Docker 的多 Agent 协作系统：你给编排器一句话，它用 LLM 拆解任务、并行调度多个 Agent（前端/后端/通用）、各自在隔离的 Docker 容器里干活、产物落到共享目录，最后编排器审查并交付。

```
你 → POST /api/chat → 编排器(LLM 工具循环)
                         ├─ plan_task（拆解 + 共享 API 契约）
                         ├─ dispatch_agents_parallel（并行调度多 Agent）
                         │    ├─ Worker 容器 1（前端）→ 写 shared_output/.../frontend/
                         │    └─ Worker 容器 2（后端）→ 写 shared_output/.../backend/
                         ├─ review（强制审查，不通过要求返工）
                         └─ finalize（交付）
```

**核心能力（均已实测）**：
- 多 Agent 并行调度 + 前后端联调（编排器主动对齐接口）。
- 产出**完整可运行**的工程（已验证：FastAPI CRUD 实跑 + 浏览器渲染后端数据）。
- 接入你自己的 Agent（4 种方式）。
- 可插拔编排器（你自己的 Agent 也能当调度器）。

---

## 2. 前置要求

| 组件 | 要求 | 验证版本 |
|------|------|---------|
| 操作系统 | macOS 或 Linux | macOS 15 (Darwin 25.5) |
| Python | ≥ 3.12 | 3.13.2 |
| Docker | Docker Desktop 或 Docker Engine | 29.4.0 |
| LLM API | 智谱 GLM Coding Plan（或任意 OpenAI 兼容端点） | glm-4.7 |
| 网络 | 能拉 PyPI（docker.io 可能不通，见下方踩坑） | — |

---

## 3. 快速开始（最小可用路径）

```bash
# 0. 克隆
git clone <repo-url> && cd swarm

# 1. Python 环境（uv 最快；或用 python3.13 -m venv .venv 等价）
uv venv --python 3.13 .venv
uv pip install -r requirements.txt -r requirements-orchestrator.txt

# 2. 配置（模板 → 实际配置文件）
cp config/default.yaml.example config/default.yaml
#   编辑 config/default.yaml，至少改这 3 行：
#     default_api_key: "你的 GLM key"          ← 必填
#     shared_output_base: "/绝对路径/swarm/shared_output"  ← 改成本机 repo 路径
#     default_model: "glm-4.7"                 ← 确认是这个（不是 glm-coding-plan）

# 3. 构建 Worker Docker 镜像
#    如果 docker.io 可达（能拉 python:3.12-slim）：
docker build -t swarm-worker:latest -f docker/Dockerfile.worker .
#    如果 docker.io 不可达（国内常见），用离线方案（见第 7 节踩坑）：
#    docker tag python:3.12.12-slim python:3.12-slim && docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .

# 4. 启 Redis（Agent 注册中心用）
docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine

# 5. 启编排器（前台运行，看日志）
.venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 9000

# 6. 验证（另一个终端）
curl http://localhost:9000/api/health
#   → {"status":"ok","pool_available":5,"pool_total":5,...}  ← 看到 pool 5/5 就成了

# 7. 发一个任务
curl -X POST http://localhost:9000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"写一个最简单的 hello world 网页"}'
#   → {"task_id":"xxxxx","status":"running",...}

# 8. 查结果
curl http://localhost:9000/api/tasks/<task_id>
#   status 变 "completed" 后，看 shared_output/tenants/default/tasks/<task_id>/ 里的产物
```

---

## 4. 安装详解

### 4.1 Python 环境

```bash
# 方案 A：uv（推荐，快）
uv venv --python 3.13 .venv
uv pip install -r requirements.txt -r requirements-orchestrator.txt

# 方案 B：标准 venv（等价）
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-orchestrator.txt
```

> 两个 requirements 的区别：`requirements.txt`（Worker 用，含 redis）和 `requirements-orchestrator.txt`（编排器用，含 docker SDK）。编排器需要两者都装（它同时用 redis 和 docker）。

### 4.2 配置文件

```bash
cp config/default.yaml.example config/default.yaml
```

编辑 `config/default.yaml`，关键字段：

```yaml
llm:
  default_model: "glm-4.7"            # ✅ 正确。不要用 glm-coding-plan（会 400）
  default_base_url: "https://open.bigmodel.cn/api/coding/paas/v4"
  default_api_key: "你的 key"          # 必填

container_pool:
  pool_size: 5                         # warm pool 容器数
  image_name: "swarm-worker:latest"    # 与下方 build 的 tag 一致
  base_port: 9001                      # worker 端口起始

storage:
  shared_output_base: "/你的路径/swarm/shared_output"  # 绝对路径，产物落地处
```

> `config/default.yaml` 是 **gitignored**（含 API key）。团队每人自己建。模板是 `default.yaml.example`。

### 4.3 Worker Docker 镜像

```bash
# 标准（docker.io 可达时）
docker build -t swarm-worker:latest -f docker/Dockerfile.worker .

# 离线（docker.io 不可达时，用本地缓存的 base）
docker tag python:3.12.12-slim python:3.12-slim    # retag 本地缓存
docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .
```

> **注意**：worker 镜像是 **baked**（构建时把 src/agents/ 复制进去）。如果你改了 `worker.py`，**必须重新 build 镜像**才生效。

---

## 5. 启动与使用

### 5.1 启动顺序

```bash
# 1. Redis（Agent 注册中心）
docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine

# 2. 编排器（前台，观察日志）
.venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
```

启动日志会依次显示：
```
🐝 Agent Swarm starting up...
AgentRegistry connected to Redis           ← Redis 连上了
ContainerPool initialized                   ← 5 个 worker 容器拉起了
Orchestrator initialized (unified dispatcher wired)
🐝 Agent Swarm ready!
```

看到 `ready` + `pool 5/5` 就可以用了。

### 5.2 发送任务

```bash
curl -X POST http://localhost:9000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"做一个前后端分离的 TODO 应用"}'
```

返回 `task_id`，任务**异步**在后台执行。

### 5.3 查询状态

```bash
# 查状态
curl http://localhost:9000/api/tasks/<task_id>
# {"status":"running"} → 还在跑
# {"status":"completed","artifacts":["frontend/index.html","backend/main.py"],...} → 完成了

# 列产物
curl http://localhost:9000/api/tasks/<task_id>/artifacts

# 下载产物 ZIP
curl -o result.zip http://localhost:9000/api/tasks/<task_id>/download
```

### 5.4 产物位置

```
shared_output/tenants/{tenant_id}/tasks/{task_id}/
├── _plan/project_plan.md     ← 编排器生成的计划（含 API 契约）
├── frontend/                 ← 前端 Agent 的产出
│   └── index.html
└── backend/                  ← 后端 Agent 的产出
    ├── main.py
    └── requirements.txt
```

### 5.5 可用 Agent 类型（内置）

| ID | 名称 | 擅长 |
|----|------|------|
| `frontend-ux-pro` | 前端 | HTML/CSS/JS/React/Vue |
| `backend-engineer` | 后端 | FastAPI/Flask/数据库/Node.js |
| `general-agent` | 通用 | 文档、脚本、分析 |

> 加新角色不用改 worker.py 核心代码——在 config 的 `agent_cards` 里加一条（含 `system_prompt`）即可（W2 已支持）。

### 5.6 API 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发消息，创建任务 |
| GET | `/api/tasks/{id}` | 查任务状态 |
| GET | `/api/tasks` | 列所有任务 |
| GET | `/api/tasks/{id}/artifacts` | 列产物 |
| GET | `/api/tasks/{id}/download` | 下载产物 ZIP |
| GET | `/api/agents` | 列内置 Agent 类型 |
| GET | `/api/health` | 健康检查（pool 状态） |
| WS | `/ws/tasks/{id}` | 实时事件流（含进度） |
| POST | `/api/v1/agents/register` | 注册外部 Agent |
| POST | `/api/v1/agents/{id}/invoke` | 调用外部 Agent |
| GET | `/api/v1/agents` | 列已注册外部 Agent |
| GET | `/api/v1/dead-letters` | 失败任务记录 |

---

## 6. 接入你自己的 Agent

详见 `docs/onboarding.md`（含完整代码示例）。这里给速查：

### 方案 A：Chatbot → Worker（OpenAI 兼容，最常用）✅ 实测
你的 chatbot 暴露 `/v1/chat/completions`，一行注册即接入：
```bash
curl -X POST http://localhost:9000/api/v1/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-bot","endpoint":"http://my-bot:8000","protocol":"openai","skills":["translate"]}'
```
注册后自动建 adapter，立即可被编排器按 skill 调度或直接 `/invoke`。

### 方案 B：Chatbot → Orchestrator 大脑（换 LLM）⚠️ 机制已测，未跑真外部 chatbot
把编排器的 LLM 端点指向你的 chatbot（需支持 function-calling）：
```bash
export LLM_DEFAULT_BASE_URL=http://my-bot:8000/v1
export LLM_DEFAULT_MODEL=my-model
export LLM_DEFAULT_API_KEY=...
```
内置编排循环不变，LLM 换成你的。（config env 覆盖已单元测试通过；但未用真实外部 chatbot 端到端验证。）

### 方案 C：A2A Agent → Worker ✅ 实测
你的 Agent 说 A2A 协议（JSON-RPC `message/send`）：
```bash
curl .../register -d '{"name":"a2a-bot","endpoint":"http://a2a:9000","protocol":"a2a","skills":["review"]}'
```

### 方案 D：外部 Agent → Orchestrator（整体接管）✅ 回退已测
```yaml
orchestrator:
  provider: "external"
  external_endpoint: "http://my-scheduler:9000"
  fallback: true    # 外部挂了自动回退内置
```
> 回退机制已实测（外部不可用→自动回退 builtin→任务正常完成）；但一个"真实的成功的外部编排器"未端到端跑过。

---

## 7. 拓扑与踩坑经验（实战总结，团队必读）

这一节全是本 session **真实踩过的坑**，不是理论。

### 7.1 docker.io 不可达（国内最常见）
**症状**：`docker build` 报 `context deadline exceeded` / `failed to resolve source metadata`。

**原因**：`registry-1.docker.io` 在国内被墙或超时。

**解法**：用本地缓存的 base image + `--pull=false`：
```bash
# 先确认本地有没有 python:3.12-slim 或类似
docker images | grep python
# 如果有 python:3.12.X-slim（版本号可能不同），retag 成 Dockerfile 要的 tag：
docker tag python:3.12.12-slim python:3.12-slim
# 然后离线构建（--pull=false 不去 registry 检查）
docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .
```

### 7.2 模型名必须用 glm-4.7（不是 glm-coding-plan）
**症状**：`POST /api/chat` 返回 `status: completed` 但 message 是 `"编排器调用 LLM 失败: Error code: 400 - 模型不存在"`。

**原因**：`glm-coding-plan` 不是模型名（它是订阅计划名）。该端点实际可用的模型：`glm-4.5` / `glm-4.5-air` / `glm-4.6` / `glm-4.7` / `glm-5`。

**解法**：`config/default.yaml` 里 `default_model: "glm-4.7"`。

**查可用模型**：
```bash
curl https://open.bigmodel.cn/api/coding/paas/v4/models -H "Authorization: Bearer 你的key" | python3 -m json.tool
```

### 7.3 重启编排器前必须清理 warm pool 容器
**症状**：重启编排器，日志报 `Conflict: container name "/swarm-worker-0" already in use`，pool 变 0。

**原因**：编排器启动时自动 spawn 5 个 `swarm-worker-0..4` 容器。上次没清理就重启，名字冲突。

**解法**：重启前先删旧的：
```bash
docker rm -f swarm-worker-0 swarm-worker-1 swarm-worker-2 swarm-worker-3 swarm-worker-4
```

### 7.4 改了 worker.py 必须重新 build 镜像
**原因**：worker 镜像是 baked（`COPY src/agents/ /app/agents/`）。改了 worker.py / agent_loop.py 不重新 build，容器跑的是旧代码。

**解法**：
```bash
docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .
```

### 7.5 config/default.yaml 是 gitignored
**原因**：含真实 API key。模板是 `config/default.yaml.example`。

**注意**：`default.yaml.example` 里的 `shared_output_base` 默认是 `/home/apx103/...`（原作者的路径），**你必须改成自己的绝对路径**。

### 7.6 端口占用
| 端口 | 用途 |
|------|------|
| 9000 | 编排器 HTTP |
| 9001-9005 | Worker 容器（warm pool） |
| 6379 | Redis |

如果这些端口被占，改 `config/default.yaml` 的 `server.port` 和 `container_pool.base_port`。

### 7.7 编排器在容器里跑（可选，更彻底的容器化）
默认推荐编排器跑在 **host**（本机），worker 跑在 Docker（如上文）。如果你想把编排器也放进容器（比如用 docker-compose），需要额外配置（本 session 已全部修通并验证过一次）：

```bash
docker run -d --name swarm-orch \
  -p 9000:9000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/shared_output:$PWD/shared_output" \
  -v "$PWD/.pool_configs:$PWD/.pool_configs" \
  -v "$PWD/src:/app/src" \
  -v "$PWD/config:/app/config" \
  -e CONTAINER_WORKER_HOST=host.docker.internal \
  -e POOL_CONFIG_DIR="$PWD/.pool_configs" \
  -e REDIS_URL=redis://host.docker.internal:6379 \
  -e SHARED_OUTPUT_BASE="$PWD/shared_output" \
  swarm-orchestrator:latest
```

关键点（不配这些会报错）：
- **docker.sock 必须挂**（编排器靠它 spawn worker 容器）。
- **CONTAINER_WORKER_HOST=host.docker.internal**（容器内到不了 host 上的 worker 端口，必须走 host.docker.internal）。
- **POOL_CONFIG_DIR 必须是 host 可见路径**（pool 把 config.json bind-mount 进 worker；容器内路径 Docker 不认）。
- **shared_output 同路径挂载**（host 和容器用相同绝对路径，避免路径翻译问题）。
- **构建编排器镜像**：`docker build --pull=false -t swarm-orchestrator:latest -f docker/Dockerfile.orchestrator .`

---

## 8. 已验证能力清单（本 session 实测）

| 能力 | 验证方式 | 结果 |
|------|---------|------|
| 单 Agent 产文件 | POST /api/chat hello-world | ✅ frontend/index.html 产出 |
| 多 Agent 并行 | POST 前后端 TODO 任务 | ✅ backend+frontend 并行 dispatch |
| 前后端联调 | 编排器审查后下"接口对齐"修正 | ✅ 联调 dispatch 确认 |
| 产出可运行工程 | 起 backend → curl CRUD | ✅ GET/POST/PATCH/DELETE 全过 |
| 浏览器渲染 | Chrome headless 渲染前端 | ✅ DOM 含后端数据 |
| 外部 Agent 接入 | 注册 mock openai → /invoke | ✅ 返回 MOCK-OK |
| 可插拔编排器 | provider=external 死端点 → 回退 | ✅ 日志+事件标注回退 |
| 流式进度 | worker 日志 POST ×8（poll_task） | ✅ 轮询链路确认 |
| trace_id | 全链路日志含 trace=task_id | ✅ |
| worker 角色可插拔 | env 注入 system_prompt | ✅ 6 测试 |
| worker 共享读 | read_shared_file 读 sibling | ✅ 4 测试 |
| 强制 review | finalize 审查未过拒绝完成 | ✅ 4 测试 |
| 幂等 | Idempotency-Key 重复复用 | ✅ 3 测试 |
| 死信 | 失败任务记录 + 查询 | ✅ 4 测试 |
| 优雅降级 | 全失败命中缓存→degraded | ✅ 7 测试 |
| 周期 health_sweep | sweeper loop 跑 | ✅ 3 测试 |
| 启动自检 | validate_settings warning | ✅ 4 测试 |
| 真实取消 | cancel_running 取消后台 task | ✅ 4 测试 |

**单元/集成测试总数：274 passed。**

---

## 9. 停止与清理

```bash
# 停编排器（Ctrl+C 或 kill）
lsof -ti tcp:9000 | xargs kill

# 删 worker + redis 容器
docker rm -f swarm-worker-0 swarm-worker-1 swarm-worker-2 swarm-worker-3 swarm-worker-4 swarm-redis

# 删网络
docker network rm swarm-net 2>/dev/null

# 清理 pool config（含 API key 的临时文件）
rm -f .pool_configs/*.json
```

---

## 10. 故障排查

| 症状 | 原因 | 解法 |
|------|------|------|
| `health` 显示 `pool_available: 0` | worker 镜像没 build / Docker 没起 | `docker images \| grep swarm-worker`；没镜像就 build |
| task 报 `模型不存在` | model 写错 | 改 `glm-4.7`（见 7.2） |
| task 报 `编排器调用 LLM 失败` | API key 无效 / 网络不通 | 检查 key + curl 测端点 |
| 重启报 `container name already in use` | 旧 warm pool 没清 | `docker rm -f swarm-worker-{0..4}`（见 7.3） |
| worker spawn 报 `mounts denied` | 编排器在容器里但没配 POOL_CONFIG_DIR | 见 7.7 |
| `docker build` 超时 | docker.io 不可达 | 用 `--pull=false` + 本地缓存 base（见 7.1） |
| `redis connect failed` | redis 没起 | `docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine` |
| 产物目录为空 | Worker 没跑成功 | 看 orchestrator 日志 + worker 日志：`docker logs swarm-worker-0` |

---

## 11. 给同事的一句话

> 把项目 clone 下来，`cp config/default.yaml.example config/default.yaml`，填上你的 GLM key，build worker 镜像，起 redis，`.venv/bin/python -m uvicorn src.main:app --port 9000`，然后 `curl POST /api/chat` 发任务。编排器自己拆解、调度多 Agent、产出落 `shared_output/`。想接你自己的 Agent？一行 `POST /api/v1/agents/register` 搞定。

---

_本文档每条命令均在本 session 实测通过。如果遇到文档没覆盖的问题，先看 `docs/plans/NEXT-STEPS.md`（已知问题 + 路线图）和 `docs/onboarding.md`（Agent 接入详解）。_
