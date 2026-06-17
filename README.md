# 🐝 Agent Swarm

基于 Docker 的多 Agent 协作系统，使用 A2A 协议实现 Agent 间通信。
支持内置编排器（GLM tool-calling）和外部编排器（如 eino agent）两种模式。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  Swarm 后端 (FastAPI :9000)                               │
│                                                           │
│  ┌─────────────────────────────────────────────────┐     │
│  │  Orchestrator（可插拔）                           │     │
│  │  builtin: GLM tool-calling 循环                  │     │
│  │  external: eino / 其他 A2A 编排器                 │     │
│  │  → Dispatcher（重试/熔断/流式/多候选）             │     │
│  └──────┬──────────────────────┬────────────────────┘     │
│         │ A2A                  │ A2A                      │
│  ┌──────▼──────┐    ┌──────────▼──────────┐              │
│  │ Docker Pool │    │ ExternalAgentBackend │              │
│  │ warm 容器×5  │    │ (registry 发现)      │              │
│  │ :9001~9005  │    │ openai/cli/mcp/a2a  │              │
│  └──────┬──────┘    └──────────┬──────────┘              │
│         │                      │                          │
│         ▼                      ▼                          │
│  ┌───────────────────────────────────────────────┐       │
│  │  Redis（Agent 注册中心 + 心跳 + 发现）          │       │
│  └───────────────────────────────────────────────┘       │
│  ┌───────────────────────────────────────────────┐       │
│  │  Shared Storage（产物盘 + SQLite）              │       │
│  │  shared_output/tenants/{tenant}/sessions/{sid}/│       │
│  └───────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────┘
        ▲                                    ▲
        │ HTTP + WebSocket                   │ A2A JSON-RPC
   ┌────┴────┐                        ┌──────┴──────┐
   │ Web UI  │                        │  LLM API    │
   │ /ui     │                        │ GLM/OpenAI  │
   │ Copilot │                        └─────────────┘
   │ + 直聊   │
   └─────────┘
```

## 快速开始

### 前置条件

- **Python 3.12+**
- **Docker & Docker Compose**（用于 worker 容器池）
- **Redis**（Docker Compose 会自动启动；本地开发需自行启动）

### 方式一：Docker Compose（推荐，一键启动）

```bash
cp .env.example .env   # 填入 LLM API Key
docker compose up --build
```

启动后访问：
- Dashboard：`http://localhost:9000/dashboard/`
- API：`http://localhost:9000/api/...`

### 方式二：本地开发

#### 1. 克隆并安装依赖

```bash
cd /your/path/swarm
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

> 项目已附带 `.venv` 时可直接 `source .venv/bin/activate && pip install -e ".[dev]"` 更新依赖。

#### 2. 构建 Worker 镜像

```bash
docker build -t swarm-worker:latest -f docker/Dockerfile.worker .
```

#### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 LLM API Key + Redis 地址
```

**必须配置的：**

```bash
LLM_DEFAULT_API_KEY=你的真实key
LLM_DEFAULT_MODEL=glm-coding-plan
LLM_DEFAULT_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
REDIS_URL=redis://localhost:6379
```

#### 4. 启动 Redis

```bash
docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine
```

#### 5. 启动 Swarm 后端

```bash
source .venv/bin/activate
python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
```

或使用脚本：

```bash
./start-orchestrator.sh
```

**使用外部编排器（如 eino-agent）：**

```bash
ORCHESTRATOR_PROVIDER=external \
ORCHESTRATOR_EXTERNAL_ENDPOINT=http://localhost:9030 \
python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
```

#### 6. 打开 Web UI

浏览器访问 `http://localhost:9000/ui`：
- **🧭 Copilot 模式**：和编排器对话，自动拆解任务、分派多个 Agent、汇总产物
- **💬 直聊模式**：从左侧 Agent 名册选一个，1:1 直接对话，支持流式进度

**监控台**：`http://localhost:9000/dashboard/` — 查看 Agent 在线状态、Session 事件链、任务列表

#### 7. （可选）起 mock Agent 做 demo

