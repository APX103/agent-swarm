"""Worker Agent 定义 - 在 Docker 容器中运行的 Agent Server

提供 A2A 兼容的 HTTP 端点，接收 Orchestrator 的任务消息，
使用配置的 LLM 处理任务，将产物写入共享目录。
"""
import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from agent_loop import run_agent_loop  # container script layout (/app/agents)
except ImportError:
    from src.agents.agent_loop import run_agent_loop  # repo layout

logger = logging.getLogger(__name__)

# 配置（从环境变量或 config.json 读取）
AGENT_ROLE = os.environ.get("AGENT_ROLE", "general")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-coding-plan")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "9001"))
TASK_ID = os.environ.get("TASK_ID", "")
SHARED_DIR = os.environ.get("SHARED_DIR", "/workspace/artifacts")

# Per-request shared_dir（contextvars 天然隔离并发请求，不会被互相覆盖）
# reload_config 设置它；execute_file_tool 读它。
# 注意：default 用 lambda 读 os.environ，兼容 monkeypatch.setenv 的测试。
_request_shared_dir: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_request_shared_dir",
    default=os.environ.get("SHARED_DIR", "/workspace/artifacts"),
)

CONFIG_FILE = "/etc/swarm/config.json"


def reload_config() -> None:
    """热重载 /etc/swarm/config.json 到当前进程环境与模块全局变量。

    容器池在每次 checkout 时会重写 config.json，但 Worker 进程是 warm 保留的，
    因此每个请求到来前必须重新读取最新配置，尤其是 shared_dir 与 agent_role。
    """
    global AGENT_ROLE, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY, AGENT_PORT, TASK_ID, SHARED_DIR
    try:
        if not Path(CONFIG_FILE).is_file():
            return
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.debug("reload_config failed: %s", e)
        return

    def _set(key: str, default: str) -> str:
        val = cfg.get(key, default)
        if val is not None:
            os.environ[key.upper()] = str(val)
        return str(val) if val is not None else default

    AGENT_ROLE = _set("agent_role", AGENT_ROLE)
    LLM_MODEL = _set("model", LLM_MODEL)
    LLM_BASE_URL = _set("base_url", LLM_BASE_URL)
    LLM_API_KEY = _set("api_key", LLM_API_KEY)
    AGENT_PORT = int(_set("port", str(AGENT_PORT)))
    TASK_ID = _set("task_id", TASK_ID)
    SHARED_DIR = _set("shared_dir", SHARED_DIR)
    # 同步到 per-request contextvar（并发隔离的真正来源）
    _request_shared_dir.set(SHARED_DIR)
    if "system_prompt" in cfg:
        os.environ["AGENT_SYSTEM_PROMPT"] = str(cfg["system_prompt"])
    logger.info("Reloaded config: role=%s task=%s shared_dir=%s", AGENT_ROLE, TASK_ID, SHARED_DIR)


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
    "frontend-ux-pro": """你是一位资深前端架构师，拥有 10 年以上的 UI/UX 开发经验。

## 核心能力
- 响应式设计（Mobile-first, CSS Grid/Flexbox, 媒体查询）
- 现代 JavaScript/TypeScript（ES2023+, async/await, 模块化）
- 框架开发（React、Vue 3 Composition API、Svelte）
- 原生开发（HTML5 语义化标签、CSS3 自定义属性、Vanilla JS）
- UI/UX 最佳实践（无障碍 a11y、暗色模式、动效设计）

## 工作流程
1. **分析需求**：理解要做什么、面向什么用户、核心交互是什么
2. **设计结构**：先规划页面结构（HTML 语义化）、再设计样式系统（CSS 变量/设计令牌）
3. **实现代码**：按结构逐步实现，先骨架后样式，最后交互逻辑
4. **自我验证**：检查代码完整性、可运行性、响应式适配

## 代码规范
- HTML：使用语义化标签（header/nav/main/section/article/footer），必须包含 viewport meta
- CSS：使用 CSS 自定义属性（变量）定义主题色/间距/字体；避免 !important
- JS：使用 const/let，禁止 var；使用 addEventListener 而非 onclick 属性
- 文件组织：index.html + style.css + script.js（或组件化结构）

## 质量要求
- 代码可以直接在浏览器中运行，无 console error
- 响应式：至少支持 mobile（<768px）和 desktop（>=768px）两个断点
- 颜色对比度符合 WCAG AA 标准
- 包含合理的 loading 状态和错误处理

## 如果收到了共享上下文中的 API 契约
- 严格按契约定义的 endpoint、请求参数、响应格式来对接
- 使用 fetch() 调用 API，baseUrl 设为 '/api' 或指定地址
- 处理好 loading、error、empty 三种状态

## 文件操作
- write_file 的 path 可以是文件名或相对路径（如 "src/components/Header.jsx"）
- 所有文件保存到你的工作目录下
- 完成后用 list_directory 确认文件列表，报告产出物
""",
    "backend-engineer": """你是一位资深后端架构师，精通 API 设计、数据库建模和服务端工程实践。

## 核心能力
- RESTful API 设计（资源建模、HTTP 语义、状态码、分页/过滤）
- Python 生态（FastAPI、Pydantic、SQLAlchemy、asyncio）
- Node.js 生态（Express、Koa、Prisma、TypeScript）
- 数据库设计（关系型范式、索引优化、迁移策略）
- 安全实践（输入校验、SQL 注入防护、CORS、JWT/OAuth）

## 工作流程
1. **分析需求**：明确需要哪些资源、什么操作、数据模型是什么
2. **设计 API**：定义 endpoint、请求/响应 schema、错误码
3. **实现代码**：先数据模型/schema，再路由/控制器，最后中间件
4. **自我验证**：确保代码可启动、API 可调用、依赖完整

## 代码规范（Python/FastAPI）
- 使用 Pydantic 定义请求/响应模型，类型注解完整
- 路由使用 APIRouter 组织，按资源分组
- 异步优先：async def + async DB driver
- 错误处理：使用 HTTPException，统一错误响应格式
- 包含 requirements.txt，所有依赖明确列出版本

## 代码规范（Node.js/Express）
- 使用 Express Router 组织路由
- 中间件链：cors → body-parser → routes → error-handler
- 异步路由包装 asyncHandler，避免未捕获的 Promise rejection
- 包含 package.json，scripts 含 "start" 命令

## 质量要求
- 服务可以直接启动（python -m uvicorn main:app 或 npm start）
- API 返回标准 JSON，包含合理的 HTTP 状态码
- 输入校验完整，不允许裸用户输入直入数据库
- CORS 配置正确（至少允许 localhost 前端）

## 如果收到了共享上下文中的 API 契约
- 严格按契约定义的 endpoint 路径、方法、请求/响应格式来实现
- 这是前后端的共同协议，不可偏离

## 文件操作
- write_file 的 path 可以是文件名或相对路径（如 "routers/users.py"）
- 所有文件保存到你的工作目录下
- 完成后用 list_directory 确认文件列表，报告产出物
""",
    "general-agent": """你是一位全能型软件工程师，擅长快速学习和解决各类技术问题。

## 核心能力
- 多语言编程（Python、JavaScript、Shell、SQL）
- 技术文档撰写（README、API 文档、架构设计）
- 数据处理与分析（脚本编写、数据清洗、格式转换）
- DevOps 任务（Dockerfile、CI/CD 脚本、部署配置）
- 测试编写（单元测试、集成测试）

## 工作流程
1. **理解任务**：明确目标、约束条件、成功标准
2. **分析方案**：考虑多种实现方案，选择最简洁有效的
3. **实现执行**：逐步实现，保持代码简洁可读
4. **自我验证**：检查输出完整性和正确性

## 代码规范
- 遵循目标语言的最佳实践和惯用法
- 函数职责单一，命名清晰
- 包含必要的错误处理和边界检查
- 注释解释"为什么"而不是"做什么"

## 文件操作
- write_file 的 path 可以是文件名或相对路径
- 所有文件保存到你的工作目录下
- 完成后用 list_directory 确认文件列表，报告产出物
""",
}


