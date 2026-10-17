#!/bin/bash
# Restart cluster pods that were left broken by an NFS outage on pinas (192.168.1.253).
#
# Why this exists: when the NAS drops and comes back, pods that mount it can be left wedged: containers in
# CreateContainerError/CrashLoopBackOff, pods stuck Terminating on a hung mount, or processes blocked on a
# stale handle. nfs-watchdog.sh on pinas heals the NAS; Kubernetes does not reliably heal the pods.
#
# What it does, once a minute (see nfs-pod-bounce.timer):
#   - Probes TCP 2049 on the NAS and remembers when it went down.
#   - When NFS has been back for SETTLE_SECONDS after an outage of at least MIN_OUTAGE_SECONDS, it looks at
#     every pod that mounts the NAS (a PVC whose PV is on the NAS, or a direct nfs volume) and deletes the
#     ones that are not healthy: not Ready, or Terminating (force-deleted, since a hung mount blocks a normal
#     delete). Healthy pods are left alone unless BOUNCE_ALL=1.
#   - Databases (FIRST_WAVE regex) go first and must be healthy before anything else is restarted.
#   - It re-checks up to PASSES times, because a pod can look fine for a moment and then fail its probes.
#   - At most one bounce per MIN_INTERVAL_SECONDS.
#
# Deleting a pod is safe here: every one is managed by a Deployment/StatefulSet, which recreates it.
# Logs: journalctl -t nfs-pod-bounce
# Dry run (list what would be restarted right now, change nothing):  sudo DRY_RUN=1 nfs-pod-bounce.sh
#
# NOTE: this is NOT deployed by CI. The repo copy is the source of truth; install by hand, see
# hosts/pinas/README.md ("Install on kubeprime").

set -u

NAS_HOST="${NAS_HOST:-192.168.1.253}"
NAS_NAMES="${NAS_NAMES:-192.168.1.253 pinas}"   # how PVs refer to the NAS (the Whiskey PVs use the hostname)
NAS_PORT=2049
SETTLE_SECONDS=90            # NFS must stay up this long before pods are touched
MIN_OUTAGE_SECONDS=45        # ignore outages shorter than this (a blip does not wedge clients)
MIN_INTERVAL_SECONDS=900     # at most one bounce per 15 minutes
PASSES=3
PASS_PAUSE=120
FIRST_WAVE='postgres|mealiedb|obsidian-sync-db|loki|prometheus-server'
FIRST_WAVE_TIMEOUT=180
BOUNCE_ALL="${BOUNCE_ALL:-0}"
DRY_RUN="${DRY_RUN:-0}"
STATE_DIR=/var/lib/nfs-pod-bounce
KUBECTL="${KUBECTL:-k3s kubectl}"
[ -r /etc/default/nfs-pod-bounce ] && . /etc/default/nfs-pod-bounce

