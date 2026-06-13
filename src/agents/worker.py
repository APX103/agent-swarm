"""Worker Agent 定义 - 在 Docker 容器中运行的 Agent Server

提供 A2A 兼容的 HTTP 端点，接收 Orchestrator 的任务消息，
使用配置的 LLM 处理任务，将产物写入共享目录。
"""
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# 配置（从环境变量或 config.json 读取）
AGENT_ROLE = os.environ.get("AGENT_ROLE", "general")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-coding-plan")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "9001"))
TASK_ID = os.environ.get("TASK_ID", "")
SHARED_DIR = os.environ.get("SHARED_DIR", "/workspace/artifacts")


# AgentCard 定义
AGENT_CARDS = {
    "frontend-ux-pro": {
        "name": "Frontend UX Pro",
        "description": "专业前端开发 Agent，擅长 UI/UX 设计和 React/Vue/HTML+CSS+JS 实现",
        "url": "",
        "version": "1.0.0",
        "skills": [
            {
                "id": "frontend-dev",
                "name": "Frontend Development",
                "description": "开发响应式前端应用",
                "tags": ["frontend", "react", "vue", "ui", "ux", "css", "html", "javascript"],
            }
        ],
        "capabilities": {"streaming": True},
    },
    "backend-engineer": {
        "name": "Backend Engineer",
        "description": "专业后端开发 Agent，擅长 API 设计、数据库操作和服务端逻辑",
        "url": "",
        "version": "1.0.0",
        "skills": [
            {
                "id": "backend-dev",
                "name": "Backend Development",
                "description": "开发 RESTful API、数据库设计、服务端逻辑",
                "tags": ["backend", "api", "database", "python", "node", "fastapi"],
            }
        ],
        "capabilities": {"streaming": True},
    },
    "general-agent": {
        "name": "General Agent",
        "description": "通用 Agent，处理各种编程和非编程任务",
        "url": "",
        "version": "1.0.0",
        "skills": [
            {
                "id": "general",
                "name": "General Task",
                "description": "处理通用任务",
                "tags": ["general", "coding", "writing"],
            }
        ],
        "capabilities": {"streaming": True},
    },
}

# Worker 的 System Prompt 模板
SYSTEM_PROMPTS = {
    "frontend-ux-pro": """你是一个专业的前端开发 Agent。你的职责是：

1. 根据任务描述开发前端代码（HTML/CSS/JS、React、Vue 等）
2. 确保代码质量高、UI/UX 设计合理
3. 使用 write_file 工具将产出文件保存到当前工作目录
4. 使用合适的工具来完成任务

工作规则：
- write_file 的 path 参数只填写**文件名**（如 "index.html"、"style.css"），不要写目录路径
- 确保代码可以直接运行
- 注释清楚，代码整洁
- 完成后报告产出的文件列表
""",
    "backend-engineer": """你是一个专业的后端开发 Agent。你的职责是：

1. 根据任务描述开发后端代码（Python/FastAPI、Node/Express 等）
2. 设计合理的 API 接口
3. 使用 write_file 工具将产出文件保存到当前工作目录
4. 使用合适的工具来完成任务

工作规则：
- write_file 的 path 参数只填写**文件名**（如 "main.py"、"requirements.txt"），不要写目录路径
- 包含 requirements.txt 或 package.json
- 确保 API 可以启动运行
- 完成后报告产出的文件列表
""",
    "general-agent": """你是一个通用的开发 Agent。你的职责是：

1. 根据任务描述完成编码任务
2. 使用 write_file 工具将产出文件保存到当前工作目录
3. 使用合适的工具来完成任务

工作规则：
- write_file 的 path 参数只填写**文件名**，不要写目录路径
- 确保代码质量和完整性
- 完成后报告产出的文件列表
""",
}


# ========== A2A 兼容的 FastAPI Server ==========

app = FastAPI(title=f"Worker Agent ({AGENT_ROLE})")

# 内存中的任务存储
_tasks: dict[str, dict] = {}


def get_agent_card() -> dict:
    card = AGENT_CARDS.get(AGENT_ROLE, AGENT_CARDS["general-agent"]).copy()
    card["url"] = f"http://localhost:{AGENT_PORT}"
    return card


@app.get("/.well-known/agent.json")
async def well_known_agent():
    return JSONResponse(get_agent_card())


@app.post("/")
async def a2a_endpoint(request: Request):
    """A2A JSON-RPC 2.0 端点"""
    body = await request.json()
    
    method = body.get("method", "")
    params = body.get("params", {})
    request_id = body.get("id", 1)
    
    if method == "message/send":
        return await handle_send_message(params, request_id)
    elif method == "tasks/get":
        return await handle_get_task(params, request_id)
    elif method == "tasks/list":
        return await handle_list_tasks(params, request_id)
    else:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


