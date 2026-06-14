# Agent 接入指南（Onboarding）

面向团队：怎么把自己的 Agent 接进这个 Swarm——当 **Worker**（干活的）或 **Orchestrator**（调度的）。你的 Agent 多半是个**对话式 Chatbot**，下面 4 套方案覆盖常见情况，都能照抄。

> 约定：`$ORCH` = 编排器地址（如 `http://localhost:9000`）。所有方案都不需要改 Swarm 核心代码。

---

## 方案 A：Chatbot → Worker（最常用）

你的 chatbot 只要暴露一个 **OpenAI 兼容**的 `POST /v1/chat/completions`。注册即自动接入（注册时自动建好 adapter，立刻可被调度/调用）。

**一行 curl：**
```bash
curl -X POST $ORCH/api/v1/agents/register \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "my-chatbot",
    "endpoint": "http://my-bot:8000",
    "protocol": "openai",
    "skills": ["summarize", "translate"]
  }'
# → {"agent_id":"...", "status":"registered"}
```
注册后立刻可调用 / 验证：
```bash
curl -X POST $ORCH/api/v1/agents/<agent_id>/invoke \
  -H 'Content-Type: application/json' -d '{"task":"总结这段话"}'
```

**Python 自注册（带心跳，推荐长驻 chatbot 用）：**
```python
import asyncio
from src.swarm_sdk.client import AgentClient, start_heartbeat_loop

async def main():
    async with AgentClient(gateway_url="http://localhost:9000") as client:
        agent_id = await client.register(
            name="my-chatbot",
            endpoint="http://my-bot:8000",
            protocol="openai",
            skills=["summarize", "translate"],
        )
        hb = asyncio.create_task(start_heartbeat_loop(client, agent_id, interval=10))
        # ... 这里跑你自己的 chatbot 服务 ...
        hb.cancel()

asyncio.run(main())
```
> `skills` 决定编排器何时选你（编排器按 skill/agent_type 选 Agent）。想被"总结"类任务选中，就把 `summarize` 放进 skills。

---

## 方案 B：Chatbot → Orchestrator 大脑（换 LLM，循环不变）

编排器的"大脑"就是一次 chat-completion 调用。把编排器的 LLM 指向**你自己的 chatbot**（需支持 **function-calling / OpenAI tools 格式**），内置的 `plan → dispatch → review → finalize` 循环和 swarm 工具全部不变，只是 LLM 换成你的。

```bash
export LLM_DEFAULT_BASE_URL=http://my-bot:8000/v1   # 你的 chatbot
export LLM_DEFAULT_MODEL=my-model
export LLM_DEFAULT_API_KEY=...
# 然后正常启动编排器
```
> 适合：你想用自己微调/私有的模型来"做决策、做拆解"，但复用 Swarm 的调度+容器+产物能力。
> 要求：你的 chatbot 必须支持 OpenAI 的 `tools`（function-calling），否则编排循环无法调用 plan/dispatch 工具。

---

## 方案 C：A2A Agent → Worker

你的 Agent 说 **A2A 协议**（JSON-RPC `message/send` over HTTP，本 Swarm worker 就是这个协议）：
```bash
curl -X POST $ORCH/api/v1/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"a2a-bot","endpoint":"http://a2a-bot:9000","protocol":"a2a","skills":["code-review"]}'
```
> 适合：你已有 A2A 服务，或想用和内置 worker 完全一致的协议（含非阻塞/进度上报）。

---

## 方案 D：外部 Agent → Orchestrator（整体接管调度）

你不只想换 LLM，想让**你的 Agent 完全接管**整个编排循环（plan/dispatch/review 全自己做）。把 provider 切到 external：
```yaml
# config/default.yaml
orchestrator:
  provider: "external"
  external_endpoint: "http://my-scheduler:9000"   # 你的调度 Agent 的 A2A URL
  fallback: true                                   # 它挂了自动回退内置编排器（日志+事件显式标注）
```
或环境变量：`ORCHESTRATOR_PROVIDER=external ORCHESTRATOR_EXTERNAL_ENDPOINT=http://my-scheduler:9000`
> 你的调度 Agent 需说 A2A；不可用时自动回退内置（已验证）。适合：你有更强的调度策略/外部编排系统。

---

## 方案 E：在仓库里新写一个 Worker 角色
> ⚠️ 当前要改 `worker.py` 的 `AGENT_CARDS` / `SYSTEM_PROMPTS`（硬编码）。**W2 会把它做成纯配置化**——加角色 = 加一条 config，不动核心代码。在那之前，参考 `worker.py` 里 `frontend-ux-pro`/`backend-engineer`/`general-agent` 的写法复制一份。

---

## 编排器是怎么"调动"你的 Agent 的
1. 用户发消息 → 编排器 LLM 工具循环：`plan_task`（拆解+生成共享计划/API 契约）。
2. `dispatch_agent` / `dispatch_agents_parallel`：按 **agent_type 或 skill** 选 Agent（内置 Docker worker + 你注册的外部 Agent **同一个候选池**），并行/串行分派。
3. 每个 Agent 在**自己的工作子目录**干活，产物落到共享任务目录。
4. `read_artifacts`/`list_artifacts`：编排器审查所有产出。
5. `finalize`：交付。
> 所有 Agent 收到同一份"共享上下文（计划/API 契约）"作为对齐依据。

## 健壮性保障（已内置）
- 外部 Agent `/invoke` 有**熔断**（连续失败→503）。
- 调度有**重试+故障转移**、**健康预检**、**超时+背压**、**per-agent 熔断**。
- 全失败时可**降级返回缓存**；失败任务进**死信**；`/api/chat` 支持**幂等**。
- 每个任务有 **trace_id** 贯穿全链路日志。

---
_本指南随 W2–W5（角色可插拔 / 共享读 / 强制 review）持续更新。_