```bash
python agents/mock-a2a-worker.py --start-port 9001 --count 10
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | Copilot 模式：编排器自动拆解+分派+汇总 |
| POST | `/api/v1/agents/{id}/invoke` | 直聊模式：直接调指定 Agent（带 `session_id` 支持 session/流式） |
| POST | `/api/internal/dispatch` | 内部调度（外部编排器调 Swarm 调度子任务） |
| POST | `/api/v1/agents/register` | 动态注册外部 Agent（openai/cli/mcp/a2a） |
| GET | `/api/agents` | 列出 Agent（实时在线状态 + endpoint，从 registry 读） |
| GET | `/api/v1/agents` | 列出已注册的外部 Agent |
| GET | `/api/tasks/{id}` | 查询任务状态 |
| GET | `/api/tasks` | 列出所有任务 |
| GET | `/api/tasks/{id}/artifacts` | 列出任务产物 |
| GET | `/api/tasks/{id}/artifacts/{path}` | 读取单个产物文件（前端预览用） |
| GET | `/api/tasks/{id}/download` | 下载产物 ZIP 包 |
| GET | `/api/sessions/{id}/events` | 会话事件审计流（state + events） |
| GET | `/api/sessions?limit=N` | 列出最近 N 个 session |
| GET | `/api/v1/metrics` | 调度指标快照 |
| GET | `/api/v1/dead-letters` | 失败任务死信队列 |
| GET | `/api/health` | 健康检查（容器池状态） |
| GET | `/api/dashboard/config` | Dashboard 配置（标题、刷新间隔） |
| WS | `/ws/tasks/{id}` | WebSocket 实时事件（进度/工具调用/取消） |

## 示例 Agent（通用软件团队 10 角色）

启动时自动从 `agents/*.yaml` 注册到 Redis（同 endpoint 去重，重启不产生重复）：

| 角色 ID | 职责 |
|----|------|
| `frontend-engineer` | React/Vue/TypeScript、响应式 UI、无障碍 |
| `backend-engineer` | API 设计、数据库、Python/FastAPI/Node.js |
| `fullstack-engineer` | 端到端全栈交付 |
| `devops-engineer` | Docker/K8s、CI/CD、部署监控 |
| `qa-engineer` | 单元/集成/E2E 测试、质量保障 |
| `security-engineer` | 漏洞评估、鉴权、安全编码审查 |
| `data-engineer` | 数据管道、ETL、SQL、分析脚本 |
| `mobile-engineer` | React Native/Flutter、iOS/Android |
| `tech-writer` | README/API 文档、架构说明 |
| `design-reviewer` | UI/UX 审查、可用性评估 |

> 接你们团队的真实 Agent：把 `agents/*.yaml` 的 `endpoint` 改成容器真实地址即可。
> 详见 `agents/README.md`。

## 编排器（可插拔）

| 模式 | 配置 | 说明 |
|------|------|------|
| `builtin`（默认） | `ORCHESTRATOR_PROVIDER=builtin` | GLM tool-calling 循环：plan→dispatch→review→finalize |
| `external` | `ORCHESTRATOR_PROVIDER=external` + `ORCHESTRATOR_EXTERNAL_ENDPOINT=...` | 外部 A2A 编排器（如 eino-agent），支持流式+多轮记忆+session 事件记录 |

外部编排器通过 `POST /api/internal/dispatch` 调度 Swarm 的子任务到 worker。

## 项目结构

```
swarm/
├── agents/                  # 声明式 Agent 注册（启动时自动加载，endpoint 去重）
│   ├── *.yaml               # 10 个示例 A2A Agent
│   ├── mock-a2a-worker.py   # mock A2A server（demo 用）
│   └── README.md
├── web/                     # 聊天 UI（纯静态，Copilot + 直聊双模式）
├── dashboard/               # 监控台（Agent 状态/Session 事件链/任务列表）
├── config/                  # 配置（default.yaml.example 是模板，default.yaml 被 gitignore）
├── docker/                  # Dockerfile.worker + Dockerfile.orchestrator + entrypoint.sh
├── docs/                    # 文档
│   ├── DEPLOYMENT.md        # 部署指南
│   ├── E2E-WALKTHROUGH.md   # 全量操作手册
│   ├── architecture-audit.html  # 架构审计报告
│   └── eino-integration-notes.md
├── shared_output/           # 产物盘 + SQLite（gitignored）
├── src/                     # 源代码
│   ├── main.py              # FastAPI 入口（lifespan 装配 + UI/dashboard 挂载）
│   ├── config.py            # 全局配置（支持环境变量覆盖）
│   ├── api/                 # 用户 API（/api/chat, tasks, sessions, health）
│   ├── gateway/             # 外部 Agent 网关（register + 直聊 invoke）
│   ├── orchestrator/        # 编排器（builtin/external/resolver 可插拔）
│   ├── dispatcher/          # 统一调度（多候选/重试/熔断/流式/结果缓存）
│   ├── adapters/            # 协议适配器（openai/cli/mcp/a2a，a2a 支持流式）
│   ├── registry/            # Redis Agent 注册中心（endpoint 去重 + 心跳 TTL）
│   ├── session/             # 会话（async SessionService + SessionManager，per-session 锁）
│   ├── task_manager/        # 任务生命周期 + 产物
│   ├── container_pool/      # Docker warm 容器池（DEAD 标记 + 归还清理）
│   ├── observability/       # trace_id + metrics
│   ├── reliability/         # 死信队列 + 熔断器
│   └── common/              # a2a_client 等
├── tests/                   # 测试（348 passed）
├── .env.example
├── pyproject.toml
└── docker-compose.yml
```

## 测试

```bash
source .venv/bin/activate
pytest tests/ -v
```

当前状态：**348 tests passed**

## 技术栈

- **LLM**: OpenAI-compatible API（默认 GLM glm-4.7）
- **协议**: A2A（Agent-to-Agent）JSON-RPC over HTTP
- **容器**: Docker（warm pool，per-request SHARED_DIR 隔离）
- **后端**: Python 3.12+ / FastAPI / Uvicorn / SQLite (WAL)
- **注册中心**: Redis 7
- **前端**: 纯静态 HTML/JS（web/ 聊天 UI + dashboard/ 监控台）
- **外部编排器**: Go + eino 框架 + go-zero（独立项目 work/eino-agent）