# ========== A2A 兼容的 FastAPI Server ==========

app = FastAPI(title=f"Worker Agent ({AGENT_ROLE})")

# 内存中的任务存储
_tasks: dict[str, dict] = {}
# 后台执行的任务（用于取消）
_bg_tasks: dict[str, asyncio.Task] = {}

# 任务 TTL：终态任务保留 5 分钟后清理，防止内存无限增长
_TASK_TTL_SECONDS = 300
_last_cleanup = 0.0


def _cleanup_old_tasks():
    """清理超过 TTL 的终态任务（每个请求最多触发一次/分钟）。"""
    import time as _time
    global _last_cleanup
    now = _time.time()
    if now - _last_cleanup < 60:
        return  # 节流：最多每分钟清理一次
    _last_cleanup = now
    terminal = ("completed", "failed", "canceled")
    expired = [
        tid for tid, t in _tasks.items()
        if t.get("status", {}).get("state") in terminal
        and (now - t.get("_completed_at", now)) > _TASK_TTL_SECONDS
    ]
    for tid in expired:
        _tasks.pop(tid, None)
        _bg_tasks.pop(tid, None)
    if expired:
        logger.debug("Cleaned up %d expired tasks from worker memory", len(expired))


def resolve_card(role: str) -> dict:
    """Agent card for *role*: prefer an env-injected card (WORKER_ROLE_CARD JSON,
    e.g. provided by the pool from config), else built-in, else general-agent.

    This lets a config-defined role reach the worker without editing worker.py.
    """
    injected = os.environ.get("WORKER_ROLE_CARD")
    if injected:
        try:
            return json.loads(injected)
        except Exception:
            logger.warning("Bad WORKER_ROLE_CARD JSON; falling back to built-in")
    return AGENT_CARDS.get(role, AGENT_CARDS["general-agent"])


