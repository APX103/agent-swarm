#!/bin/bash
set -e
echo "Starting Redis..."
docker rm -f swarm-redis 2>/dev/null || true
docker run -d --name swarm-redis -p 6379:6379 redis:7-alpine
echo "Redis ready: $(docker exec swarm-redis redis-cli ping)"
