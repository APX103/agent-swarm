# 团队好用：Agent 接入产品化 + 可靠调度 — GOAL

> 自主执行（goal 模式）。最高原则：**团队用起来稳定、好用**（用户反复强调）。

## 设计原则（来自用户）
1. 稳定/好用 > 一切。
2. 接入自己的 Agent 要极简：写一个 / 接 A2A / 当 Orchestrator / 当 Worker，几套方案。
3. 用户的 Agent 通常是**对话式 Chatbot** → 要能轻松变成 Worker 或 Orchestrator。
4. **真能调动**：LLM 辨意图 + 准确调度多 Agent 实现目标。
5. 面向团队内部使用。

## 现状盘点（避免重复造轮子）—— 接入能力大半已建成
| 方案 | 现状 | 证据 |
|---|---|---|
| Chatbot → Worker（openai 协议） | ✅ 已可用 | R1.2 自动建 adapter；B1 真实 /invoke 验证 |
| Chatbot → Orchestrator 大脑（换 LLM） | ✅ 已可用 | LLM_DEFAULT_BASE_URL 指向 chatbot；内置循环+工具不变 |
| A2A → Worker | ✅ 已可用 | gateway register（R1.2） |
| 外部 Agent → Orchestrator（整体接管） | ✅ 已可用 | provider=external + fallback（R3 / B2 验证） |

## 缺口（本 goal 补）
1. **产品化接入**：4 条方案没文档化、没便利 helper / 照抄示例 → 团队"不知道怎么接"。
2. **in-repo Worker 角色不可插拔**：加角色要改 `worker.py` 硬编码（AGENT_CARDS / SYSTEM_PROMPTS）。
3. **可靠调度·共享读**：Worker 被锁在自己 role_dir，读不到共享区/彼此产出，只靠编排器注入的计划文本。
4. **可靠调度·强制 review**：finalize 不做真实校验，review 靠 prompt 自觉（弱、非确定）。

## Waves
- **W1 接入产品化**：`docs/onboarding.md`（4 方案 recipes + 可照抄示例）+ 自注册 SDK（`swarm_sdk`）便利化/示例 + 验证 chatbot→Worker / chatbot→Orchestrator 两条最常用路径。
- **W2 in-repo Worker 可插拔**：把 worker.py 的角色卡/prompt 从硬编码抽到 config/registry；加角色 = 加配置（不动核心代码）。向后兼容。
- **W3 Worker 共享/彼此读**：给 worker 一个"读共享区/sibling"的能力（只读 `_shared/` 或编排器 dispatch 前 link），让它能基于彼此工作继续做。
- **W4 强制 review**：finalize 前对每个 Agent 产物做真实结构/契约校验，不通过自动 dispatch 返工（不靠 prompt 自觉）。
- **W5 稳定性硬化**：agent 选择可靠性（skill 匹配无候选时显式报错）、接入路径回归测试、向后兼容核对。

## DoD
- 每 code 子任务 TDD，pytest 全绿（只增不删）；不破坏公开 API。
- W1：recipes 文档可照抄；chatbot→Worker 与 chatbot→Orchestrator 各跑通一次（mock 即可）。
- W2：新增一个 worker 角色纯靠 config，不改 worker.py 核心逻辑。
- W3/W4：真实校验/共享读有测试 + 不影响现有 E2E。

## 变更记录
- 2026-06-15：目标创建（综合用户"团队好用 + 真调动"原则）。开始 W1。
