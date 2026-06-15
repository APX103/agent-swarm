# Agent Swarm 部署指南

> 本文档讲清楚：怎么把 Swarm 从开发机搬到一台服务器跑起来，让团队通过浏览器用。
> 两种部署方式，按你的规模选一种就行。

---

## 先理解：部署后是什么样子

```
┌─────────────────────────────────────────────────────┐
│  你的服务器（一台机器）                                │
│                                                      │
│  ┌─────────────┐   ┌──────────┐   ┌──────────────┐ │
│  │ Swarm 后端   │──▶│  Redis   │   │ Agent 容器群  │ │
│  │ :9000       │   │ :6379    │   │ :9001~9010   │ │
│  │ (FastAPI)   │   │ (注册中心) │   │ (你们团队的   │ │
│  │ + web/ 静态 │   │          │   │  Agent 服务)  │ │
│  └─────┬───────┘   └──────────┘   └──────────────┘ │
│        │                                             │
│        │ shared_output/  (产物盘，所有组件共享)        │
│        ▼                                             │
│  ┌─────────────┐                                    │
│  │  LLM API    │  (GLM/OpenAI，外网)                 │
│  └─────────────┘                                    │
└─────────────────────────────────────────────────────┘
        ▲
        │ 浏览器访问 http://服务器IP:9000/ui
   团队成员
```

**三个角色：**
1. **Swarm 后端**（本仓库）—— 编排器 + API + Web UI，1 个进程。
2. **Redis** —— Agent 注册中心，1 个容器。
3. **Agent 容器群** —— 你们团队开发的各个 Agent，每个一个容器，常驻运行。

---

## 前置要求

- 一台 Linux 服务器（4 核 8G 起步，跑 10 个 Agent 建议 8 核 16G）。
- 已装 **Docker** 和 **Docker Compose**（`docker --version` 能出版本号）。
- 能访问 LLM API（GLM / OpenAI 兼容端点）。
- 你团队的 Agent 容器镜像（每个 Agent 一个，监听一个端口，实现 A2A 协议）。

> 不懂 A2A 协议怎么实现？看 `agents/README.md` 和 `agents/mock-a2a-worker.py`——
> 后者是一个完整的 A2A server 示例，照着改就行。

---

## 方式一：Docker Compose 一键部署（推荐，最省心）

所有东西都跑在容器里，一条命令起停。

### 步骤 1：把代码放到服务器

```bash
# 在服务器上
git clone <你的仓库地址> swarm
cd swarm
```

### 步骤 2：配置环境变量

```bash
cp .env.example .env
vi .env
```

**必须改的：**
```bash
LLM_DEFAULT_API_KEY=你的真实key          # 最关键，没这个编排器跑不动
LLM_DEFAULT_MODEL=glm-4.7               # 用 coding/paas 端点支持的模型名
LLM_DEFAULT_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
```

**按需改的：**
```bash
SWARM_API_KEY=设一个内网访问密钥          # 保护 /api/v1/ 路由，防误调用
CONTAINER_POOL_SIZE=5                    # Docker worker 池大小（你们用常驻容器的话不太用得上）
```

### 步骤 3：编辑 `docker-compose.yml`，补上 Redis 和 Agent 容器

仓库里的 `docker-compose.yml` 只有后端，需要加 Redis + 你们 Agent。改成这样：

```yaml
version: "3.9"
services:
  # ── Swarm 后端（编排器 + API + Web UI）──────────────
  swarm:
    build: { context: ., dockerfile: docker/Dockerfile.orchestrator }
    ports: ["9000:9000"]
    volumes:
      - ./shared_output:/workspace/shared_output
      - ./agents:/app/agents          # 声明式 Agent 配置（启动自动注册）
      - ./web:/app/web                # Web UI 静态文件
      - /var/run/docker.sock:/var/run/docker.sock   # 要管 Docker 容器的话
    environment:
      - LLM_DEFAULT_MODEL=${LLM_DEFAULT_MODEL}
      - LLM_DEFAULT_BASE_URL=${LLM_DEFAULT_BASE_URL}
      - LLM_DEFAULT_API_KEY=${LLM_DEFAULT_API_KEY}
      - SHARED_OUTPUT_BASE=/workspace/shared_output
      - REDIS_URL=redis://redis:6379
      - SWARM_API_KEY=${SWARM_API_KEY:-}
    depends_on: [redis]
    restart: unless-stopped

  # ── Redis（Agent 注册中心）──────────────────────────
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    restart: unless-stopped

  # ── 你们团队的 Agent 容器（每个一个 service）─────────
  # 照这个模板复制 N 个，改镜像名、端口、环境变量
  frontend-engineer:
    image: your-team/frontend-agent:latest
    ports: ["9001:9001"]
    volumes:
      - ./shared_output:/workspace/artifacts   # 共享产物盘
    environment:
      - LLM_API_KEY=${LLM_DEFAULT_API_KEY}
    restart: unless-stopped

  backend-engineer:
    image: your-team/backend-agent:latest
    ports: ["9002:9001"]
    volumes:
      - ./shared_output:/workspace/artifacts
    environment:
      - LLM_API_KEY=${LLM_DEFAULT_API_KEY}
    restart: unless-stopped
  # ... 照此复制到 10 个
```

> ⚠️ `Dockerfile.orchestrator` 要补两行 COPY（把 agents/ 和 web/ 打进镜像），
> 或者像上面那样用 volume 挂载（更灵活，改配置不用重 build）。推荐 volume 挂载。

### 步骤 4：声明每个 Agent 的连接信息

