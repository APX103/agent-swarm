"""Orchestrator Agent - 系统编排核心

负责：
1. 分析用户请求，拆解为子任务
2. 选择合适的 Worker Agent 类型
3. 通过 A2A 协议向 Worker 发送任务
4. 监控进度，汇总结果
5. 返回最终响应给用户

使用 OpenAI-compatible API（GLM Coding Plan）实现 tool-calling 循环。
"""
import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from openai import OpenAI

from src.container_pool.pool import ContainerPoolManager
from src.dispatcher.backends import DockerBackend
from src.dispatcher.base import DispatchRequest, DispatchResult
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig

logger = logging.getLogger(__name__)

ORCHESTRATOR_SYSTEM_PROMPT = """你是一个 Agent Swarm 编排器——多 Agent 协作系统的核心大脑。

## 你的职责
1. 深度分析用户请求，理解真实意图和技术需求
2. 制定结构化执行计划（含 API 契约、技术选型、任务拆分）
3. 将计划写入共享上下文，分派 Worker Agent 执行
4. 审查 Worker 产出，必要时要求修正
5. 汇总结果，交付完整方案

## 工作流程（严格按顺序执行）

### 第一步：规划（必须首先执行）
调用 `plan_task` 工具生成结构化计划。计划必须包含：
- **需求分析**：用户想要什么，核心功能列表
- **技术选型**：前端/后端技术栈、数据库、关键依赖
- **API 契约**：前后端接口定义（REST endpoint、请求/响应格式）
- **子任务拆分**：每个子任务指明 agent_type 和具体工作内容
- **集成要点**：前后端如何对接，需要共享的数据格式

### 第二步：分派
根据计划调用 `dispatch_agent`（单任务）或 `dispatch_agents_parallel`（多任务并行）。
- 独立的任务使用 `dispatch_agents_parallel` 并行执行（如前端和后端可以并行）
- 有依赖关系的任务串行执行
- 共享上下文（API 契约、设计规范）会自动注入到每个 Worker 的任务描述中

### 第三步：审查
Worker 完成后：
- 用 `read_artifacts` / `list_artifacts` 检查产出物
- 如果产出不完整或有明显问题，调用 `dispatch_agent` 让 Worker 修正
- 如果前后端接口不匹配，指出冲突并要求修正

### 第四步：交付
确认所有产出合格后，调用 `finalize` 提供总结。

## 关键原则
- **先规划再执行**：永远不要跳过 plan_task 步骤
- **API 契约先行**：前后端必须有统一的接口定义
- **最大化并行**：独立任务用 dispatch_agents_parallel
- **质量把关**：finalize 前必须检查产出物
- 如果 dispatch_agent 返回错误，分析原因并重试

## 可用的 Agent 类型
- "frontend-ux-pro": 前端开发（HTML/CSS/JS/React/Vue），擅长 UI/UX
- "backend-engineer": 后端开发（API/数据库/Python/FastAPI/Node.js）
- "general-agent": 通用任务（文档、分析、脚本等）
"""


def review_artifacts(dispatched_types: list[str], artifacts_dir) -> dict:
    """检查每个已分派 Agent 是否在自己的角色子目录下产出了文件。

    角色子目录取 agent_type 的第一段（frontend-ux-pro -> frontend），与 worker 写盘一致。
    返回 {'passed': bool, 'missing': [...], 'per_agent': {agent_type: file_count}}。
    """
    artifacts_dir = Path(artifacts_dir)
    per_agent: dict[str, int] = {}
    missing: list[str] = []
    for agent_type in dispatched_types:
        sub = agent_type.split("-")[0]
        d = artifacts_dir / sub
        files = [f for f in d.rglob("*") if f.is_file()] if d.exists() else []
        per_agent[agent_type] = len(files)
        if not files:
            missing.append(agent_type)
    return {"passed": not missing, "missing": missing, "per_agent": per_agent}


