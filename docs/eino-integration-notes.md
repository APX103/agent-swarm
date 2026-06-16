# eino Orchestrator 接入调研报告

## 结论：✅ 能接。已验证 eino ReAct agent 能作为 Swarm 的外部 orchestrator 工作。

同事写的成熟 eino agent 只要暴露 A2A 端点（`POST /` message/send + `GET /.well-known/agent.json`），就能按同样的方式接入。

---

## 已验证的完整链路（2026-06-16 实测）

```
用户 POST /api/chat "做一个极简待办应用"
  → Swarm ExternalOrchestrator
    → eino A2A endpoint (POST localhost:9020 message/send, blocking)
      → eino ReAct loop（GLM glm-4.7 驱动）：
         11:55:57  决策 → dispatch_backend-engineer（调 GLM 10s 决策）
         11:55:57  tool body → A2A POST localhost:9001 message/send（Swarm worker）
         11:56:23  ← worker 返回 completed（26s 写代码）
         11:56:27  决策 → dispatch_frontend-engineer
         11:57:15  ← worker 返回 completed（48s，带文本输出）
         11:57:40  eino 汇总 → 返回 A2A task (status=completed)
  ← Swarm 拿到字符串，task.status = completed ✅
```

**关键证明点：**
1. eino 的 ReAct agent 真的调了 GLM 做 tool-calling 决策（不是写死的）
2. eino 真的通过 dispatch tool 打到了 Swarm 的 worker（localhost:9001）
3. worker 真的执行了（返回 completed + 文本）
4. eino 真的汇总了两个子任务的结果，返回了结构化的交付总结
5. Swarm 真的把 eino 当 orchestrator 用了（provider=external 生效）

---

## 怎么接的（3 个配置点）

### 1. eino 服务侧：实现 A2A 端点
Go + eino + go-zero 写一个服务（`eino-orchestrator/`），暴露：
- `GET /.well-known/agent.json` → AgentCard
- `POST /` → JSON-RPC `message/send`：解析 user message → 调 `einoAgent.Generate()` → 返回 A2A task

核心代码（`internal/svc/servicecontext.go`）：
```go
// GLM chat model
cm, _ := openaimodel.NewChatModel(ctx, &openaimodel.ChatModelConfig{
    APIKey: c.GLM.APIKey, BaseURL: c.GLM.BaseURL, Model: c.GLM.Model,
})
// 从 Swarm 平台拉 agent 列表，每个生成一个 dispatch tool
tools := logic.BuildTools(c.Swarm.APIBase)
// eino ReAct agent
ag, _ := react.NewAgent(ctx, &react.AgentConfig{
    ToolCallingModel: cm,
    ToolsConfig: compose.ToolsNodeConfig{Tools: tools},
})
```

### 2. Swarm 侧：切到 external provider
```bash
ORCHESTRATOR_PROVIDER=external \
ORCHESTRATOR_EXTERNAL_ENDPOINT=http://localhost:9020 \
python -m uvicorn src.main:app --port 9000
```
Swarm 的 `ExternalOrchestrator` 自动把 `/api/chat` 的请求 A2A 转发给 eino。

### 3. dispatch tool：eino 调 Swarm worker
每个 Swarm agent 变成 eino 的一个 tool。tool body 是 Go 版 A2A 客户端，
POST 到 worker 的 9001 端口（blocking message/send），拿结果返回给 ReAct loop。

---

## 踩的 3 个坑（都解决了）

### 坑 1：go-zero 默认 1s 超时 → 503
**现象**：Swarm 发 message/send 给 eino，3 秒后收到 503，Swarm 回退到内置 orchestrator。
**根因**：go-zero 的 `rest.RestConf` 默认 `Timeout: 1000ms`，ReAct loop（调 GLM + 调 worker 要 60s+）远超。
**解决**：`etc/eino-orchestrator.yaml` 加 `Timeout: 600000`（10 分钟）。