编辑 `agents/*.yaml`，把 `endpoint` 改成**服务器上 Agent 容器的真实地址**：

```yaml
# agents/frontend-engineer.yaml
name: Frontend Engineer
endpoint: http://frontend-engineer:9001     # docker-compose 内用 service 名
# 如果 Agent 和 Swarm 不在同一个 docker network，用服务器 IP:
# endpoint: http://192.168.1.100:9001
protocol: a2a
skills: [frontend-engineer]
```

Swarm 启动时会自动读这些文件，把每个 Agent 注册进 Redis。

### 步骤 5：起服务

```bash
docker-compose up -d --build
```

看日志确认起来了：
```bash
docker-compose logs -f swarm
# 应该看到 "🐝 Agent Swarm ready!" + "Registered declared agent: Frontend Engineer" ×10
```

### 步骤 6：验证

```bash
# 健康检查
curl http://localhost:9000/api/health

# 列出已注册的 Agent（应该看到你的 10 个）
curl http://localhost:9000/api/v1/agents

# 浏览器打开
open http://你的服务器IP:9000/ui
```

**完成。** 团队成员浏览器访问 `http://服务器IP:9000/ui` 就能用了。

### 日常运维命令

```bash
docker-compose logs -f swarm        # 看后端日志
docker-compose restart swarm        # 重启后端
docker-compose down                 # 停所有服务
docker-compose up -d                # 起所有服务
docker-compose up -d --build swarm  # 改了代码后重新构建后端
```

---

## 方式二：裸机部署（后端跑宿主机，Agent 跑容器）

适合开发调试，或者服务器资源紧张不想再套一层 Docker。

### 步骤 1：装 Python 环境

```bash
cd swarm
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-orchestrator.txt
```

### 步骤 2：起 Redis（用 Docker 最快）

```bash
./start-redis.sh
# 或手动：docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine
```

### 步骤 3：配 `.env`

同方式一步骤 2。

### 步骤 4：起 Agent 容器

你们团队的每个 Agent 容器各自起起来（docker run 或你们的部署脚本），确保端口和 `agents/*.yaml` 里的 endpoint 对得上。

### 步骤 5：起 Swarm 后端

```bash
./start-orchestrator.sh
# 或手动：python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
```

### 步骤 6：验证

同方式一步骤 6。

---

## 方式三：纯 mock 模式（demo / 演示用，最快）

不需要真实 Agent 容器，用自带的 mock server 演示完整链路。

```bash
# 1. 起 Redis
./start-redis.sh

# 2. 起 10 个 mock Agent（端口 9001-9010）
python agents/mock-a2a-worker.py --start-port 9001 --count 10

# 3. 起 Swarm 后端
./start-orchestrator.sh

# 4. 浏览器打开 http://localhost:9000/ui
```

适合给老板/客户演示，5 分钟搞定。**这不是生产部署。**

---

## 防火墙 / 网络

只需对外暴露 **1 个端口**：
- `9000`（Swarm 后端 + Web UI）

**不需要对外暴露** Redis(6379) 和 Agent 容器端口(9001~)——它们走内网通信即可。
生产环境建议：
- 把 9000 放到 Nginx/Caddy 后面，加 HTTPS。
- Redis 和 Agent 端口只开内网。

```bash
# UFW 示例
sudo ufw allow 9000/tcp        # 对外
sudo ufw deny 6379/tcp         # 只内网
```

---

## 常见问题

**Q: 启动后 `/api/v1/agents` 返回空？**
A: Agent 容器没起来，或 `agents/*.yaml` 的 endpoint 不对。先 `curl http://agent-host:port/.well-known/agent.json` 确认 Agent 活着。

**Q: 编排器报 "No candidates for agent_type"？**
A: Agent 注册时 `skills` 字段没填对。Copilot 按 skill 分派，`skills` 里的值要和编排器用的 agent_type 完全一致。

**Q: 直聊模式按钮灰的？**
A: `/api/v1/agents`（外部注册的 Agent）为空。直聊只对**已注册的外部 Agent**开放，内置 card（`/api/agents`）不能直聊。

**Q: 取消任务后 Agent 还在跑？**
A: 常驻容器模式下，Swarm 通过 A2A `tasks/cancel` 通知 Agent。确认你们的 Agent 实现了这个方法（mock-a2a-worker.py 有示例）。

**Q: 多人同时用会冲突吗？**
A: 不会。每个请求独立 session + task。但并发受 `dispatcher.max_concurrent`（默认 8）限制，超了会排队。

**Q: 怎么更新某个 Agent？**
A: 重新 build 它的镜像，`docker-compose up -d <agent-service>`。Swarm 不用重启，Agent 容器重启后会重新心跳注册。

---

## 部署 Checklist

跑生产前过一遍：

- [ ] `.env` 里的 `LLM_DEFAULT_API_KEY` 是真实可用的
- [ ] `agents/*.yaml` 的 endpoint 指向真实 Agent 容器地址
- [ ] 每个 Agent 容器都实现了 `GET /.well-known/agent.json` + `POST /`（A2A）
- [ ] `shared_output/` 目录所有组件都能读写（volume 挂载对齐）
- [ ] Redis 起来了且 Swarm 能连上（`docker-compose logs swarm` 无 Redis 报错）
- [ ] 浏览器能打开 `/ui` 且左上角"已连接"
- [ ] 发一个简单任务（如"写个 hello world"）能跑通，看到产物
- [ ] 设了 `SWARM_API_KEY`（如果对外暴露的话）
- [ ] Redis/Agent 端口没对外暴露