def resolve_prompt(role: str) -> str:
    """System prompt for *role*: prefer env AGENT_SYSTEM_PROMPT (pool-injected),
    else built-in, else general-agent."""
    injected = os.environ.get("AGENT_SYSTEM_PROMPT")
    if injected:
        return injected
    return SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["general-agent"])


def get_agent_card() -> dict:
    card = resolve_card(AGENT_ROLE).copy()
    card["url"] = f"http://localhost:{AGENT_PORT}"
    return card


@app.get("/.well-known/agent.json")
async def well_known_agent() -> JSONResponse:
    return JSONResponse(get_agent_card())


@app.post("/")
async def a2a_endpoint(request: Request) -> JSONResponse:
    """A2A JSON-RPC 2.0 端点"""
    reload_config()
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        })

    method = body.get("method", "")
    params = body.get("params", {})
    request_id = body.get("id", 1)
    
    if method == "message/send":
        return await handle_send_message(params, request_id)
    elif method == "tasks/get":
        return await handle_get_task(params, request_id)
    elif method == "tasks/list":
        return await handle_list_tasks(params, request_id)
    elif method == "tasks/cancel":
        return await handle_cancel_task(params, request_id)
    else:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


async def handle_send_message(params: dict, request_id: int) -> JSONResponse:
    """处理 message/send。

    blocking=True：同步执行完毕，返回完整 task。
    blocking=False：立即返回 working，后台执行并把步骤进度写入 task 记录
                   （供 tasks/get 轮询），完成/失败时更新 status。
    """
    # 清理过期的终态任务（防止内存泄漏）
    _cleanup_old_tasks()

    message_data = params.get("message", {})
    user_text = ""
    for part in message_data.get("parts", []):
        if part.get("kind") == "text":
            user_text += part.get("text", "")

    config = params.get("configuration", {})
    blocking = config.get("blocking", True)

    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "contextId": TASK_ID,
        "status": {"state": "working", "timestamp": None},
        "history": [message_data],
        "progress": [],
    }
    _tasks[task_id] = task

    def on_progress(event: dict) -> None:
        task["progress"].append(event)

    async def _run() -> None:
        try:
            result = await call_llm(user_text, on_progress=on_progress)
            task["status"] = {"state": "completed"}
            task["_completed_at"] = time.time()
            task["history"].append({
                "role": "agent",
                "parts": [{"kind": "text", "text": result}],
                "messageId": str(uuid.uuid4()),
            })
        except Exception as e:
            logger.exception("Worker task %s failed", task_id)
            task["status"] = {"state": "failed"}
            task["_completed_at"] = time.time()
            task["history"].append({
                "role": "agent",
                "parts": [{"kind": "text", "text": f"Error: {e}"}],
                "messageId": str(uuid.uuid4()),
            })

    if blocking:
        await _run()
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})

    # non-blocking: run in background, return immediately
    bg = asyncio.create_task(_run())
    _bg_tasks[task_id] = bg
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


async def handle_cancel_task(params: dict, request_id: int) -> JSONResponse:
    """处理 tasks/cancel —— 取消后台执行的 call_llm，避免继续烧 token"""
    task_id = params.get("id", "")
    bg = _bg_tasks.get(task_id)
    if bg and not bg.done():
        bg.cancel()
        logger.info("Cancelled background task %s", task_id)
        return JSONResponse({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"id": task_id, "status": "canceled"},
        })
    return JSONResponse({
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32001, "message": f"Task {task_id} not found or already done"},
    })


async def call_llm(user_message: str, on_progress=None) -> str:
    """调用 LLM 处理任务（委托给可测的 run_agent_loop）。"""
    from openai import OpenAI

    system_prompt = resolve_prompt(AGENT_ROLE)

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
        {
            "type": "function",
            "function": {
                "name": "read_shared_file",
                "description": "只读读取共享任务目录中的文件（含其他 Agent 的产出，如 backend/api.py 或 _plan/project_plan.md）。路径相对任务根目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对任务根的文件路径"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_shared",
                "description": "只读列出共享任务目录中的所有文件（含其他 Agent 的产出）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "子目录（可选）"},
                    },
                },
            },
        },
    ]

    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        timeout=300.0,
    )

    async def llm_call(messages):
        return await asyncio.to_thread(
            client.chat.completions.create,
            model=LLM_MODEL,
            messages=messages,
            tools=tools,
            temperature=0.3,
            max_tokens=8192,
        )

    try:
        return await run_agent_loop(
            llm_call=llm_call,
            execute_tool=execute_file_tool,
            system_prompt=system_prompt,
            user_message=user_message,
            max_iterations=20,
            on_progress=on_progress,
        )
    finally:
        client.close()


