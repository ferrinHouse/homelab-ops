# pinas (NAS) self-heal and fan control

Files for the OpenMediaVault NAS `pinas` (`192.168.1.253`, Raspberry Pi 4 in a Geekworm NASPi Gemini 2.5 / X882 V2.0),
plus the matching pod-restart timer for `kubeprime` (in [`../kubeprime/`](../kubeprime/)).

**None of this is deployed by CI.** `ci.yaml` does not watch `hosts/**` and its runner cannot reach pinas. Merging a
change here installs nothing; a human installs each file by hand (below). The repo copy is the source of truth.

## Why

On 2026-10-16 the JMicron JMS561 USB-to-SATA bridge that both data disks sit behind dropped off the USB bus 17 times in
~35 minutes. The ext4 filesystems took I/O errors, the `/srv` mounts went stale and `nfs-server` stopped. The disks
re-enumerated by themselves within seconds but nothing remounted them. After a manual recovery three `/export` bind
mounts also came back **read-only** (plex-config, mealie-data, npm-data) while the disks were `rw`, which crash-looped
Postgres and Mealie until they were remounted. These files automate that recovery.

| File | Runs on | Purpose |
|---|---|---|
| `nfs-watchdog.sh` + `.service` + `.timer` | pinas | Every minute: probe disks, `/export` binds, nfs-server, port 2049. After 2 failures, recover. |
| `nfs-server-requires-mounts.conf` | pinas | Drop-in so `nfs-server` cannot run unless every export is a real mount. |
| `install-geekworm-fan.sh` | pinas | Installs Geekworm's PWM fan service (pinned + SHA-256 checked). pinas had no fan control. |
| `../kubeprime/nfs-pod-bounce.*` | kubeprime | After an NFS outage, restart the pods left broken by it. |

## What the watchdog does and does not do

- Healthy: nothing, silently. Light failure (a bind mount read-only, disks fine): `mount -o remount,bind,rw`, no
  disruption. Heavy failure: stop NFS, lazy-unmount, wait for 30 s of USB quiet, `fsck.ext4 -p`, remount, probe,
  start NFS.
- Limits: 3 recoveries per hour. If `fsck -p` reports damage it can't safely fix, or the bus never settles, it leaves
  NFS **down** and logs `crit`. It never runs `fsck -y`.
- It restores service, not hardware. If it fires repeatedly, the enclosure is failing: back up the data.
- Logs: `journalctl -t nfs-watchdog` and the persistent `/var/lib/nfs-watchdog/events.log`. Set `NOTIFY_CMD` in `/etc/default/nfs-watchdog` to be told on `crit`.
- Dry run: `sudo DRY_RUN=1 /usr/local/bin/nfs-watchdog.sh` logs what it would do and changes nothing.

Tested 2026-10-16 only by dry run on pinas (healthy path, and a simulated missing export through two failures into the
recovery sequence). The real recovery path and the read-only-bind fix have **not** been exercised end to end.

## Install on pinas

```bash
# from a checkout of this repo, on pinas (or scp the files over)
cd hosts/pinas
sudo install -m 0755 nfs-watchdog.sh /usr/local/bin/nfs-watchdog.sh
sudo install -m 0644 nfs-watchdog.service nfs-watchdog.timer /etc/systemd/system/
sudo install -D -m 0644 nfs-server-requires-mounts.conf /etc/systemd/system/nfs-server.service.d/requires-mounts.conf
sudo systemctl daemon-reload
sudo DRY_RUN=1 /usr/local/bin/nfs-watchdog.sh        # expect no output
sudo systemctl enable --now nfs-watchdog.timer
```

If the disks' UUIDs or the export list change, update `DISKS` / `EXPORTS` in the script (or override them in
`/etc/default/nfs-watchdog`) and the paths in `nfs-server-requires-mounts.conf`.

**Logs:** the journal on pinas is in RAM (OMV `folder2ram` mounts `/var/log` as tmpfs), which is why the logs from before
the 2026-10-16 power cycle were gone. The watchdog therefore also keeps a persistent, size-capped event log at
`/var/lib/nfs-watchdog/events.log` (on the SD card). Read that after an incident, not just the journal.

## Install the fan service (needs one reboot)

pinas has no fan control, and the X882's PWM fan only runs while a script drives it, so the fan has not been running.

```bash
sudo ./install-geekworm-fan.sh --disable-audio    # --disable-audio frees the PWM hardware from snd_bcm2835
```
Then reboot with the supervised procedure in the main README (stop the NFS clients first), and check
`journalctl -u x-c1-fan -n 5`. The script reads the Pi's CPU temperature only, not the SSDs'. The safe-shutdown /
power-button daemon (`xPWR.sh`) is deliberately not installed; it drives the board's boot/shutdown pins and deserves
a separate, supervised trial.

## Install on kubeprime

```bash
cd hosts/kubeprime
sudo install -m 0755 nfs-pod-bounce.sh /usr/local/bin/nfs-pod-bounce.sh
sudo install -m 0644 nfs-pod-bounce.service nfs-pod-bounce.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo DRY_RUN=1 /usr/local/bin/nfs-pod-bounce.sh      # lists the pods it would restart right now
sudo systemctl enable --now nfs-pod-bounce.timer
```
It only acts after NFS has been down for at least 45 s and then stable for 90 s, restarts unhealthy NFS pods only
(databases first), and runs at most once per 15 minutes. `BOUNCE_ALL=1` in `/etc/default/nfs-pod-bounce` restarts
every NFS pod instead. Logs: `journalctl -t nfs-pod-bounce`.

## Still manual

- A Checkmk check on `192.168.1.253:2049` (Checkmk is configured in its UI; see `docs/monitoring-strategy.md`). The
  Prometheus alerts in `values-prometheus.yaml` go nowhere while Alertmanager is disabled.
- Backing up the data off the enclosure.
