#!/bin/bash
# NFS self-heal watchdog for pinas (OpenMediaVault NAS, 192.168.1.253).
#
# Why this exists: both data disks sit behind one JMicron JMS561 USB-to-SATA bridge (Geekworm
# NASPi Gemini 2.5 / X882). When it drops off the USB bus (2026-10-16, 17 times in ~35 minutes) the
# ext4 filesystems take I/O errors, the /srv mounts go stale, and nfs-server stops. The disks re-enumerate
# on their own within seconds, but nothing remounted them or restarted NFS.
#
# What it does, once a minute (see nfs-watchdog.timer):
#   1. Probe: each data disk mounted + readable + writable, every /export bind mount present, nfs-server
#      active, port 2049 listening. All probes run under `timeout -s KILL` because I/O on a dead mount
#      can hang.
#   2. Two consecutive failed probes (~2 min) are needed before acting, to ride out a blip.
#   3. Recover: stop nfs-server, lazy-unmount the binds and disks, wait until the USB bus has been
#      quiet for STABLE_SECONDS and both disks are visible, `fsck.ext4 -p` (safe fixes only), remount,
#      probe again, start nfs-server.
#   4. Safety rails: at most MAX_ATTEMPTS recoveries per hour; if fsck reports damage it cannot fix safely
#      (exit >= 4) it stops and leaves NFS DOWN rather than serving or "repairing" a damaged disk.
#      After giving up it does nothing until an operator looks.
#
# Everything is logged to the journal as tag "nfs-watchdog" (journalctl -t nfs-watchdog) and to the persistent
# /var/lib/nfs-watchdog/events.log, because the journal on pinas is in RAM and is lost at every reboot.
# Set NOTIFY_CMD in /etc/default/nfs-watchdog to be told (it receives the message as $1).
#
# Dry run (log what would happen, change nothing):  sudo DRY_RUN=1 /usr/local/bin/nfs-watchdog.sh
#
# NOTE: this is NOT deployed by CI. The repo copy is the source of truth; install by hand, see README.md.

set -u

# --- configuration (override in /etc/default/nfs-watchdog) -------------------------------------
# Disk UUIDs of the two data disks (see /etc/fstab). DISK_A holds ferrinMedia, whiskey-*, KubernetesLogs,
# obsidian_data, travel-db; DISK_B holds plex-config, mealie-data, npm-data.
DISKS="44477d40-c1ee-46e0-9104-14886001f78b 7462ec5d-a120-45df-a96c-f714d67b6943"
EXPORTS="ferrinMedia plex-config mealie-data npm-data whiskey-db whiskey-photos whiskey-app KubernetesLogs obsidian_data travel-db"
USB_DEV="usb 2-2"            # kernel log prefix of the enclosure, matches "usb 2-2: USB disconnect"
PROBE_TIMEOUT=10             # seconds before a filesystem probe is killed and counted as failed
FAILS_BEFORE_ACT=2           # consecutive failed probes before recovering
STABLE_SECONDS=30            # USB bus must log no disconnects for this long before touching the disks
STABLE_WAIT_MAX=300          # give up waiting for the bus to settle after this long
MAX_ATTEMPTS=3               # recoveries allowed per ATTEMPT_WINDOW
ATTEMPT_WINDOW=3600
NOTIFY_CMD=""
STATE_DIR=/var/lib/nfs-watchdog
DRY_RUN="${DRY_RUN:-0}"
[ -r /etc/default/nfs-watchdog ] && . /etc/default/nfs-watchdog
# ---------------------------------------------------------------------------------------------

