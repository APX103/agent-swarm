# Agent Swarm — 全量 E2E 操作手册

> 给团队成员的操作指南。照着做，从零启动到拿到可运行的生成工程。
> 预计耗时：30 分钟（含构建镜像）。

---

## 0. 你需要什么

- macOS 或 Linux
- Docker（`docker --version` 能出版本号）
- Python 3.12+（`python3 --version`）
- Go 1.21+（如果要跑 eino-agent）（`go version`）
- 网络能访问 `https://open.bigmodel.cn`（GLM API）

---

## 1. 拿到代码

```bash
git clone https://github.com/APX103/agent-swarm.git swarm
cd swarm
```

eino-agent（可选，如果你想用外部编排器）：
```bash
cd /your/work/dir
# eino-agent 在本地，拷过来或从内部仓库拉
# 假设你已有 eino-agent 目录
```

---

## 2. 准备 Python 环境

```bash
cd swarm
python3.12 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-orchestrator.txt
pip install pytest pytest-asyncio respx   # 测试用
```

验证：
```bash
python -c "import fastapi, uvicorn, redis, openai, docker; print('deps OK')"
```

---

## 3. 构建 Worker Docker 镜像

Worker 是跑在 Docker 容器里干活的 Agent（内置 frontend/backend 等角色）。

```bash
cd swarm
docker build -t swarm-worker:latest -f docker/Dockerfile.worker .
```

验证：
```bash
docker images swarm-worker
# 应看到 swarm-worker:latest
```

> 如果 `docker build` 因为网络拉不到 `python:3.12-slim`，用本地缓存：
> ```bash
> docker tag python:3.12.12-slim python:3.12-slim 2>/dev/null || true
> docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .
> ```

---

## 4. 配置

```bash
cp .env.example .env
```

编辑 `.env`，**必须改的**：
```bash
LLM_DEFAULT_API_KEY=你的真实GLM key        # 没有 key 整个系统跑不了
LLM_DEFAULT_MODEL=glm-4.7                  # coding/paas 端点支持的模型名
LLM_DEFAULT_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
```

编辑 `config/default.yaml`（这个文件被 gitignore，不会提交），确认：
```yaml
storage:
  shared_output_base: "/你的路径/swarm/shared_output"   # 改成你的实际路径
```

---

## 5. 启动 Redis

Swarm 用 Redis 做 Agent 注册中心。

```bash
docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine
# 验证
docker exec swarm-redis redis-cli ping
# 应返回 PONG
```

---

## 6. 启动 Swarm 后端

### 场景 A：用内置编排器（最简单，推荐先跑这个）

```bash
cd swarm
source .venv/bin/activate
python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
```

看到 `🐝 Agent Swarm ready!` 就是成功了。

### 场景 B：用 eino 外部编排器

先启动 eino-agent（另一个终端）：
```bash
cd /your/work/eino-agent
go build -o eino-agent .
./eino-agent -f etc/eino-agent.yaml
# 看到 "[eino] ReAct agent 就绪" 就是成功了
```

然后启动 Swarm，指向 eino：
```bash
cd swarm
source .venv/bin/activate
ORCHESTRATOR_PROVIDER=external \
ORCHESTRATOR_EXTERNAL_ENDPOINT=http://localhost:9030 \
python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
```

---

## 7. 验证系统就绪

```bash
# 健康检查
curl http://localhost:9000/api/health
# 应返回 {"status":"ok","pool_available":5,"pool_total":5,...}

# 列出可用 Agent
curl http://localhost:9000/api/agents | python -m json.tool
# 应返回 10+ 个 agent（frontend-engineer, backend-engineer, ...）

# 打开 Web UI
open http://localhost:9000/ui
```

---

## 8. 发任务（核心步骤）

### 方式 1：Web UI（推荐）

浏览器打开 `http://localhost:9000/ui`：
1. 左下角确认"已连接"
2. **Copilot 模式**：在输入框发任务，例如"写一个 FastAPI 的 TODO 应用，GET /api/todos 返回示例数据，POST /api/todos 创建"
3. 看中间区域的实时进度（plan → dispatch → agent 执行 → finalize）
4. 右侧"产物"面板看生成的文件，点击预览

### 方式 2：API（脚本/自动化）

```bash
# 发任务
RESP=$(curl -s -X POST http://localhost:9000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"写一个极简 FastAPI 后端：GET /api/todos 返回示例数据，POST /api/todos 创建。内存存储，CORS 全开。"}')
echo "$RESP"
TASK_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
echo "TASK_ID=$TASK_ID"

# 轮询状态（每 5 秒）
while true; do
  STATUS=$(curl -s http://localhost:9000/api/tasks/$TASK_ID | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  echo "status: $STATUS"
  [ "$STATUS" = "completed" ] && break
  [ "$STATUS" = "failed" ] && break
  sleep 5
done

# 看结果
curl -s http://localhost:9000/api/tasks/$TASK_ID | python3 -m json.tool

# 看产物
curl -s http://localhost:9000/api/tasks/$TASK_ID/artifacts | python3 -m json.tool
```

---

## 9. 验证产物可运行（金标准）

这一步证明 Swarm 生成的代码是真的能跑的。

