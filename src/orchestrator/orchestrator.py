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
from typing import Optional
from dataclasses import dataclass

from openai import OpenAI

from src.common.a2a_client import A2AClient, A2AMessage
from src.container_pool.pool import ContainerPoolManager, PooledContainer

logger = logging.getLogger(__name__)

ORCHESTRATOR_SYSTEM_PROMPT = """你是一个 Agent Swarm 编排器。你的职责是：

1. 分析用户的请求，理解需要完成什么任务
2. 将任务拆解为子任务，分配给合适的 Worker Agent
3. 通过调用工具来启动 Worker Agent 并发送任务
4. 监控 Worker 的进度
5. 汇总所有 Worker 的结果，给用户一个完整的回答

你必须使用以下工具来完成工作：
- `dispatch_agent`: 启动一个 Worker Agent 并向它发送任务
- `check_agent_status`: 查询 Worker Agent 的任务状态
- `read_artifacts`: 读取共享目录中的文件内容
- `finalize`: 汇总结果，完成任务

工作流程（严格按顺序执行）：
1. 分析用户请求，决定需要哪些类型的 Agent
2. 立即调用 dispatch_agent 启动需要的 Worker（每次一个）
3. 等待 Worker 完成（通过 check_agent_status 轮询）
4. 读取 Worker 产出（通过 read_artifacts）
5. 如果有问题，继续 dispatch_agent 让 Worker 修复
6. 所有工作完成后，调用 finalize 返回最终结果

重要：你必须调用工具来完成任务，不要直接用文字回复。第一件事就是调用 dispatch_agent。

可用的 Agent 类型：
- "frontend-ux-pro": 前端开发（HTML/CSS/JS/React/Vue）
- "backend-engineer": 后端开发（API/数据库/Python/FastAPI）
- "general-agent": 通用任务

规则：
- 始终使用 dispatch_agent 启动 Agent，不要自己生成代码
- 如果 dispatch_agent 返回错误，分析原因并重试
- 所有工作完成后必须调用 finalize
"""


@dataclass
class DispatchedAgent:
    """已分派的 Agent"""
    container: PooledContainer
    agent_card_id: str
    a2a_task_id: Optional[str] = None
    a2a_client: Optional[A2AClient] = None


class Orchestrator:
    """编排器 Agent"""
    
    def __init__(self, settings, pool_manager: ContainerPoolManager, task_manager=None):
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
        
        # 当前任务的上下文
        self._messages: list[dict] = []
        self._dispatched: dict[str, DispatchedAgent] = {}  # task_id -> DispatchedAgent
        self._current_task_id: Optional[str] = None
        self._current_tenant_id: Optional[str] = None
        
        # 工具定义
        self._tools = self._define_tools()
    
    def _define_tools(self) -> list[dict]:
        """定义 OpenAI function calling 工具"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "dispatch_agent",
                    "description": "启动一个 Worker Agent 并向它发送任务。可用的 agent_type 有：" + 
                                   ", ".join(ac.name for ac in self.settings.agent_cards),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_type": {
                                "type": "string",
                                "description": "Agent 类型 ID",
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
        
        max_iterations = 15  # 防止无限循环（减少到15，更快结束）
        final_result = "任务执行完毕，但未产生最终结果。"
        
        for i in range(max_iterations):
            await emit("orchestrator_thinking", {"iteration": i})
            
            # 如果已经分配了 agent 且在最后几轮，检查是否有足够的结果来 finalize
            if i == max_iterations - 3 and self._dispatched:
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
                    tools=self._tools if i < max_iterations - 2 else None,  # 最后两轮不带工具，强制文本回复
                    temperature=0.1,
                    max_tokens=4096,
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
                
                # 如果是 finalize，提取结果并退出
                if fn_name == "finalize":
                    try:
                        args = json.loads(fn_args) if isinstance(fn_args, str) else fn_args
                        final_result = args.get("summary", result)
                    except Exception:
                        final_result = result
                    break
            
            # 检查是否 finalize 已经被调用
            if any(tc.function.name == "finalize" for tc in msg.tool_calls):
                break
        
        # 清理：归还所有容器
        for dispatch_id, dispatched in self._dispatched.items():
            try:
                await self.pool.return_container(dispatched.container.container_id)
                if dispatched.a2a_client:
                    await dispatched.a2a_client.close()
            except Exception as e:
                logger.error(f"Error returning container: {e}")
        self._dispatched.clear()
        
        return final_result
    
    async def _execute_tool(self, name: str, args: dict) -> str:
        """执行工具调用"""
        if name == "dispatch_agent":
            return await self._tool_dispatch_agent(args)
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
    
    async def _tool_dispatch_agent(self, args: dict) -> str:
        """dispatch_agent 工具实现"""
        agent_type = args.get("agent_type", "")
        task = args.get("task", "")
        
        # 从池中 checkout 容器
        container = await self.pool.checkout(
            agent_card_id=agent_type,
            task_id=self._current_task_id,
            model=self.settings.llm.default_model,
            base_url=self.settings.llm.default_base_url,
            api_key=self.settings.llm.default_api_key,
            tenant_id=self._current_tenant_id,
        )
        
        if not container:
            return "错误：没有可用的 Worker 容器。请稍后重试或减少并行 Worker 数量。"
        
        dispatch_id = str(uuid.uuid4())[:8]
        
        # 创建 A2A 客户端（通过宿主机映射端口访问容器）
        a2a_url = f"http://localhost:{container.port}"
        a2a_client = A2AClient(a2a_url, timeout=300.0)
        
        # 发送任务
        message = A2AMessage(role="user", text=task)
        a2a_task = await a2a_client.send_message(message, blocking=True)
        
        dispatched = DispatchedAgent(
            container=container,
            agent_card_id=agent_type,
            a2a_task_id=a2a_task.task_id if a2a_task else None,
            a2a_client=a2a_client,
        )
        self._dispatched[dispatch_id] = dispatched
        
        state = a2a_task.state if a2a_task else "unknown"
        result_text = a2a_task.message if a2a_task else "无响应"
        
        return (f"已分派 Agent '{agent_type}'（dispatch_id={dispatch_id}）\n"
                f"容器: {container.container_name}:{container.port}\n"
                f"任务状态: {state}\n"
                f"Agent 响应: {result_text[:500]}")
    
    async def _tool_check_agent_status(self, args: dict) -> str:
        """check_agent_status 工具实现"""
        dispatch_id = args.get("dispatch_id", "")
        dispatched = self._dispatched.get(dispatch_id)
        
        if not dispatched:
            return f"错误：未找到分派 {dispatch_id}"
        
        if not dispatched.a2a_client:
            return f"Agent {dispatch_id} 无 A2A 连接"
        
        if dispatched.a2a_task_id:
            task = await dispatched.a2a_client.get_task(dispatched.a2a_task_id)
            if task:
                return (f"Agent '{dispatched.agent_card_id}'（{dispatch_id}）状态:\n"
                        f"  状态: {task.state}\n"
                        f"  最新消息: {task.message[:500] if task.message else '无'}")
        
        return f"Agent '{dispatched.agent_card_id}'（{dispatch_id}）无任务状态信息"
    
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
        """finalize 工具实现"""
        summary = args.get("summary", "")
        return f"[FINALIZE] 任务已完成。摘要：\n{summary}"