TAG=nfs-watchdog
# The journal on pinas lives in RAM (folder2ram), so also keep a persistent, size-capped event log on the SD card.
log()  {
    logger -t "$TAG" -p "${2:-daemon.info}" -- "$1"; echo "$1" >&2
    [ "$DRY_RUN" = 1 ] && return 0
    [ "$(stat -c %s "$STATE_DIR/events.log" 2>/dev/null || echo 0)" -gt 204800 ] && mv -f "$STATE_DIR/events.log" "$STATE_DIR/events.log.1"
    echo "$(date '+%F %T') $1" >> "$STATE_DIR/events.log"
}
warn() { log "$1" daemon.warning; }
crit() { log "$1" daemon.crit; [ -n "$NOTIFY_CMD" ] && [ "$DRY_RUN" != 1 ] && $NOTIFY_CMD "nfs-watchdog on $(hostname): $1" >/dev/null 2>&1; }
run()  { if [ "$DRY_RUN" = 1 ]; then log "DRY RUN: $*"; return 0; fi; "$@"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
flock -n 9 || exit 0          # another run (possibly a long recovery) is in progress

# Probe a path: the directory is readable and a small file can be created and removed.
probe_dir() {
    local d="$1"
    timeout -s KILL "$PROBE_TIMEOUT" ls "$d" >/dev/null 2>&1 || return 1
    timeout -s KILL "$PROBE_TIMEOUT" sh -c ': > "$1/.nfs-watchdog" && rm -f "$1/.nfs-watchdog"' _ "$d" 2>/dev/null || return 1
}

# Echo the first problem found and return 1; return 0 when everything is healthy.
check_health() {
    local u e
    for u in $DISKS; do
        mountpoint -q "/srv/dev-disk-by-uuid-$u" || { echo "disk $u is not mounted"; return 1; }
        probe_dir "/srv/dev-disk-by-uuid-$u"     || { echo "disk $u failed the read/write probe"; return 1; }
    done
    # The bind mounts need their own read/write probe: after the 2026-10-16 recovery the disks were rw
    # but three binds (plex-config, mealie-data, npm-data) had come up read-only, so Postgres and
    # Mealie crash-looped with "Read-only file system" while every disk-level check passed.
    for e in $EXPORTS; do
        mountpoint -q "/export/$e" || { echo "/export/$e is not mounted"; return 1; }
        probe_dir "/export/$e"     || { echo "/export/$e failed the read/write probe (read-only bind?)"; return 1; }
    done
    systemctl is-active --quiet nfs-server || { echo "nfs-server is not active"; return 1; }
    ss -Hltn 'sport = :2049' | grep -q . || { echo "nothing is listening on port 2049"; return 1; }
    return 0
}

usb_disconnects() { dmesg 2>/dev/null | grep -c "$USB_DEV: USB disconnect"; }

wait_for_stable_bus() {
    local waited=0 before after u missing
    while [ "$waited" -lt "$STABLE_WAIT_MAX" ]; do
        before=$(usb_disconnects)
        sleep "$STABLE_SECONDS"; waited=$((waited + STABLE_SECONDS))
        after=$(usb_disconnects)
        missing=""
        for u in $DISKS; do [ -e "/dev/disk/by-uuid/$u" ] || missing="$missing $u"; done
        if [ "$before" = "$after" ] && [ -z "$missing" ]; then return 0; fi
        warn "USB bus not settled yet (disconnects $before -> $after, missing disks:${missing:- none})"
    done
    return 1
}

# Attempt accounting: one epoch timestamp per line; drop entries older than the window.
attempts_in_window() {
    local now cutoff
    now=$(date +%s); cutoff=$((now - ATTEMPT_WINDOW))
    touch "$STATE_DIR/attempts"
    awk -v c="$cutoff" '$1 >= c' "$STATE_DIR/attempts" > "$STATE_DIR/attempts.tmp" && mv "$STATE_DIR/attempts.tmp" "$STATE_DIR/attempts"
    wc -l < "$STATE_DIR/attempts"
}

# Cheap fix for the common partial failure: the disks are fine but some /export binds are read-only.
# Remounting a bind rw does not disturb NFS clients. Returns 0 only if that made everything healthy.
heal_ro_binds() {
    local u e fixed=0
    for u in $DISKS; do probe_dir "/srv/dev-disk-by-uuid-$u" || return 1; done   # disk problem: needs full recovery
    for e in $EXPORTS; do
        mountpoint -q "/export/$e" || return 1
        if ! probe_dir "/export/$e"; then
            warn "/export/$e is not writable; remounting the bind read-write"
            run mount -o remount,bind,rw "/export/$e" && fixed=1
        fi
    done
    [ "$fixed" = 1 ] || return 1
    [ "$DRY_RUN" = 1 ] && return 1
    check_health >/dev/null
}

recover() {
    local u e rc
    warn "recovery started"
    run systemctl stop nfs-server
    for e in $EXPORTS; do run umount -l "/export/$e" 2>/dev/null; done
    for u in $DISKS; do run umount -l "/srv/dev-disk-by-uuid-$u" 2>/dev/null; done

    if [ "$DRY_RUN" != 1 ]; then
        wait_for_stable_bus || { crit "USB bus never settled within ${STABLE_WAIT_MAX}s; NFS left DOWN, hardware needs attention"; return 1; }
    fi

    for u in $DISKS; do
        # -p: repair only what is safe to repair automatically. Exit 0/1 are fine (1 = fixed);
        # 4 or more means damage that needs a human.
        if [ "$DRY_RUN" = 1 ]; then log "DRY RUN: fsck.ext4 -p /dev/disk/by-uuid/$u"; continue; fi
        fsck.ext4 -p "/dev/disk/by-uuid/$u" >"$STATE_DIR/fsck-$u.log" 2>&1; rc=$?
        if [ "$rc" -ge 4 ]; then
            crit "fsck on $u exited $rc (see $STATE_DIR/fsck-$u.log); NFS left DOWN, needs a manual fsck"
            return 1
        fi
        log "fsck on $u exited $rc"
    done

    for u in $DISKS; do run mount "/srv/dev-disk-by-uuid-$u" || { crit "could not mount disk $u"; return 1; }; done
    for e in $EXPORTS; do run mount "/export/$e" || { crit "could not mount /export/$e"; return 1; }; done

    if [ "$DRY_RUN" != 1 ]; then
        for u in $DISKS; do
            probe_dir "/srv/dev-disk-by-uuid-$u" || { crit "disk $u still fails probes after remount; NFS left DOWN"; return 1; }
        done
    fi
    run systemctl start nfs-server
    if [ "$DRY_RUN" != 1 ]; then
        sleep 3
        check_health >/dev/null || { crit "still unhealthy after recovery: $(check_health)"; return 1; }
    fi
    log "recovery complete: NFS is serving again (pods may need a restart, see nfs-pod-bounce on kubeprime)"
    return 0
}

# --- main --------------------------------------------------------------------------------------
if problem=$(check_health); then
    [ -s "$STATE_DIR/fails" ] && log "healthy again after $(cat "$STATE_DIR/fails") failed probe(s)"
    : > "$STATE_DIR/fails"
    exit 0
fi

fails=$(( $(cat "$STATE_DIR/fails" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE_DIR/fails"
warn "unhealthy ($fails/$FAILS_BEFORE_ACT): $problem"
[ "$fails" -ge "$FAILS_BEFORE_ACT" ] || exit 0

# Try the light fix first; it does not count as a recovery attempt.
if heal_ro_binds; then
    log "fixed by remounting read-only binds read-write; NFS clients are unaffected"
    : > "$STATE_DIR/fails"
    exit 0
fi

n=$(attempts_in_window)
if [ "$n" -ge "$MAX_ATTEMPTS" ]; then
    # Report at most every 10 minutes so an unattended failure does not flood the journal.
    last=$(stat -c %Y "$STATE_DIR/gave-up" 2>/dev/null || echo 0)
    if [ $(( $(date +%s) - last )) -ge 600 ]; then
        crit "giving up: $n recoveries in the last hour and still unhealthy ($problem); manual intervention needed"
        touch "$STATE_DIR/gave-up"
    fi
    exit 1
fi

[ "$DRY_RUN" = 1 ] || date +%s >> "$STATE_DIR/attempts"
if recover; then
    : > "$STATE_DIR/fails"
    rm -f "$STATE_DIR/gave-up"
    exit 0
fi
exit 1
