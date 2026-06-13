#!/bin/bash
# Agent Swarm 启动脚本
# 解决 Docker 权限问题（需要 docker 组权限）

cd "$(dirname "$0")"

# 激活 venv
source .venv/bin/activate

# 加载 .env（如果存在）
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

# 设置默认值
export GLM_API_KEY="${GLM_API_KEY:-}"
export SWARM_CONFIG="${SWARM_CONFIG:-config/default.yaml}"

echo "🐝 Starting Agent Swarm..."
echo "   Config: $SWARM_CONFIG"
echo "   API Key: ${GLM_API_KEY:+configured}"
echo ""

exec python -m uvicorn src.main:app --host 0.0.0.0 --port 9000 "$@"
