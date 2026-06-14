# 可靠投递与优雅降级 (Reliable Delivery & Graceful Degradation) — GOAL

- **状态**：自主执行（用户预授权 3 个 wave，不中途确认）
- **关联**：主系列 `2026-06-14-agent-scheduling-optimization-goal.md`；进度计数 `iteration-progress.md`

## 目标声明
在不引入新依赖、不破坏公开 API 的前提下，补齐「投递可靠 + 失败可观测 + 全链路失败能降级」三件事：

- **W9 幂等性**：`/api/chat` 支持可选 `Idempotency-Key`；同一 key 重复请求复用已有 task，不重复编排。
- **W10 死信**：编排失败时记录死信（task_id / tenant / error / message / 时间），可查询，便于排查与重放。
- **W11 优雅降级 L2**：Dispatcher 缓存成功结果；当某 agent_type 所有候选全失败时，命中缓存则降级返回（显式标注 degraded），避免硬失败。

## 约束（同主系列宪法）
- 无新依赖；不破坏公开 HTTP API（新增 header / endpoint / 可选字段允许）。
- 防御式、错误显式传播、资源上下文管理、日志完整、类型提示。
- KISS；存储用进程内（in-memory），跨进程/持久化（Redis）列为后续。

## 每 wave DoD
- TDD：先写失败测试 → 实现 → 绿。
- `pytest tests/` 全绿（只增不删）。
- 单独 commit。

## 验证策略
- W9：同 key 复用 / 无 key 总新建 / 不同 key 不同 task。
- W10：DeadLetterStore record/recent/bounded；编排失败路径落死信。
- W11：成功入缓存；全失败 + 命中→降级 success(degraded=True)；全失败 + 未命中→失败。

## 变更记录
- 2026-06-14：目标创建。开始 W9。