### 坑 2：eino dispatch tool 端口映射
**现象**：eino 假设 frontend 在 9001、backend 在 9002，但 Swarm pool 只有 1 个 worker（9001）。
**根因**：Swarm `/api/agents` 返回的 card 没有 endpoint 字段，eino 按端口约定推导会错。
**解决（demo）**：所有 dispatch tool 都指向 9001（唯一活着的 worker）。
**生产解法**：dispatch tool 应走 Swarm 的 `/api/v1/agents/{id}/invoke`（带 agent_id 直选），不直连 worker。

### 坑 3：eino 的真实 API 和文档不一样
**现象**：调研文档说 `react.WithChatModel(cm)` + `react.WithToolsConfig(...)`，实际不存在。
**真实 API**（eino v0.9.8）：
- `react.NewAgent(ctx, *react.AgentConfig)` —— AgentConfig 有 `ToolCallingModel` + `ToolsConfig`
- `Agent.Generate(ctx, []*schema.Message, opts...) → *schema.Message` —— 输入是 message 切片不是 string
- tool 用 `utils.InferTool[T,D](name, desc, fn)` 泛型构造
- `agent` 包在 `github.com/cloudwego/eino/flow/agent`（不是 `components/agent`）

---

## 已知限制（external orchestrator 路径的，不是 eino 的）

| 限制 | 影响 | 解法（生产时做） |
|------|------|----------------|
| ExternalOrchestrator 不传 session 上下文 | eino 收不到 work_dir/tenant_id/历史 | 改 external.py 传 session |
| ExternalOrchestrator 不流式 | Web UI 等 eino 返回才显示 | external.py 改用 poll_task |
| eino dispatch 直连 worker，产物不落 task 目录 | 文件写在 worker 默认 SHARED_DIR | dispatch 走 Swarm gateway |
| external 返回无 plan_created/finalized 事件 | session 事件链不完整 | eino 侧或 Swarm 侧补事件 |

这些都是 Swarm 的 ExternalOrchestrator 实现简单导致的，不是 eino 框架的限制。

---

## 给同事的接入指南（"你的 eino agent 怎么接 Swarm"）

1. **你的 eino agent 要暴露 A2A 端点**：
   - `GET /.well-known/agent.json` → 返回 AgentCard（name/description/skills）
   - `POST /` → JSON-RPC `message/send`：解析 user message → 调你的 eino agent → 返回 A2A task
   - 参考实现：`eino-orchestrator/internal/handler/handler.go`

2. **你的 dispatch tool 要能调 Swarm 的子 agent**：
   - 简单版：直连 worker A2A（`POST localhost:9001`）
   - 推荐版：走 Swarm gateway（`POST localhost:9000/api/v1/agents/{id}/invoke`）
   - 参考实现：`eino-orchestrator/internal/logic/tools.go`

3. **Swarm 配置切到 external**：
   ```bash
   ORCHESTRATOR_PROVIDER=external \
   ORCHESTRATOR_EXTERNAL_ENDPOINT=http://你的eino服务:端口
   ```

4. **注意超时**：go-zero / httpx 的超时要拉到 600s（ReAct loop 慢）。

---

## 文件清单

```
eino-orchestrator/
├── eino-orchestrator.go              # main 入口（go-zero server）
├── go.mod / go.sum                   # 依赖（eino v0.9.8 + eino-ext + go-zero）
├── etc/eino-orchestrator.yaml        # 配置（GLM + Swarm 地址 + Timeout 600s）
├── internal/
│   ├── config/config.go              # 配置结构
│   ├── a2a/client.go                 # Go 版 A2A 客户端（调 Swarm worker）
│   ├── handler/handler.go            # A2A 端点（agent card + JSON-RPC）
│   ├── logic/tools.go                # 动态生成 dispatch tools
│   └── svc/servicecontext.go         # 初始化 eino ReAct agent
└── RESEARCH-NOTES.md                 # 本文档
```