```bash
# 找到产物目录（在 shared_output 里，按 task_id 或 session_id）
find shared_output -name "main.py" -newer config/default.yaml | head

# 假设产物在 shared_output/.../backend/main.py
PROD_DIR=$(dirname $(find shared_output -name "main.py" -newer config/default.yaml | head -1))

# 启动生成的后端
cd "$PROD_DIR"
python -m uvicorn main:app --host 127.0.0.1 --port 8001

# 另一个终端测 API
curl http://127.0.0.1:8001/api/todos                    # GET
curl -X POST http://127.0.0.1:8001/api/todos \
  -H 'Content-Type: application/json' \
  -d '{"title":"测试"}'                                  # POST
curl http://127.0.0.1:8001/api/todos                    # 确认有数据
curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/docs  # 应 200
```

如果 GET 返回数据、POST 成功、/docs 返回 200——**E2E 验证通过**。

---

## 10. 验证多轮对话（Session 恢复）

```bash
# 第一轮
RESP1=$(curl -s -X POST http://localhost:9000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"写个 hello world 网页","session_id":"my-test-session"}')
echo "$RESP1"

# 等 task 完成...

# 第二轮（同一个 session_id）——eino/编排器应该"记得"第一轮
RESP2=$(curl -s -X POST http://localhost:9000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"在上一个网页基础上加个按钮","session_id":"my-test-session"}')
echo "$RESP2"

# 查看 session 的完整事件链（审计/回放）
curl http://localhost:9000/api/sessions/my-test-session/events | python3 -m json.tool
```

事件链应该包含：`user_message → plan_created → agent_dispatched → agent_completed → finalized`（内置编排器）
或 `user_message → orchestrator_started → agent_progress → orchestrator_completed`（eino 外部编排器）

---

## 11. 接入你自己的 Agent（给同事）

### 方式 1：声明式（推荐，最简单）

在 swarm 的 `agents/` 目录加一个 YAML：
```yaml
# agents/my-agent.yaml
name: My Awesome Agent
endpoint: http://你的agent地址:端口
protocol: a2a
skills: [my-skill]    # 编排器按这个 skill 名调度你的 agent
```

重启 Swarm，它会自动注册。

### 方式 2：运行时注册

```bash
curl -X POST http://localhost:9000/api/v1/agents/register \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"My Agent",
    "endpoint":"http://你的agent:端口",
    "protocol":"a2a",
    "skills":["my-skill"]
  }'
```

### 你的 Agent 要实现什么（A2A 协议）

你的 Agent 服务必须暴露 3 个端点：

**1. Agent Card**
```
GET /.well-known/agent.json
```
返回：
```json
{
  "name": "My Agent",
  "description": "做什么的",
  "skills": [{"id":"my-skill","name":"My Skill"}]
}
```

**2. 接收任务**
```
POST /
Content-Type: application/json
```
请求体（JSON-RPC 2.0）：
```json
{
  "jsonrpc":"2.0","id":1,"method":"message/send",
  "params":{
    "message":{"role":"user","parts":[{"kind":"text","text":"用户任务"}]},
    "configuration":{"blocking":true}
  }
}
```
你处理完任务后返回：
```json
{
  "jsonrpc":"2.0","id":1,
  "result":{
    "id":"你的task_id",
    "status":{"state":"completed"},
    "history":[{"role":"agent","parts":[{"kind":"text","text":"你的回复"}]}]
  }
}
```

**3. 查询任务状态**（non-blocking 模式用）
```
POST /  method: "tasks/get"
```

> 完整范例参考：eino-agent 的 `internal/handler/handler.go`，或 swarm 的 `agents/mock-a2a-worker.py`。

---

## 12. 清理

```bash
# 停 Swarm
pkill -f "uvicorn src.main"

# 停 eino-agent
pkill -f eino-agent

# 删容器
docker rm -f swarm-redis swarm-worker-0 swarm-worker-1 swarm-worker-2 swarm-worker-3 swarm-worker-4

# 删测试产物（可选）
rm -rf shared_output/tenants/default/sessions/*
rm -rf shared_output/tenants/default/tasks/*
```

---

## 常见问题

**Q: 启动后 /api/health 返回 pool_available:0？**
A: Worker 镜像没构建。回到第 3 步 `docker build`。

**Q: 任务一直 running 不完成？**
A: 看日志 `tail -f` uvicorn 输出。最常见原因：LLM key 无效、worker 容器卡在等配置、GLM 端点不通。

**Q: eino 路径 3 秒就 fallback 到内置了？**
A: go-zero 默认超时 1s。检查 eino-agent 的 `etc/eino-agent.yaml` 里 `Timeout: 600000`。

**Q: 产物盘里找不到文件？**
A: 检查 `config/default.yaml` 的 `storage.shared_output_base` 路径对不对。worker 容器挂载了这个目录。

**Q: 多轮对话第二轮 eino 不记得第一轮？**
A: 确认用的是同一个 `session_id`。eino 路径现在会把历史压缩传过去，但必须是同一 session。

---

## Checklist（跑完打勾）

- [ ] Redis 起来了（PONG）
- [ ] Worker 镜像构建了（docker images 看到）
- [ ] Swarm 启动了（/api/health 返回 ok）
- [ ] 发任务成功（task completed）
- [ ] 产物落盘了（find shared_output 有文件）
- [ ] 生成的后端能启动（uvicorn 起来）
- [ ] 生成的后端 CRUD 正常（GET/POST 都通）
- [ ] /docs 返回 200
- [ ] Session 事件链完整（/api/sessions/{id}/events）
- [ ] 清理完成（无残留容器/进程）
