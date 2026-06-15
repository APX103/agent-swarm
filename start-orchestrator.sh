#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Starting orchestrator on :9000..."
# dev mode (hot-reload workers): export WORKER_DEV_MODE=true
# api key: export SWARM_API_KEY=your-key
.venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 9000
