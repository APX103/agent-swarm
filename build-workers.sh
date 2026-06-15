#!/bin/bash
set -e
cd "$(dirname "$0")"
echo "Building swarm-worker image..."
# offline fallback if docker.io blocked
docker tag python:3.12.12-slim python:3.12-slim 2>/dev/null || true
docker build --pull=false -t swarm-worker:latest -f docker/Dockerfile.worker .
echo "Done: $(docker images swarm-worker --format '{{.Repository}}:{{.Tag}} {{.Size}}')"