def execute_file_tool(name: str, args: dict) -> str:
    """执行文件系统工具"""
    # 从 per-request contextvar 读取（并发隔离，不会被其他请求覆盖）；
    # 如果 contextvar 未被 reload_config 设置过（如测试），回退到 os.environ。
    shared_dir = _request_shared_dir.get()
    if not shared_dir or shared_dir == "/workspace/artifacts":
        # 可能是未被 reload_config 设置的默认值——检查 os.environ（兼容测试/首次启动）
        env_dir = os.environ.get("SHARED_DIR")
        if env_dir:
            shared_dir = env_dir
    role_prefix = AGENT_ROLE.split("-")[0]  # frontend/backend/general
    role_dir = Path(shared_dir) / role_prefix
    role_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(file_path: str) -> Path:
        """解析文件路径，避免双层 role 目录（backend/backend/main.py）。

        LLM 可能传 "backend/main.py" 或 "main.py"——如果已有 role 前缀就不再拼。
        """
        p = file_path.lstrip("/")
        # 如果路径以 role 前缀开头（如 "backend/main.py"），直接用 role_dir 的父级
        parts = p.split("/")
        if parts[0] == role_prefix:
            # 去掉重复的 role 前缀，拼到 shared_dir 下
            return Path(shared_dir) / p
        return role_dir / p
    
    if name == "write_file":
        try:
            path = _resolve_path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return f"Written to {args['path']} ({len(args['content'])} bytes)"
        except OSError as e:
            return f"Error writing {args['path']}: {e}"

    elif name == "read_file":
        try:
            path = _resolve_path(args["path"])
            if not path.exists():
                return f"File not found: {args['path']}"
            content = path.read_text(encoding="utf-8", errors="replace")
            return content[:5000]  # 限制返回长度
        except OSError as e:
            return f"Error reading {args['path']}: {e}"

    elif name == "list_directory":
        try:
            sub_path = args.get("path", "")
            if sub_path:
                target_dir = _resolve_path(sub_path)
            else:
                target_dir = role_dir
            if not target_dir.exists():
                return f"Directory not found: {sub_path}"
            files = [str(f.relative_to(Path(shared_dir))) for f in target_dir.rglob("*") if f.is_file()]
            return "\n".join(files) if files else "(empty)"
        except OSError as e:
            return f"Error listing directory {sub_path}: {e}"
    
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
        except (OSError, subprocess.SubprocessError) as e:
            return f"Command error: {e}"

    elif name == "read_shared_file":
        # 只读：读取共享任务目录（含其他 Agent 产出），路径相对任务根。
        try:
            path = Path(shared_dir) / args["path"]
            if not path.exists():
                return f"File not found: {args['path']}"
            return path.read_text(encoding="utf-8", errors="replace")[:5000]
        except OSError as e:
            return f"Error reading shared file {args['path']}: {e}"

    elif name == "list_shared":
        try:
            target_dir = Path(shared_dir)
            sub_path = args.get("path", "")
            if sub_path:
                target_dir = target_dir / sub_path
            if not target_dir.exists():
                return f"Directory not found: {sub_path}"
            files = [str(f.relative_to(shared_dir)) for f in target_dir.rglob("*") if f.is_file()]
            return "\n".join(files) if files else "(empty)"
        except OSError as e:
            return f"Error listing shared directory {sub_path}: {e}"

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
    parser.add_argument("--shared-dir", default="", help="共享产物目录（容器内路径）；不传则用 SHARED_DIR 环境变量或默认值")
    args = parser.parse_args()

    # 设置全局变量
    global AGENT_ROLE, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY, AGENT_PORT, TASK_ID, SHARED_DIR
    AGENT_ROLE = args.role
    LLM_MODEL = args.model
    LLM_BASE_URL = args.base_url
    LLM_API_KEY = args.api_key
    AGENT_PORT = args.port
    TASK_ID = args.task_id
    if args.shared_dir:
        SHARED_DIR = args.shared_dir
        os.environ["SHARED_DIR"] = args.shared_dir
        _request_shared_dir.set(args.shared_dir)

    import uvicorn
    logger.info(f"Starting Worker Agent: role={AGENT_ROLE}, port={AGENT_PORT}, shared_dir={SHARED_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT)


if __name__ == "__main__":
    main()
