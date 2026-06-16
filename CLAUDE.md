# 项目开发规范

## 绝对禁止（ZERO TOLERANCE）
1. **禁止引入新依赖** —— 除非现有依赖完全无法满足，且经过显式确认
2. **禁止破坏现有 API 接口** —— 公开端点必须保持向后兼容；新增字段可选、不删旧字段
3. **禁止添加未要求的功能** —— 只做被要求的事；如果看到可以"顺便做"的事情，先问
4. **禁止删除现有测试** —— 只能新增或修复测试，不能删除
5. **禁止提交 secrets** —— API key、密码、.env、config/default.yaml 不进 git

## 开发原则
1. **防御式编程** —— 所有输入必须验证，所有边界必须处理
2. **错误传播** —— 错误必须显式返回或记录，不能静默吞掉（no bare `except:`）
3. **资源管理** —— 文件、网络、A2A client 必须显式关闭或使用上下文管理器
4. **日志完整** —— 关键操作必须有日志，错误必须有堆栈信息（`exc_info=True`）
5. **并发安全** —— 共享状态（session messages、worker 全局变量）必须加锁或用 contextvar
6. **类型提示** —— 新增/修改的函数必须加类型提示

## 质量标准（完成 checklist）
- [ ] 所有现有测试通过（`pytest tests/ -q`）
- [ ] 新增边界情况测试
- [ ] 无 magic number，无硬编码路径（特别是 `/home/xxx`）
- [ ] 产物盘/容器配置不进 git（已 gitignore）

## 技术栈
- **语言**: Python 3.12+
- **Web 框架**: FastAPI + Uvicorn
- **数据库**: SQLite（WAL 模式，stdlib sqlite3，无 ORM）
- **缓存/注册中心**: Redis 7（aioredis 风格的 async 调用）
- **容器**: Docker（warm pool 管理）
- **LLM**: OpenAI-compatible（openai SDK）
- **测试**: pytest + pytest-asyncio + respx
- **前端**: 纯静态 HTML/CSS/JS（无构建工具，无框架）

## 关键设计约束
- **session 双存储**: SessionManager（messages+shared_context）和 SessionService（state+events）
  并存——这是已知技术债，不要随意合并（会破坏 orchestrator 的 resume 逻辑）
- **SHARED_DIR**: 用 contextvars.ContextVar 做 per-request 隔离，不要改回 os.environ
- **Agent 注册**: endpoint 去重（同 endpoint = 同 agent_id），不要改回随机 UUID
- **orchestrator 可插拔**: resolver 按 provider 切 builtin/external，external 失败 fallback 到 builtin

## 决策规则
当面临选择时，按以下优先级：
1. 最简单的方式（KISS）
2. 最显式的方式（不要隐式魔法）
3. 最可测试的方式
4. 最符合现有代码风格的方式