log() { logger -t nfs-pod-bounce -- "$1"; echo "$1" >&2; }
k()   { $KUBECTL "$@"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
flock -n 9 || exit 0

nas_up() { timeout 4 bash -c "exec 3<>/dev/tcp/$NAS_HOST/$NAS_PORT" 2>/dev/null; }

# "namespace/claim" for every PVC bound to a PV on the NAS
nfs_claims() {
    local cond="" n
    for n in $NAS_NAMES; do cond="$cond (eq .spec.nfs.server \"$n\")"; done
    k get pv -o go-template='{{range .items}}{{if .spec.nfs}}{{if or'"$cond"'}}{{if .spec.claimRef}}{{.spec.claimRef.namespace}}/{{.spec.claimRef.name}}{{"\n"}}{{end}}{{end}}{{end}}{{end}}'
}

# One line per pod: ns|name|Ready(True/False)|phase|T if terminating|space-separated claims (+ DIRECT for nfs volumes)
pod_table() {
    k get pods -A -o go-template='{{range .items}}{{$ns:=.metadata.namespace}}{{$r:="False"}}{{range .status.conditions}}{{if eq .type "Ready"}}{{$r = .status}}{{end}}{{end}}{{$c:=""}}{{range .spec.volumes}}{{if .persistentVolumeClaim}}{{$c = printf "%s %s/%s" $c $ns .persistentVolumeClaim.claimName}}{{end}}{{if .nfs}}{{$c = printf "%s DIRECT" $c}}{{end}}{{end}}{{$ns}}|{{.metadata.name}}|{{$r}}|{{.status.phase}}|{{if .metadata.deletionTimestamp}}T{{end}}|{{$c}}{{"\n"}}{{end}}'
}

# Print "ns|name|T-or-empty" for NFS pods that need a restart.
broken_pods() {
    local claims uses c ns name ready phase term vols
    claims=$(nfs_claims)
    pod_table | while IFS='|' read -r ns name ready phase term vols; do
        [ "$phase" = "Succeeded" ] && continue              # finished Jobs are not broken
        uses=0
        for c in $vols; do
            if [ "$c" = "DIRECT" ] || printf '%s\n' "$claims" | grep -qx "$c"; then uses=1; fi
        done
        [ "$uses" = 1 ] || continue
        if [ "$BOUNCE_ALL" = 1 ] || [ "$ready" != "True" ] || [ -n "$term" ]; then
            echo "$ns|$name|$term"
        fi
    done
}

# Lines of a broken_pods listing whose pod name is (or is not) in the first wave
wave1() { grep -E "^[^|]*\|[^|]*(${FIRST_WAVE})[^|]*\|" || true; }
rest()  { grep -vE "^[^|]*\|[^|]*(${FIRST_WAVE})[^|]*\|" | grep . || true; }

delete_pod() {      # ns name terminating
    local flags="--wait=false"
    [ -n "$3" ] && flags="$flags --grace-period=0 --force"
    if [ "$DRY_RUN" = 1 ]; then log "DRY RUN: would delete pod $1/$2${3:+ (Terminating, forced)}"; return; fi
    log "deleting pod $1/$2${3:+ (Terminating, forced)}"
    k delete pod -n "$1" "$2" $flags >/dev/null
}

delete_list() { while IFS='|' read -r ns name term; do [ -n "$name" ] && delete_pod "$ns" "$name" "$term"; done; }

# Dry run: show what would be restarted right now, skipping all the outage timing and state.
if [ "$DRY_RUN" = 1 ]; then
    list=$(broken_pods)
    if [ -z "$list" ]; then log "DRY RUN: no broken NFS pods right now"; exit 0; fi
    printf 'wave 1 (databases):\n'; printf '%s\n' "$list" | wave1 | delete_list
    printf 'wave 2 (the rest):\n';  printf '%s\n' "$list" | rest  | delete_list
    exit 0
fi

now=$(date +%s)
if ! nas_up; then
    [ -e "$STATE_DIR/down_since" ] || { echo "$now" > "$STATE_DIR/down_since"; log "NFS on $NAS_HOST:$NAS_PORT is DOWN"; }
    rm -f "$STATE_DIR/up_since"
    exit 0
fi

[ -e "$STATE_DIR/down_since" ] || exit 0          # was never down: nothing to do
[ -e "$STATE_DIR/up_since" ] || echo "$now" > "$STATE_DIR/up_since"
down_since=$(cat "$STATE_DIR/down_since"); up_since=$(cat "$STATE_DIR/up_since")
outage=$((up_since - down_since))

if [ "$outage" -lt "$MIN_OUTAGE_SECONDS" ]; then
    log "NFS back after only ${outage}s; not touching pods"
    rm -f "$STATE_DIR/down_since" "$STATE_DIR/up_since"; exit 0
fi
[ $((now - up_since)) -ge "$SETTLE_SECONDS" ] || exit 0       # wait until it has stayed up

last=$(cat "$STATE_DIR/last_bounce" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$MIN_INTERVAL_SECONDS" ]; then
    log "NFS recovered but a bounce already ran in the last ${MIN_INTERVAL_SECONDS}s; skipping"
    rm -f "$STATE_DIR/down_since" "$STATE_DIR/up_since"; exit 0
fi

log "NFS back after a ${outage}s outage and stable for $((now - up_since))s; checking pods"
echo "$now" > "$STATE_DIR/last_bounce"

for pass in $(seq 1 "$PASSES"); do
    list=$(broken_pods)
    if [ -z "$list" ]; then
        log "pass $pass: no broken NFS pods"
        [ "$pass" -gt 1 ] && break
        sleep "$PASS_PAUSE"; BOUNCE_ALL=0; continue      # look once more: pods may still be settling
    fi

    # wave 1: databases first, then wait (up to FIRST_WAVE_TIMEOUT) until none of them is broken
    if [ -n "$(printf '%s\n' "$list" | wave1)" ]; then
        printf '%s\n' "$list" | wave1 | delete_list
        waited=0
        while [ "$waited" -lt "$FIRST_WAVE_TIMEOUT" ]; do
            sleep 10; waited=$((waited + 10))
            [ -z "$(broken_pods | wave1)" ] && break
        done
        [ "$waited" -ge "$FIRST_WAVE_TIMEOUT" ] && log "pass $pass: databases not healthy after ${FIRST_WAVE_TIMEOUT}s; carrying on"
    fi
    # wave 2: everything else
    printf '%s\n' "$list" | rest | delete_list

    BOUNCE_ALL=0        # a full bounce only applies to the first pass
    [ "$pass" -lt "$PASSES" ] && sleep "$PASS_PAUSE"
done

left=$(broken_pods | wc -l)
if [ "$left" -eq 0 ]; then log "done: all NFS pods are healthy"; else log "done, but $left NFS pod(s) are still unhealthy; check them by hand"; fi
rm -f "$STATE_DIR/down_since" "$STATE_DIR/up_since"
