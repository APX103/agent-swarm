#!/usr/bin/env python3
"""Mock A2A worker — a lightweight A2A server for local demo/testing.

Spins up N fake agents on consecutive ports, each answering the A2A JSON-RPC
contract (message/send, tasks/get, tasks/cancel, /.well-known/agent.json) that
the real worker uses. Each agent just echoes a canned reply tagged with its
role, so you can exercise the full Swarm pipeline (registration → dispatch →
streaming → artifacts) without real LLM calls or 10 Docker containers.

Usage:
    # start 3 mock agents on ports 9001-9003 with roles from agents/*.yaml
    python agents/mock-a2a-worker.py --start-port 9001 --count 3

    # or pick specific roles
    python agents/mock-a2a-worker.py --roles frontend-engineer,backend-engineer

Then point Swarm at them (agents/*.yaml endpoints already default to
http://localhost:9001..9010).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Role id → display name + canned reply. Keeps the demo self-contained.
ROLE_INFO: dict[str, dict] = {
    "frontend-engineer": {"name": "Frontend Engineer", "reply": "已生成前端代码：index.html + style.css + app.js（响应式、无障碍）。"},
    "backend-engineer": {"name": "Backend Engineer", "reply": "已生成后端代码：main.py (FastAPI) + requirements.txt，4 个 REST 接口可用。"},
    "fullstack-engineer": {"name": "Fullstack Engineer", "reply": "已端到端打通：后端 API + 前端 UI + 联调，可直接运行。"},
    "devops-engineer": {"name": "DevOps Engineer", "reply": "已生成 Dockerfile + docker-compose.yml + .github/workflows/ci.yml。"},
    "qa-engineer": {"name": "QA Engineer", "reply": "已编写测试：test_api.py（单元）+ test_e2e.py（端到端），覆盖率 85%。"},
    "security-engineer": {"name": "Security Engineer", "reply": "安全审查完成：发现 2 个中危（CORS 过宽、缺输入校验），已给出修复建议。"},
    "data-engineer": {"name": "Data Engineer", "reply": "已生成 ETL 脚本 pipeline.py + schema.sql，数据管道可跑通。"},
    "mobile-engineer": {"name": "Mobile Engineer", "reply": "已生成 React Native 工程：App.tsx + 2 个屏幕组件，iOS/Android 适配。"},
    "tech-writer": {"name": "Technical Writer", "reply": "已生成 README.md + API.md + 架构图说明，含快速开始与示例。"},
    "design-reviewer": {"name": "Design Reviewer", "reply": "UI/UX 评审完成：视觉一致性良好，建议改进表单错误状态与加载骨架屏。"},
}


def make_app(role_id: str, port: int) -> FastAPI:
    """Build a single-role A2A server app."""
    info = ROLE_INFO.get(role_id, {"name": role_id, "reply": f"[{role_id}] 已处理任务。"})
    tasks: dict[str, dict] = {}
    bg_tasks: dict[str, asyncio.Task] = {}

    app = FastAPI(title=f"Mock A2A Worker ({role_id})")

    @app.get("/.well-known/agent.json")
    async def agent_card():
        return JSONResponse({
            "name": info["name"],
            "description": f"Mock A2A worker for {role_id}",
            "url": f"http://localhost:{port}",
            "version": "1.0.0",
            "skills": [{"id": role_id, "name": info["name"], "tags": [role_id]}],
            "capabilities": {"streaming": True},
        })

    @app.post("/")
    async def a2a_endpoint(request: Request):
        body = await request.json()
        method = body.get("method", "")
        params = body.get("params", {})
        request_id = body.get("id", 1)

        if method == "message/send":
            return await _handle_send(params, request_id, role_id, tasks, bg_tasks, info)
        if method == "tasks/get":
            return _handle_get(params, request_id, tasks)
        if method == "tasks/list":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": list(tasks.values())})
        if method == "tasks/cancel":
            return await _handle_cancel(params, request_id, tasks, bg_tasks)
        return JSONResponse({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })

    return app


async def _handle_send(params, request_id, role_id, tasks, bg_tasks, info):
    msg = params.get("message", {})
    user_text = ""
    for part in msg.get("parts", []):
        if part.get("kind") == "text":
            user_text += part.get("text", "")
    blocking = params.get("configuration", {}).get("blocking", True)

    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id, "contextId": role_id,
        "status": {"state": "working"},
        "history": [msg], "progress": [],
    }
    tasks[task_id] = task

    async def run():
        # simulate 2 progress steps then complete
        await asyncio.sleep(0.2)
        task["progress"].append({"step": 0, "type": "assistant", "content": "分析需求…"})
        await asyncio.sleep(0.2)
        task["progress"].append({"step": 1, "type": "tool", "tool": "write_file", "result": "ok"})
        await asyncio.sleep(0.2)
        task["status"] = {"state": "completed"}
        task["history"].append({
            "role": "agent",
            "parts": [{"kind": "text", "text": info["reply"]}],
            "messageId": str(uuid.uuid4()),
        })

    if blocking:
        await run()
    else:
        bg_tasks[task_id] = asyncio.create_task(run())

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})


def _handle_get(params, request_id, tasks):
    task_id = params.get("id", "")
    task = tasks.get(task_id)
    if not task:
        return JSONResponse({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32001, "message": "Task not found"},
        })
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})


async def _handle_cancel(params, request_id, tasks, bg_tasks):
    task_id = params.get("id", "")
    bg = bg_tasks.get(task_id)
    if bg and not bg.done():
        bg.cancel()
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"id": task_id, "status": "canceled"}})
    return JSONResponse({
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32001, "message": "Task not found or already done"},
    })


def _serve_role(role_id: str, port: int):
    app = make_app(role_id, port)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def main():
    parser = argparse.ArgumentParser(description="Mock A2A worker(s) for local Swarm demo")
    parser.add_argument("--start-port", type=int, default=9001)
    parser.add_argument("--count", type=int, default=0, help="number of agents (uses first N roles)")
    parser.add_argument("--roles", default="", help="comma-separated role ids (overrides --count)")
    args = parser.parse_args()

    if args.roles:
        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    else:
        roles = list(ROLE_INFO.keys())[: args.count] if args.count else list(ROLE_INFO.keys())

    if not roles:
        parser.error("provide --count N or --roles a,b,c")

    print(f"Starting {len(roles)} mock A2A agent(s):")
    threads = []
    for i, role in enumerate(roles):
        port = args.start_port + i
        print(f"  - {role:<20} http://localhost:{port}")
        t = threading.Thread(target=_serve_role, args=(role, port), daemon=True)
        t.start()
        threads.append(t)

    print("\nPress Ctrl+C to stop.")
    try:
        signal.pause() if hasattr(signal, "pause") else threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down mock agents.")


if __name__ == "__main__":
    main()
