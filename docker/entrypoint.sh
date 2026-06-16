#!/bin/bash
# Worker Agent 入口脚本
# 
# 容器启动后有两种模式：
# 1. WAIT_FOR_CONFIG=true: 等待 /etc/swarm/config.json 写入后启动
# 2. 否则直接启动（使用环境变量配置）

set -e

CONFIG_FILE="/etc/swarm/config.json"

if [ "$WAIT_FOR_CONFIG" = "true" ]; then
    echo "[Worker] Waiting for configuration at $CONFIG_FILE ..."
    
    # 等待配置文件出现
    while [ ! -s "$CONFIG_FILE" ]; do
        sleep 0.5
    done
    
    echo "[Worker] Configuration received, starting agent..."
fi

# 从配置文件或环境变量读取参数（用 .get() 兜底，防 KeyError 崩溃）
if [ -s "$CONFIG_FILE" ]; then
    export AGENT_ROLE=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('agent_role','general'))")
    export LLM_MODEL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('model','glm-coding-plan'))")
    export LLM_BASE_URL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('base_url','https://open.bigmodel.cn/api/coding/paas/v4'))")
    export LLM_API_KEY=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('api_key',''))")
    export AGENT_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('port',9001))")
    export TASK_ID=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('task_id',''))")
    export SHARED_DIR=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('shared_dir','/workspace/artifacts'))")
    export AGENT_SYSTEM_PROMPT=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('system_prompt',''))" 2>/dev/null || echo "")
fi

echo "[Worker] Starting Agent: role=$AGENT_ROLE, model=$LLM_MODEL, port=$AGENT_PORT, shared_dir=$SHARED_DIR"

# 启动 Worker Agent Server
exec python3 -u /app/agents/worker.py \
    --role "$AGENT_ROLE" \
    --model "$LLM_MODEL" \
    --base-url "$LLM_BASE_URL" \
    --api-key "$LLM_API_KEY" \
    --port "$AGENT_PORT" \
    --task-id "$TASK_ID" \
    --shared-dir "$SHARED_DIR"
