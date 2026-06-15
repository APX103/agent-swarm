# Event-First Session 设计（参考 ADK SessionService）

> 状态：设计阶段，待用户确认。

## 目标
把编排器的上下文从"一坨文本 messages"升级为**结构化 state 盒子 + events 流水账**。Agent 获得精准的结构化上下文（不是整坨文本），session 可审计、可恢复、LLM 无关。

## 数据模型

### Session（替换当前 SessionState）
```python
@dataclass
class Session:
    session_id: str
    tenant_id: str
    work_dir: Path
    state: dict          # 结构化状态（Agent 读写这个）
    events: list[dict]   # append-only 事件流水账
    created_at: float
```

### state 结构
```python
state = {
    "plan": {                          # plan_task 生成
        "analysis": "用户要 TODO 应用...",
        "api_contract": "GET /api/todos ...",
        "tech_stack": "FastAPI + vanilla JS",
        "subtasks": [{"agent_type": "backend-engineer", "description": "..."}],
    },
    "artifacts": {                     # Agent 产出累积
        "frontend": ["index.html"],
        "backend": ["main.py", "requirements.txt"],
    },
    "dispatched": ["frontend-ux-pro"], # 已 dispatch 的 Agent
    "decisions": ["用 FastAPI", "CORS 全开"],
}
```

### events 格式
```python
{"type": "user_message", "text": "...", "timestamp": 1718...}
{"type": "plan_created", "plan": {...}}
{"type": "agent_dispatched", "agent_type": "backend-engineer", "task": "..."}
{"type": "agent_completed", "agent_type": "...", "artifacts": ["main.py"]}
{"type": "review_passed", "agent_type": "..."}
{"type": "finalized", "summary": "..."}
```

## SessionService 接口
```python
class SessionService:
    async def create_session(self, tenant_id) -> Session
    async def get_session(self, session_id) -> Session | None
    async def append_event(self, session_id, event: dict) -> Session
    async def update_state(self, session_id, delta: dict) -> Session
```

实现：`SQLiteSessionService`（复用现有 swarm.db，新表 `sessions_v2`）。

## 编排器集成（LLM 循环不变）

编排器内部仍然用 LLM + messages（它需要 LLM 做决策）。但：
- `plan_task` → 写 `state["plan"]` + append `plan_created` 事件。
- `dispatch_agent` → 从 `state["plan"]` 投影相关部分给 Agent + append `agent_dispatched`。
- dispatch 结果回来 → 更新 `state["artifacts"]` + append `agent_completed`。
- `review` → 读 `state["artifacts"]` 做结构化检查 + append `review_passed/failed`。
- `finalize` → 从 `state` + `events` 生成总结 + append `finalized`。

**Agent 获得的上下文**：不再是整坨 shared_context 文本，而是 `state` 的投影（plan + 相关 decisions + sibling artifacts 清单）。

## 向后兼容
- 现有 `session.messages` + `session.shared_context` **保留**（LLM 循环需要）。
- `state` + `events` 是**新增层**（additive），不是替换。
- 现有测试全绿（只增不改）。
- 后续可选：从 events 重放重建 messages（完整 event sourcing）。

## 改动范围
1. `src/session/models.py` — Session + Event 类型。
2. `src/session/service.py` — SessionService + SQLiteSessionService。
3. `src/orchestrator/orchestrator.py` — 工具方法写 state + append events。
4. `src/api/routes.py` — 用 SessionService 替换 SessionManager（或并存）。
5. 测试 — SessionService CRUD + 事件追加 + state 更新 + 恢复。

## 不改的
- LLM 循环结构（plan→dispatch→review→finalize 不变）。
- Dispatcher / DockerBackend / 外部 Agent 路径。
- 公开 HTTP API。
- 已有的 messages 持久化（backward compat）。

## 验证标准
- 同一 session 多轮：state 累积（plan + artifacts + decisions），events 追加。
- 重启后：从 SQLite 恢复 state + events（不用 LLM messages）。
- Agent 收到的上下文是结构化的（JSON），不是一坨文本。
- 现有 297 测试全绿。
