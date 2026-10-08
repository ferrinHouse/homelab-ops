#!/usr/bin/env bash
set -euo pipefail

# setup.sh - Install Docker and deploy Frigate NVR on yoga-node
# Must be run with sudo on yoga-node: sudo bash setup.sh

echo "=== [1/5] Checking Prerequisites & Installing Docker ==="
if ! command -v docker &>/dev/null || ! command -v docker-compose &>/dev/null; then
    echo "Installing docker.io and docker-compose..."
    apt-get update
    apt-get install -y docker.io docker-compose
    systemctl enable --now docker
    echo "Docker installed successfully."
else
    echo "Docker is already installed."
fi

# Ensure user mferrin is in the docker, render, and video groups
usermod -aG docker,video,render mferrin || true

echo "=== [2/5] Creating Directory Structure on NVMe ==="
mkdir -p /opt/frigate/config
mkdir -p /var/lib/frigate/storage
chown -R mferrin:mferrin /opt/frigate
chown -R mferrin:mferrin /var/lib/frigate

echo "=== [3/5] Deploying Frigate Configuration ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/config.yml" ]; then
    cp "${SCRIPT_DIR}/config.yml" /opt/frigate/config/config.yml
    echo "Copied config.yml to /opt/frigate/config/config.yml"
else
    echo "Warning: config.yml not found in ${SCRIPT_DIR}"
fi

if [ -f "${SCRIPT_DIR}/docker-compose.yml" ]; then
    cp "${SCRIPT_DIR}/docker-compose.yml" /opt/frigate/docker-compose.yml
    echo "Copied docker-compose.yml to /opt/frigate/docker-compose.yml"
else
    echo "Warning: docker-compose.yml not found in ${SCRIPT_DIR}"
fi

echo "=== [4/5] Pulling and Starting Frigate Container ==="
cd /opt/frigate
docker compose pull
docker compose up -d

echo "=== [5/5] Verification ==="
docker compose ps
echo ""
echo "Frigate is starting up!"
echo "Web UI: http://192.168.1.249:8971"
echo "API (Home Assistant): http://192.168.1.249:5000"
echo "go2rtc WebRTC / RTSP: http://192.168.1.249:1984"