class Orchestrator:
    """编排器 Agent"""
    
    def __init__(self, settings, pool_manager: ContainerPoolManager, task_manager=None,
                 dispatcher: Optional[Dispatcher] = None):
        self.settings = settings
        self.pool = pool_manager
        self.task_manager = task_manager

        # LLM Client (OpenAI-compatible for GLM)
        api_key = settings.llm.default_api_key or "no-key-configured"
        base_url = settings.llm.default_base_url
        model = settings.llm.default_model

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = model

        # Unified dispatcher (Docker + external). If none is injected, build a
        # Docker-only one so behaviour matches the pre-refactor orchestrator.
        if dispatcher is None:
            dispatcher = Dispatcher(
                [DockerBackend(pool=pool_manager, model=model, base_url=base_url, api_key=api_key)],
                DispatcherConfig(),
            )
        self._dispatcher = dispatcher

        # 当前任务的上下文
        self._messages: list[dict] = []
        # dispatch_id -> (agent_type, DispatchResult)
        self._dispatched: dict[str, tuple[str, DispatchResult]] = {}
        self._current_task_id: Optional[str] = None
        self._current_tenant_id: Optional[str] = None
        self._shared_context: str = ""  # plan_task 生成的共享上下文
        self._current_emit = None  # set during execute(), used to forward worker progress

        # 工具定义
        self._tools = self._define_tools()
    
    def _define_tools(self) -> list[dict]:
        """定义 OpenAI function calling 工具"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "plan_task",
                    "description": (
                        "生成结构化任务计划。这是分派 Agent 前必须执行的第一步。"
                        "计划包含需求分析、技术选型、API 契约、子任务拆分和集成要点。"
                        "计划会被保存为共享上下文，自动注入到每个 Worker 的任务中。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "analysis": {
                                "type": "string",
                                "description": "需求分析：用户意图、核心功能列表",
                            },
                            "tech_stack": {
                                "type": "string",
                                "description": "技术选型：前端/后端技术栈、数据库、关键依赖",
                            },
                            "api_contract": {
                                "type": "string",
                                "description": "前后端 API 契约：REST endpoint 列表、请求/响应 JSON 格式。前后端共同遵循此定义。",
                            },
                            "subtasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "agent_type": {
                                            "type": "string",
                                            "description": "Agent 类型 ID",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "该子任务的具体工作内容和要求",
                                        },
                                    },
                                    "required": ["agent_type", "description"],
                                },
                                "description": "子任务列表，每个包含 agent_type 和工作描述",
                            },
                            "integration_notes": {
                                "type": "string",
                                "description": "集成要点：前后端如何对接、共享数据格式、注意事项",
                            },
                        },
                        "required": ["analysis", "subtasks"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dispatch_agent",
                    "description": "启动一个 Worker Agent 并发送任务。共享上下文（来自 plan_task）会自动注入。" +
                                   "可用的 agent_type 有：" +
                                   ", ".join(ac.id for ac in self.settings.agent_cards),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_type": {
                                "type": "string",
                                "description": "Agent 类型 ID，如 'frontend-ux-pro'",
                            },
                            "task": {
                                "type": "string",
                                "description": "发送给 Worker Agent 的详细任务描述",
                            },
                        },
                        "required": ["agent_type", "task"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dispatch_agents_parallel",
                    "description": (
                        "并行启动多个 Worker Agent。适用于无依赖关系的独立任务（如前端和后端可并行开发）。"
                        "共享上下文（来自 plan_task）会自动注入到每个 Agent。"
                        "所有 Agent 并发执行，全部完成后返回。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agents": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "agent_type": {
                                            "type": "string",
                                            "description": "Agent 类型 ID",
                                        },
                                        "task": {
                                            "type": "string",
                                            "description": "该 Agent 的具体任务描述",
                                        },
                                    },
                                    "required": ["agent_type", "task"],
                                },
                                "description": "并行分派的 Agent 列表",
                            },
                        },
                        "required": ["agents"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "check_agent_status",
                    "description": "查询一个已分派的 Worker Agent 的任务状态和结果",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "dispatch_id": {
                                "type": "string",
                                "description": "dispatch_agent 返回的分派 ID",
                            },
                        },
                        "required": ["dispatch_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_artifacts",
                    "description": "读取共享目录中的文件内容。文件路径相对于任务工作目录。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "文件路径，如 'frontend/index.html' 或 'backend/main.py'",
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_artifacts",
                    "description": "列出共享目录中的所有文件",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finalize",
                    "description": "汇总所有工作结果，完成任务。必须调用此工具来结束任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "给用户的最终结果摘要",
                            },
                        },
                        "required": ["summary"],
                    },
                },
            },
        ]
    
    async def execute(self, task_id: str, tenant_id: str, user_message: str,
                     event_callback=None) -> str:
        """执行编排流程
        
        Args:
            task_id: 任务 ID
            tenant_id: 租户 ID
            user_message: 用户消息
            event_callback: 事件回调（用于 WebSocket 推送）
        
        Returns:
            最终结果文本
        """
        self._current_task_id = task_id
        self._current_tenant_id = tenant_id
        self._dispatched.clear()
        self._shared_context = ""
        self._messages = [
            {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        
        async def emit(event_type: str, data: dict = None, agent: str = None):
            if event_callback:
                await event_callback({
                    "type": event_type,
                    "task_id": task_id,
                    "agent": agent,
                    "data": data,
                })

        self._current_emit = emit  # lets dispatch tools forward worker progress
        
        max_iterations = 25  # 允许更多迭代以容纳 plan → dispatch → review 流程
        final_result = "任务执行完毕，但未产生最终结果。"
        
        for i in range(max_iterations):
            await emit("orchestrator_thinking", {"iteration": i})
            
            # 如果已经分配了 agent 且在最后几轮，检查是否有足够的结果来 finalize
            if i == max_iterations - 4 and self._dispatched:
                logger.info("Approaching max iterations, forcing finalize check")
                self._messages.append({
                    "role": "user",
                    "content": "请立即调用 finalize 工具来完成任务，总结目前的工作成果。"
                })
                await emit("orchestrator_thinking", {"iteration": i, "note": "forcing finalize"})
            
            try:
                response = await asyncio.to_thread(
                    self.client.chat.completions.create,
                    model=self.model,
                    messages=self._messages,
                    tools=self._tools if i < max_iterations - 3 else None,  # 最后三轮不带工具，强制文本回复
                    temperature=0.3,
                    max_tokens=8192,
                )
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                final_result = f"编排器调用 LLM 失败: {e}"
                break
            
            choice = response.choices[0]
            msg = choice.message
            
            # Debug: 记录 LLM 响应
            logger.debug(f"LLM response: content={msg.content is not None}, tool_calls={msg.tool_calls is not None}")
            if msg.content:
                logger.debug(f"LLM content: {msg.content[:200]}")
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    logger.debug(f"LLM tool_call: {tc.function.name}({tc.function.arguments[:100]})")
            
            # 将 assistant 消息加入历史
            self._messages.append(msg.model_dump())
            
            if not msg.tool_calls:
                # 没有工具调用，直接作为回复
                final_result = msg.content or final_result
                break
            
            # 处理每个工具调用
            finalize_ok = False
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                
                logger.info(f"Tool call: {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:100]})")
                await emit("tool_call", {"tool": fn_name, "args": fn_args}, agent="orchestrator")
                
                result = await self._execute_tool(fn_name, fn_args)
                
                await emit("tool_result", {"tool": fn_name, "result": result[:500]}, agent="orchestrator")
                
                # 将工具结果加入历史
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
                
                # finalize：仅当 review 通过才完成；否则继续循环让编排器重新 dispatch
                if fn_name == "finalize":
                    if "[REVIEW_FAILED]" in result:
                        logger.info("finalize blocked by review: %s", result[:200])
                    else:
                        try:
                            fargs = json.loads(fn_args) if isinstance(fn_args, str) else fn_args
                            final_result = fargs.get("summary", result)
                        except Exception:
                            final_result = result
                        finalize_ok = True
                        break

            # 仅在 finalize 通过 review 后退出
            if finalize_ok:
                break
        
        # 清理：容器归还已由 Dispatcher 在每次 dispatch 的 finally 内完成，这里仅清理分派记录
        self._dispatched.clear()
        self._current_emit = None

        return final_result
    
    async def _execute_tool(self, name: str, args: dict) -> str:
        """执行工具调用"""
        if name == "plan_task":
            return await self._tool_plan_task(args)
        elif name == "dispatch_agent":
            return await self._tool_dispatch_agent(args)
        elif name == "dispatch_agents_parallel":
            return await self._tool_dispatch_agents_parallel(args)
        elif name == "check_agent_status":
            return await self._tool_check_agent_status(args)
        elif name == "read_artifacts":
            return await self._tool_read_artifacts(args)
        elif name == "list_artifacts":
            return await self._tool_list_artifacts(args)
        elif name == "finalize":
            return await self._tool_finalize(args)
        else:
            return f"Unknown tool: {name}"
    
    async def _tool_plan_task(self, args: dict) -> str:
        """plan_task 工具实现 — 生成结构化计划并存储为共享上下文"""
        analysis = args.get("analysis", "")
        tech_stack = args.get("tech_stack", "")
        api_contract = args.get("api_contract", "")
        subtasks = args.get("subtasks", [])
        integration_notes = args.get("integration_notes", "")

        sections = ["=== 项目计划（共享上下文） ===\n"]
        if analysis:
            sections.append(f"## 需求分析\n{analysis}\n")
        if tech_stack:
            sections.append(f"## 技术选型\n{tech_stack}\n")
        if api_contract:
            sections.append(f"## API 契约（前后端必须共同遵循）\n{api_contract}\n")
        if integration_notes:
            sections.append(f"## 集成要点\n{integration_notes}\n")

        self._shared_context = "\n".join(sections)

        if self.task_manager:
            artifacts_dir = self.task_manager.get_artifacts_dir(self._current_task_id)
            if artifacts_dir:
                plan_dir = artifacts_dir / "_plan"
                plan_dir.mkdir(parents=True, exist_ok=True)
                (plan_dir / "project_plan.md").write_text(self._shared_context, encoding="utf-8")

        subtask_summary = "\n".join(
            f"  {i+1}. [{st.get('agent_type', '?')}] {st.get('description', '')[:80]}"
            for i, st in enumerate(subtasks)
        )

        can_parallel = len(set(st.get("agent_type", "") for st in subtasks)) > 1

        return (
            f"计划已生成并保存为共享上下文。\n\n"
            f"子任务列表（{len(subtasks)} 个）：\n{subtask_summary}\n\n"
            f"建议：{'多个不同类型的 Agent 可使用 dispatch_agents_parallel 并行执行' if can_parallel else '使用 dispatch_agent 串行执行'}"
        )

    async def _tool_dispatch_agent(self, args: dict) -> str:
        """dispatch_agent 工具实现 — 经统一 Dispatcher 分派（Docker + 外部 Agent）"""
        agent_type = args.get("agent_type", "")
        task = args.get("task", "")

        full_task = self._build_worker_task(task)

        # forward worker mid-flight progress to the event stream (WebSocket)
        on_progress = None
        if self._current_emit is not None:
            emit = self._current_emit

            async def on_progress(event: dict) -> None:
                await emit("agent_progress", {"agent": agent_type, **event}, agent=agent_type)

        request = DispatchRequest(
            agent_type=agent_type,
            task=full_task,
            context={"task_id": self._current_task_id, "tenant_id": self._current_tenant_id},
            on_progress=on_progress,
        )
        result = await self._dispatcher.dispatch(request)

        dispatch_id = str(uuid.uuid4())[:8]
        self._dispatched[dispatch_id] = (agent_type, result)

        state = "completed" if result.success else "failed"
        text = result.output or result.error or "无响应"
        return (f"已分派 Agent '{agent_type}'（dispatch_id={dispatch_id}）\n"
                f"任务状态: {state}\n"
                f"Agent 响应: {text[:500]}")
    
    def _build_worker_task(self, task: str) -> str:
        """将共享上下文注入 Worker 任务描述"""
        if self._shared_context:
            return f"{self._shared_context}\n\n---\n\n## 你的具体任务\n{task}"
        return task
    
    async def _tool_dispatch_agents_parallel(self, args: dict) -> str:
        """dispatch_agents_parallel 工具实现 — 并行分派多个 Worker（经统一 Dispatcher）"""
        agents_spec = args.get("agents", [])
        if not agents_spec:
            return "错误：agents 列表为空"

        async def _run_single(spec: dict) -> tuple[str, DispatchResult]:
            agent_type = spec.get("agent_type", "")
            full_task = self._build_worker_task(spec.get("task", ""))
            request = DispatchRequest(
                agent_type=agent_type,
                task=full_task,
                context={"task_id": self._current_task_id, "tenant_id": self._current_tenant_id},
            )
            return agent_type, await self._dispatcher.dispatch(request)

        outcomes = await asyncio.gather(*[_run_single(spec) for spec in agents_spec])

        lines = [f"并行分派完成（{len(outcomes)} 个 Agent）："]
        for agent_type, result in outcomes:
            dispatch_id = str(uuid.uuid4())[:8]
            self._dispatched[dispatch_id] = (agent_type, result)
            state = "completed" if result.success else "failed"
            text = result.output or result.error or "无响应"
            lines.append(
                f"\n[{agent_type}（dispatch_id={dispatch_id}）]\n"
                f"  状态: {state}\n"
                f"  响应: {text[:300]}"
            )

        return "\n".join(lines)

    async def _tool_check_agent_status(self, args: dict) -> str:
        """check_agent_status 工具实现 — 返回已分派 Agent 的最终结果"""
        dispatch_id = args.get("dispatch_id", "")
        rec = self._dispatched.get(dispatch_id)

        if not rec:
            return f"错误：未找到分派 {dispatch_id}"

        agent_type, result = rec
        state = "completed" if result.success else "failed"
        text = result.output or result.error or "无"
        return (f"Agent '{agent_type}'（{dispatch_id}）状态:\n"
                f"  状态: {state}\n"
                f"  最新消息: {text[:500]}")
    
    async def _tool_read_artifacts(self, args: dict) -> str:
        """read_artifacts 工具实现"""
        file_path = args.get("file_path", "")
        
        if not self.task_manager:
            return "错误：TaskManager 未初始化"
        
        artifacts_dir = self.task_manager.get_artifacts_dir(self._current_task_id)
        if not artifacts_dir:
            return "错误：未找到任务工作目录"
        
        full_path = artifacts_dir / file_path
        if not full_path.exists():
            return f"文件不存在: {file_path}"
        
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            # 限制返回长度
            if len(content) > 5000:
                content = content[:5000] + "\n... (截断)"
            return content
        except Exception as e:
            return f"读取文件失败: {e}"
    
    async def _tool_list_artifacts(self, args: dict) -> str:
        """list_artifacts 工具实现"""
        if not self.task_manager:
            return "错误：TaskManager 未初始化"
        
        artifacts_dir = self.task_manager.get_artifacts_dir(self._current_task_id)
        if not artifacts_dir:
            return "错误：未找到任务工作目录"
        
        files = []
        for f in artifacts_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(artifacts_dir))
                size = f.stat().st_size
                files.append(f"  {rel} ({size} bytes)")
        
        if not files:
            return "工作目录为空，暂无产物。"
        
        return f"产物文件列表（共 {len(files)} 个）：\n" + "\n".join(files)
    
    async def _tool_finalize(self, args: dict) -> str:
        """finalize 工具实现 — 验证产出物后完成任务"""
        summary = args.get("summary", "")

        # 强制 review：每个已分派 Agent 必须产出文件，否则拒绝完成（让编排器重新 dispatch）
        if self.task_manager and self._dispatched:
            artifacts_dir = self.task_manager.get_artifacts_dir(self._current_task_id)
            if artifacts_dir:
                dispatched_types = [t for t, _ in self._dispatched.values()]
                review = review_artifacts(dispatched_types, artifacts_dir)
                if not review["passed"]:
                    return (
                        f"[REVIEW_FAILED] 审查未通过，以下已分派 Agent 未产出文件: "
                        f"{', '.join(review['missing'])}。"
                        f"请用 dispatch_agent 让它们产出后再次 finalize。"
                    )

        artifact_warning = ""
        if self.task_manager:
            artifacts_dir = self.task_manager.get_artifacts_dir(self._current_task_id)
            if artifacts_dir and artifacts_dir.exists():
                real_files = [
                    f for f in artifacts_dir.rglob("*")
                    if f.is_file() and not f.parent.name.startswith("_plan")
                ]
                if not real_files:
                    artifact_warning = (
                        "\n\n⚠️ 警告：工作目录中没有产出文件。"
                        "请确认 Worker 是否已正确执行任务。"
                    )
                else:
                    file_list = "\n".join(
                        f"  - {f.relative_to(artifacts_dir)}" for f in real_files
                    )
                    artifact_warning = f"\n\n已验证产出物（{len(real_files)} 个文件）：\n{file_list}"
            elif self._dispatched:
                artifact_warning = (
                    "\n\n⚠️ 警告：已分派 Agent 但找不到工作目录。"
                    "产出物可能未正确保存。"
                )

        return f"[FINALIZE] 任务已完成。摘要：\n{summary}{artifact_warning}"
