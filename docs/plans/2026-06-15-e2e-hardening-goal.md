# E2E 硬化 (Full Containerization + Path Coverage + Teardown) — GOAL

> 自主执行，不中途确认。承接 2026-06-15 的真实 E2E（已跑通 host-orchestrator + Docker workers）。

## 背景
真实 E2E 已证明 host-orchestrator + Docker-worker 链路可用。本目标把剩余三件事做完：
1. 全容器化（orchestrator 也进 Docker）—— 暴露并修掉阻塞它的真实问题。
2. 覆盖另外两条调度路径（外部 Agent /invoke、可插拔编排器 provider=external + 回退）。
3. 收尾 teardown。

## 已知阻塞（全容器化的真问题）
- `requirements-orchestrator.txt` 缺 `redis` → orchestrator 镜像 ImportError（A1 修）。
- pool/DockerBackend 把 worker 地址写死 `localhost` → orchestrator 在容器里到不了 host 上发布的 worker 端口（A2：改可配置 worker_host，默认 localhost；容器里用 host.docker.internal）。
- `config.py` 不读 compose 传的环境变量（LLM_*/SHARED_OUTPUT_BASE/CONTAINER_*/WORKER_HOST）→ 容器化部署无法用 env 覆盖（A3：加非空 env 覆盖）。
- bind-mount 路径：pool 用 host 路径挂进 worker，task_manager 用容器路径读 —— 用「同路径挂载」绕过（shared_output 挂到容器内相同绝对路径）。

## 子任务
- **A1** `requirements-orchestrator.txt` 加 redis。
- **A2** worker_host 可配置：`ContainerPoolConfig.worker_host`；pool.checkout 与 DockerBackend 用它。TDD。
- **A3** config.py 非空 env 覆盖（LLM_*/SHARED_OUTPUT_BASE/CONTAINER_POOL_SIZE/CONTAINER_BASE_PORT/CONTAINER_IMAGE_NAME/CONTAINER_WORKER_HOST）。TDD。
- **A4** 构建 orchestrator 镜像（离线）→ 容器跑（socket + 同路径 shared_output 挂载 + WORKER_HOST=host.docker.internal）→ /api/health + /api/chat 真实任务验证。
- **B1** 外部 Agent：注册一个 mock openai 外部 Agent → /invoke 成功（真 HTTP，respx 或本地 mock server）。
- **B2** 可插拔编排器：provider=external 指向一个 mock 调度 Agent → /api/chat 走外部 → 故意失败 → 自动回退 builtin（事件 + 日志）。
- **C** teardown：停 orchestrator、删 redis + worker 容器。

## DoD
- 每 code 子任务 TDD，pytest 全绿（只增不删）。
- A4：容器化 orchestrator 真实完成一次 /api/chat 并产出 artifact。
- B1/B2：真 HTTP 演示成功 + 回退。
- C：干净收尾。

## 变更记录
- 2026-06-15：目标创建，开始 A1。
- 2026-06-15：**全部完成（A1–A4 + B1 + B2 + C），真实 Docker E2E 验证。** 容器化 orchestrator 真实跑通 `/api/chat`（产物落盘，host.docker.internal 连 worker）；外部 Agent `/invoke` 真实成功（mock openai）；`provider=external` 死端点 → 自动回退 builtin（WARNING+INFO 显式）。253 passed。修复：redis 依赖、worker_host 可配置、config env 覆盖、pool_config_dir（host 可见）。teardown 干净。
