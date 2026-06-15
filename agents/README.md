# 声明式 Agent 注册

放 YAML 文件到这个目录，编排器启动时自动注册到 registry + adapter_manager。
也可以运行时 `POST /api/v1/agents/register` 动态注册。

## 内置示例 Agent（通用软件团队 10 角色）

本目录已带 10 个 A2A 协议的示例 Agent，端口 9001–9010：

| 角色 ID | 端口 | 职责 |
|---|---|---|
| `frontend-engineer` | 9001 | React/Vue/TypeScript、响应式 UI、无障碍 |
| `backend-engineer` | 9002 | API 设计、数据库、Python/FastAPI/Node.js |
| `fullstack-engineer` | 9003 | 端到端全栈交付 |
| `devops-engineer` | 9004 | Docker/K8s、CI/CD、部署监控 |
| `qa-engineer` | 9005 | 单元/集成/E2E 测试、质量保障 |
| `security-engineer` | 9006 | 漏洞评估、鉴权、安全编码审查 |
| `data-engineer` | 9007 | 数据管道、ETL、SQL、分析脚本 |
| `mobile-engineer` | 9008 | React Native/Flutter、iOS/Android |
| `tech-writer` | 9009 | README/API 文档、架构说明 |
| `design-reviewer` | 9010 | UI/UX 审查、可用性评估 |

每个 YAML 的 `skills` 字段就是它的 role id——编排器/Dispatcher 按 skill 匹配分派，
直聊模式按 agent_id 直选。

## 把示例 Agent 跑起来（两种方式）

### A. 用 mock worker（最快，无需真实容器/LLM）
```bash
# 起 10 个 mock A2A agent（端口 9001-9010），返回每个角色的预设回复
python agents/mock-a2a-worker.py --start-port 9001 --count 10
```

### B. 接真实容器
把各 YAML 的 `endpoint` 改成你团队容器的真实地址，确保它实现 A2A JSON-RPC：
- `GET /.well-known/agent.json` —— 返回 AgentCard
- `POST /` —— `message/send`（支持 `blocking` 字段）、`tasks/get`、`tasks/cancel`
- 完成后 `status.state` 设为 `completed`，agent 回复放 `history` 里 `role=agent` 的 `parts[].text`

## YAML 格式
```yaml
name: my-chatbot          # 必填
endpoint: http://host:port  # 必填（A2A 服务的根 URL）
protocol: a2a            # a2a | openai | cli | mcp | http
skills: [my-role]        # 编排器按 skill 选中；直聊按返回的 agent_id 选
```

启动时自动注册到 registry + adapter_manager。
