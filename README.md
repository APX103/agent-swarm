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

### 4. 发送任务

```bash
curl -X POST http://localhost:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我写一个前后端分离的待办事项应用"}'
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发送消息，创建并执行任务 |
| GET | `/api/tasks/{id}` | 查询任务状态 |
| GET | `/api/tasks` | 列出所有任务 |
| GET | `/api/tasks/{id}/artifacts` | 列出任务产物 |
| GET | `/api/tasks/{id}/download` | 下载产物 ZIP 包 |
| GET | `/api/agents` | 列出可用 Agent 类型 |
| GET | `/api/health` | 健康检查 |
| WS | `/ws/tasks/{id}` | WebSocket 实时事件 |

## 可用 Agent 类型

| ID | 名称 | 技能 |
|----|------|------|
| `frontend-ux-pro` | Frontend UX Pro | UI/UX 设计、React/Vue、HTML+CSS+JS |
| `backend-engineer` | Backend Engineer | API 设计、数据库、Python/Node.js |
| `general-agent` | General Agent | 通用编程和文本处理 |

## 项目结构

```
swarm/
├── config/                  # 配置文件
│   └── default.yaml         # 默认配置（LLM、容器池、Agent 定义）
├── design/                  # 设计文档（HTML）
│   ├── 01-architecture.html
│   └── 02-multi-tenant.html
├── docker/                  # Docker 相关
│   ├── Dockerfile.worker    # Worker 容器镜像
│   ├── Dockerfile.orchestrator  # Orchestrator 容器镜像
│   └── entrypoint.sh        # Worker 入口脚本
├── docs/                    # 调研文档
│   └── index.html
├── research/                # 技术调研（HTML）
│   ├── 01-a2a-protocol.html
│   ├── 02-framework-comparison.html
│   └── 03-docker-sandbox-architecture.html
├── shared_output/           # 共享产物存储
├── src/                     # 源代码
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # 全局配置加载
│   ├── api/                 # API 层
│   │   ├── routes.py        # REST + WebSocket 路由
│   │   ├── models.py        # Pydantic 模型
│   │   └── websocket.py     # WS 连接管理
│   ├── orchestrator/        # 编排器
│   │   └── orchestrator.py  # Orchestrator Agent（tool-calling 循环）
│   ├── container_pool/      # 容器池
│   │   └── pool.py          # 预启动 Docker 容器池管理
│   ├── task_manager/        # 任务管理
│   │   └── manager.py       # 任务生命周期 + 产物收集
│   ├── common/              # 公共组件
│   │   └── a2a_client.py    # A2A 协议客户端
│   └── agents/              # Worker Agent
│       └── worker.py        # Agent Server（A2A 端点 + LLM + 文件工具）
├── tests/                   # 测试
│   ├── test_api.py          # API 路由测试
│   ├── test_a2a.py          # A2A 客户端测试
│   ├── test_config.py       # 配置加载测试
│   ├── test_container_pool.py  # 容器池测试
│   ├── test_orchestrator.py    # 编排器测试
│   ├── test_task_manager.py    # 任务管理测试
│   └── test_integration.py     # 集成测试
├── .env.example             # 环境变量模板
├── requirements.txt         # Worker 依赖
├── requirements-orchestrator.txt  # Orchestrator 依赖
├── pyproject.toml           # Python 项目配置
└── docker-compose.yml       # Docker Compose 编排
```

## 测试

```bash
source .venv/bin/activate
pytest tests/ -v
```

当前状态：**38 tests passed**

## 技术栈

- **LLM**: OpenAI-compatible API（默认 GLM Coding Plan）
- **协议**: A2A（Agent-to-Agent）Protocol
- **容器**: Docker（预启动 warm pool）
- **API**: FastAPI + Uvicorn
- **通信**: A2A JSON-RPC over HTTP + WebSocket
