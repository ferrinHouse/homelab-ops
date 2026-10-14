#!/usr/bin/env bash
# Install and register the Checkmk agent on a Debian/Raspberry Pi OS host (kubeprime, kube2, yoga-node, omv-nas).
# Run ON the target host:
#   CMK_SERVER=192.168.1.249 HOST_NAME=kubeprime ./install-agent.sh
# Needs an automation user secret for registration; prompted if CMK_SECRET is unset.
set -euo pipefail
: "${CMK_SERVER:?yoga-node IP (192.168.1.249)}"
: "${HOST_NAME:?name as listed in hosts.txt}"
SITE=${SITE:-homelab}
USER_=${CMK_USER:-automation}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
base="http://$CMK_SERVER:30500/$SITE/check_mk/agents"
deb=$(curl -fsS "$base/" | grep -oE 'check-mk-agent_[^"]+_all\.deb' | head -1)
curl -fsS -o "$tmp/$deb" "$base/$deb"
sudo dpkg -i "$tmp/$deb"

if [ -z "${CMK_SECRET:-}" ]; then read -rsp "automation secret: " CMK_SECRET; echo; fi
sudo cmk-agent-ctl register --hostname "$HOST_NAME" --server "$CMK_SERVER:30800" \
  --site "$SITE" --user "$USER_" --password "$CMK_SECRET" --trust-cert
sudo cmk-agent-ctl status