async def handle_send_message(params: dict, request_id: int) -> JSONResponse:
    """处理 message/send"""
    message_data = params.get("message", {})
    user_text = ""
    for part in message_data.get("parts", []):
        if part.get("kind") == "text":
            user_text += part.get("text", "")
    
    # 创建任务
    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "contextId": TASK_ID,
        "status": {"state": "working", "timestamp": None},
        "history": [message_data],
    }
    _tasks[task_id] = task
    
    # 调用 LLM 处理任务
    try:
        result = await call_llm(user_text)
        
        task["status"] = {"state": "completed"}
        task["history"].append({
            "role": "agent",
            "parts": [{"kind": "text", "text": result}],
            "messageId": str(uuid.uuid4()),
        })
    except Exception as e:
        task["status"] = {"state": "failed"}
        task["history"].append({
            "role": "agent",
            "parts": [{"kind": "text", "text": f"Error: {e}"}],
            "messageId": str(uuid.uuid4()),
        })
    
    config = params.get("configuration", {})
    blocking = config.get("blocking", True)
    
    if blocking:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": task,
        })
    else:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"id": task_id, "status": {"state": "working"}},
        })


async def handle_get_task(params: dict, request_id: int) -> JSONResponse:
    """处理 tasks/get"""
    task_id = params.get("id", "")
    task = _tasks.get(task_id)
    
    if not task:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32001, "message": "Task not found"},
        })
    
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": task,
    })


async def handle_list_tasks(params: dict, request_id: int) -> JSONResponse:
    """处理 tasks/list"""
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": list(_tasks.values()),
    })


async def call_llm(user_message: str) -> str:
    """调用 LLM 处理任务"""
    from openai import OpenAI
    
    system_prompt = SYSTEM_PROMPTS.get(AGENT_ROLE, SYSTEM_PROMPTS["general-agent"])
    
    # 构建文件系统工具
    tools = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "将内容写入文件。路径相对于工作目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件相对路径"},
                        "content": {"type": "string", "description": "文件内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件内容",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件相对路径"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "列出目录中的文件",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录相对路径（可选）"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "在沙箱中执行 shell 命令",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "shell 命令"},
                    },
                    "required": ["command"],
                },
            },
        },
    ]
    
    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        timeout=300.0,
    )
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    
    max_iterations = 20
    final_response = ""
    
    for _ in range(max_iterations):
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=LLM_MODEL,
            messages=messages,
            tools=tools,
            temperature=0.1,
            max_tokens=4096,
        )
        
        choice = response.choices[0]
        msg = choice.message
        messages.append(msg.model_dump())
        
        if not msg.tool_calls:
            final_response = msg.content or ""
            break
        
        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            fn_args = json.loads(tool_call.function.arguments)
            result = execute_file_tool(fn_name, fn_args)
            
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })
    
    return final_response


def execute_file_tool(name: str, args: dict) -> str:
    """执行文件系统工具"""
    # 每次执行时从环境变量读取（确保使用最新值）
    shared_dir = os.environ.get("SHARED_DIR", "/workspace/artifacts")
    role_dir = Path(shared_dir) / AGENT_ROLE.split("-")[0]  # frontend/backend/general
    role_dir.mkdir(parents=True, exist_ok=True)
    
    if name == "write_file":
        # 防止 LLM 返回绝对路径或目录前缀，统一转为相对文件名
        file_name = Path(args["path"]).name  # 只取文件名，去掉任何目录前缀
        path = role_dir / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Written to {file_name} ({len(args['content'])} bytes)"
    
    elif name == "read_file":
        file_name = Path(args["path"]).name
        path = role_dir / file_name
        if not path.exists():
            return f"File not found: {args['path']}"
        content = path.read_text(encoding="utf-8", errors="replace")
        return content[:5000]  # 限制返回长度
    
    elif name == "list_directory":
        # list_directory 直接列出 role_dir（LLM 不需要指定子路径）
        if not role_dir.exists():
            return f"Directory not found"
        files = [str(f.relative_to(role_dir)) for f in role_dir.rglob("*") if f.is_file()]
        return "\n".join(files) if files else "(empty)"
    
    elif name == "run_command":
        import subprocess
        try:
            result = subprocess.run(
                args["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(role_dir),
            )
            output = result.stdout + result.stderr
            return output[:5000] if output else "(no output)"
        except Exception as e:
            return f"Command error: {e}"
    
    return f"Unknown tool: {name}"


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Worker Agent")
    parser.add_argument("--role", default="general")
    parser.add_argument("--model", default="glm-coding-plan")
    parser.add_argument("--base-url", default="https://open.bigmodel.cn/api/coding/paas/v4")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    
    # 设置全局变量
    global AGENT_ROLE, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY, AGENT_PORT, TASK_ID
    AGENT_ROLE = args.role
    LLM_MODEL = args.model
    LLM_BASE_URL = args.base_url
    LLM_API_KEY = args.api_key
    AGENT_PORT = args.port
    TASK_ID = args.task_id
    
    import uvicorn
    logger.info(f"Starting Worker Agent: role={AGENT_ROLE}, port={AGENT_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT)


if __name__ == "__main__":
    main()
