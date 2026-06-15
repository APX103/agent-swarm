# 🐝 Agent Swarm

基于 Docker 的多 Agent 协作系统，使用 A2A 协议实现 Agent 间通信。

## 架构

```
┌─────────────────────────────────────────────────┐
│  FastAPI Server (:9000)                          │
│  ┌───────────────────────────────────────────┐  │
│  │  Orchestrator Agent (GLM Coding Plan)     │  │
│  │  - 分析用户请求                              │  │
│  │  - 选择 Agent 类型                           │  │
│  │  - 通过 A2A 协议分发任务                      │  │
│  │  - 监控进度，汇总结果                         │  │
│  └──────────┬────────────────────┬────────────┘  │
│             │ A2A Protocol       │                │
│  ┌──────────▼────────┐ ┌────────▼───────────┐  │
│  │ Worker Container 1 │ │ Worker Container 2  │  │
│  │ Frontend UX Pro   │ │ Backend Engineer    │  │
│  │ (port 9001)       │ │ (port 9002)         │  │
│  └────────────────────┘ └────────────────────┘  │
│             │                    │                │
│             ▼                    ▼                │
│  ┌───────────────────────────────────────────┐  │
│  │  Shared Storage (shared_output/)           │  │
│  │  tenants/{tenant_id}/tasks/{task_id}/      │  │
│  │  ├── frontend/                             │  │
│  │  ├── backend/                              │  │
│  │  └── _final/                               │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

## 快速开始

### 1. 构建 Worker 镜像

```bash
cd /home/apx103/work/swarm
docker build -t agent-swarm-worker:latest -f docker/Dockerfile.worker .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 LLM API Key
```

### 3. 启动服务

```bash
# 直接启动（需要 Docker）
cd /home/apx103/work/swarm
source .venv/bin/activate
python -m uvicorn src.main:app --host 0.0.0.0 --port 9000

# 或使用 docker-compose
docker-compose up -d
```

### 4. （可选）起 10 个示例 Agent（mock，用于 demo/联调）

```bash
# 起 10 个 mock A2A agent（端口 9001-9010），无需真实容器/LLM
python agents/mock-a2a-worker.py --start-port 9001 --count 10
```

### 5. 打开 Web UI（双模式：Copilot + 直聊）

浏览器访问 `http://localhost:9000/ui`：
- **🧭 Copilot 模式**：和编排器对话，自动拆解任务、分派多个 Agent、汇总产物。
- **💬 直聊模式**：从左侧 Agent 名册选一个，1:1 直接对话，支持流式进度。

> 前后端分离：UI 是纯静态文件（`web/`），也可部署到任意位置——在左下角
> 配置后端地址即可，CORS 已开 `*`。

### 6. 或用 API 发送任务

```bash
# Copilot 模式（编排器自动调度）
curl -X POST http://localhost:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我写一个前后端分离的待办事项应用"}'

# 直聊模式（直接调某个 Agent，带 session 支持多轮 + 流式）
curl -X POST http://localhost:9000/api/v1/agents/<agent_id>/invoke \
  -H "Content-Type: application/json" \
  -d '{"task": "帮我写个登录页", "session_id": "my-sess"}'
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | Copilot 模式：发送消息，编排器自动拆解+分派+汇总 |
| POST | `/api/v1/agents/{id}/invoke` | 直聊模式：直接调指定 Agent（带 `session_id` 则有 session/流式/产物） |
| POST | `/api/v1/agents/register` | 动态注册外部 Agent（openai/cli/mcp/a2a/http） |
| GET | `/api/v1/agents` | 列出已注册的外部 Agent（直聊候选） |
| GET | `/api/agents` | 列出内置 Agent 角色（Copilot 可调度） |
| GET | `/api/tasks/{id}` | 查询任务状态 |
| GET | `/api/tasks` | 列出所有任务 |
| GET | `/api/tasks/{id}/artifacts` | 列出任务产物 |
| GET | `/api/tasks/{id}/artifacts/{path}` | 读取单个产物文件（前端预览用） |
| GET | `/api/tasks/{id}/download` | 下载产物 ZIP 包 |
| GET | `/api/sessions/{id}/events` | 会话事件审计流（state + events） |
| GET | `/api/v1/metrics` | 调度指标快照 |
| GET | `/api/v1/dead-letters` | 失败任务死信队列 |
| GET | `/api/health` | 健康检查 |
| WS | `/ws/tasks/{id}` | WebSocket 实时事件（进度/工具/取消） |

## 示例 Agent（通用软件团队 10 角色）

启动时自动从 `agents/*.yaml` 注册，全部走 A2A 协议：

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

## 项目结构

```
swarm/
├── agents/                  # 声明式 Agent 注册（启动时自动加载）
│   ├── *.yaml               # 10 个示例 A2A Agent（通用软件团队角色）
│   ├── mock-a2a-worker.py   # mock A2A server（demo/联调用，无需真实容器）
│   └── README.md            # 如何接真实容器
├── web/                     # 前端 UI（纯静态，前后端分离）
│   ├── index.html           # 双模式控制台（Copilot + 直聊）
│   ├── app.js               # 名册/对话/流式/产物预览
│   └── style.css
├── config/                  # 配置文件
│   └── default.yaml         # 默认配置（LLM、容器池、Agent 定义、dispatcher）
├── docker/                  # Docker 相关
│   ├── Dockerfile.worker    # Worker 容器镜像
│   ├── Dockerfile.orchestrator
│   └── entrypoint.sh        # Worker 入口脚本
├── shared_output/           # 共享产物存储 + swarm.db（SQLite）
├── src/                     # 源代码
│   ├── main.py              # FastAPI 入口（lifespan 装配 + UI 静态挂载）
│   ├── config.py            # 全局配置加载
│   ├── api/                 # 用户 API 层
│   │   ├── routes.py        # /api/chat + 任务/产物/取消 + 单文件预览
│   │   ├── models.py        # Pydantic 模型
│   │   └── websocket.py     # WS 连接管理
│   ├── gateway/             # 外部 Agent 网关（注册 + 直聊 invoke）
│   │   └── routes.py        # /api/v1/agents/* + 直聊增强
│   ├── orchestrator/        # 编排器（可插拔）
│   ├── dispatcher/          # 统一调度（多候选/重试/熔断/流式）
│   ├── adapters/            # 协议适配器（openai/cli/mcp/a2a，a2a 支持流式）
│   ├── registry/            # Redis Agent 注册中心
│   ├── session/             # 会话（async SessionService + SessionManager）
│   ├── task_manager/        # 任务生命周期 + 产物
│   ├── container_pool/      # Docker warm 容器池
│   ├── observability/       # trace_id + metrics
│   ├── reliability/         # 死信队列
│   └── common/              # a2a_client 等
├── tests/                   # 测试（324 passed，含 A2A 流式/直聊/failover/cancel-event E2E）
├── .env.example
├── requirements.txt
├── pyproject.toml
└── docker-compose.yml
```

## 测试

```bash
source .venv/bin/activate
pytest tests/ -v
```

当前状态：**324 tests passed**

## 技术栈

- **LLM**: OpenAI-compatible API（默认 GLM Coding Plan）
- **协议**: A2A（Agent-to-Agent）Protocol
- **容器**: Docker（预启动 warm pool）
- **API**: FastAPI + Uvicorn
- **通信**: A2A JSON-RPC over HTTP + WebSocket
