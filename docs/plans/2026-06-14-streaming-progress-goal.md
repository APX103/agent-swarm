# 流式任务进度 (Streaming Task Progress) — GOAL

- **状态**：自主执行（用户预授权，不中途确认）
- **关联**：主系列已合并入 main（`9c3b842`）；进度计数 `iteration-progress.md`

## 背景与问题
当前 worker（`src/agents/worker.py`）的 A2A `message/send` 即便 `configuration.blocking=false`，也会在返回前把整个 `call_llm`（LLM tool-calling 循环）跑完——"非阻塞"名存实亡。后果：编排器拿不到 worker 中途进度，WebSocket 只能看到编排器自己的 thinking，看不到 worker 内部步骤。

## 目标声明（3 wave）
- **W12 A2A 轮询原语**：`A2AClient.poll_task(task_id, interval, timeout) -> AsyncIterator[A2ATask]`，按 interval 调 `tasks/get`，状态/消息变化时 yield，终态（completed/failed/canceled）停止。respx 可测。
- **W13 worker 后台执行 + 进度**：`message/send` 非阻塞时把 `call_llm` 放后台 task 立即返回；LLM 循环每步经 `on_progress` 回调把进度写入 task 记录；`tasks/get` 可轮询到中途进度。把循环抽成可测函数。
- **W14 编排器转发进度**：非阻塞 dispatch 后轮询 worker 任务，经 `event_callback` 发 `agent_progress` 事件到 WebSocket。

## 约束（同宪法）
- 无新依赖；不破坏公开 API；防御式/显式/资源上下文/日志/类型；KISS。
- worker 改动以"抽出可测函数 + 行为兼容（blocking 仍可用）"为原则。

## 每 wave DoD
- TDD：先失败测试 → 实现 → 绿；`pytest tests/` 全绿；单独 commit。

## 变更记录
- 2026-06-14：目标创建。开始 W12。
- 2026-06-14：**W12 + W13 + W14 全部完成（TDD，自主执行）。** 249 passed。commits `ac895f2`（W12）/ `4a4c411`（W13）/ `3183075`（W14）。目标达成：worker 真非阻塞 + 步骤进度写入 task 记录 + A2A poll_task 轮询 + DockerBackend 流式 + 编排器转发 `agent_progress` 到 WebSocket。
