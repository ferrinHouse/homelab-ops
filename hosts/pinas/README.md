# pinas (NAS)

Host files for the NAS `pinas` (`192.168.1.253`; Checkmk still calls it `omv-nas`), plus the matching pod-restart
timer for `kubeprime` (in [`../kubeprime/`](../kubeprime/)).

**None of this is deployed by CI.** `ci.yaml` does not watch `hosts/**` and its runner cannot reach pinas. Merging a
change here installs nothing; a human installs each file by hand (below). The repo copy is the source of truth.

## The box

| | |
|---|---|
| Hardware | Raspberry Pi 5 (8 GB) in a small Geekworm case, passive cooling only (see "Cooling") |
| OS | Debian 13 (Raspberry Pi OS Lite base), rebuilt 2026-10-29. No OpenMediaVault any more |
| Boot / OS disk | 32 GB SD card |
| Data disk | One 3.6T PCIe NVMe, ext4, `UUID=be77ee55-2852-4994-ae9f-26b76de17318`, mounted at `/mnt/nvme1` |
| Services | `nfs-server`, `smbd` (+ `wsdd2`, `avahi` for discovery), `ssh`, `check-mk-agent.socket` (6556), `nfs-watchdog.timer` |
| Network | `eth0` only. Wi-Fi and Bluetooth are disabled in `config.txt` |

## Storage layout

Every share is a directory under `/mnt/nvme1/shares/`. NFS clients see it through an fstab bind mount at
`/export/<name>` ([`fstab-export-binds`](fstab-export-binds), [`exports`](exports)); Samba points straight at the
directory ([`smb.conf`](smb.conf)). The NFS `fsid=` values are carried over from the old OMV exports so client file
handles stayed valid across the rebuild and the 2026-11-04 move; don't change them.

| Share | NFS `/export/...` | SMB | Used by |
|---|---|---|---|
| ferrinMedia | yes | yes | Plex media (Movies, TV Shows, Music, Audio Books, Photos) |
| plex-config | yes | | Plex |
| mealie-data | yes | yes | Mealie + its Postgres |
| npm-data | yes | | Nginx Proxy Manager |
| whiskey-db / whiskey-photos / whiskey-app | yes | | Whiskey Tracker |
| KubernetesLogs | yes | yes | Dynamic NFS provisioner (Grafana, Prometheus, Loki, Checkmk backups) |
| obsidian_data | yes | | Obsidian / CouchDB |
| travel-db | yes | yes | Family Travel site |
| travel-uploads | yes | | Family Travel photos (scrapbook), owned by 1001:1001 |
| GeneralBackup | | yes | Personal files, Google Takeout (1.1T) |
| moreMedia | | yes | Old media / container data |

All SMB shares require user `mferrin`. If a share is added or renamed, update `/etc/fstab`, `/etc/exports`,
`EXPORTS` in `nfs-watchdog.sh` and the `RequiresMountsFor=` list in `nfs-server-requires-mounts.conf` together, then
copy the live files back here.

**Backups:** there is no off-device copy. Everything above exists only on this one NVMe.

## Cleanup history (2026-11-04)

- Moved the shares from `/mnt/nvme1/pinas-backup/<old-disk-uuid>/` (the layout the 10-29 migration rsync left) to
  `/mnt/nvme1/shares/`. Same filesystem, so the `mv` was a rename; NFS and Samba were down about a second.
- Deleted leftovers nothing used (~32G): an old Docker root, the travel Pi's Jellyfin/Plex configs, old mealie/radarr
  copies, OMV quota files, rsync logs, a photo-compare report, and the one-off `standin-nfs.sh`. All but the Docker
  root were archived first to the workstation (`C:\Users\whwar\Backups\pinas\2026-11-04\`, SHA-256 checked), along
  with the SD-card config tarballs and a copy of `/etc` from before the change.
- Disabled `winbind`, `nmbd`, `samba-ad-dc`, `bluetooth`, `wpa_supplicant`; removed the saved Wi-Fi profile; purged
  `rpi-connect-lite`, `rpicam-apps*`, `udisks2`, `samba-ad-dc`, `winbind`. Restricted the three SMB shares that had
  no `valid users`. `config.txt`: audio, camera and display probing off; `disable-bt`, `disable-wifi`.
- Patched (24 packages) and updated the bootloader EEPROM (2026-10-30 build), installed `smartmontools` + `nvme-cli`
  (NVMe: PASSED, 0% used, 0 media errors, 7,714 h) and `unattended-upgrades` limited to Debian-Security origins
  (the plain `label=Debian` origin is commented out in `/etc/apt/apt.conf.d/50unattended-upgrades`). Rebooted
  22:48; NFS was back in under a minute, all three nodes reconnected, and Mealie/mealiedb/Postgres restarted once
  on their own and came back ready.

## Cooling

Idle CPU temperature was ~69 °C on 2026-11-04 and `vcgencmd get_throttled` reported `0xe0000` (frequency capped,
throttled and soft temperature limit hit since boot). No fan is attached. The Pi 5 drives a fan on its own 4-pin
FAN header from firmware, with no software: fit the official Active Cooler (if the case allows) or a case fan on that
header, then check `cat /sys/class/thermal/cooling_device0/cur_state` and aim for < 55 °C idle.
Stopgap since 2026-11-04: an external room fan pointed at the case, which brought idle to ~56 °C. Watch
Checkmk's `Temperature Zone 0` on `omv-nas` if that fan gets turned off.
(The old Geekworm X882 fan installer was Pi 4 only and has been removed; it's in git history.)

## Why the watchdog exists

On 2026-10-16, on the old Pi 4 build, the USB-to-SATA bridge both data disks sat behind dropped off the bus 17 times
in ~35 minutes. The filesystems took I/O errors, the mounts went stale, `nfs-server` stopped, and three `/export`
binds came back **read-only**, which crash-looped Postgres and Mealie. The NVMe removed the USB bridge, but the
watchdog still catches a missing disk, a missing or read-only bind, or a dead NFS server.

| File | Runs on | Purpose |
|---|---|---|
| `nfs-watchdog.sh` + `.service` + `.timer` | pinas | Every minute: probe the disk, `/export` binds, nfs-server, port 2049. After 2 failures, recover. |
| `nfs-server-requires-mounts.conf` | pinas | Drop-in so `nfs-server` cannot run unless every export is a real mount. |
| `fstab-export-binds`, `exports`, `smb.conf` | pinas | Records of the live `/etc` files. |
| `../kubeprime/nfs-pod-bounce.*` | kubeprime | After an NFS outage, restart the pods left broken by it. |

### What the watchdog does and does not do

- Healthy: nothing, silently. Light failure (a bind mount read-only, disk fine): `mount -o remount,bind,rw`, no
  disruption. Heavy failure: stop NFS, lazy-unmount, wait for the disk to be visible (`USB_DEV` is empty, so no USB
  settling), `fsck.ext4 -p`, remount, probe, start NFS.
- Limits: 3 recoveries per hour. If `fsck -p` reports damage it can't safely fix, it leaves NFS **down** and logs
  `crit`. It never runs `fsck -y`.
- Logs: `journalctl -t nfs-watchdog` (the journal is persistent) and `/var/lib/nfs-watchdog/events.log`, which only
  gets lines when something happens. Set `NOTIFY_CMD` in `/etc/default/nfs-watchdog` to be told on `crit`.
- Dry run: `sudo DRY_RUN=1 /usr/local/bin/nfs-watchdog.sh` logs what it would do and changes nothing.

Installed and enabled 2026-11-03 after a dry run (healthy path and a simulated missing export). The real recovery
path has **not** been exercised end to end.

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

`smb.conf` and `exports` install as-is (`sudo install -m 0644 smb.conf /etc/samba/smb.conf && testparm -s &&
sudo smbcontrol smbd reload-config`; `sudo install -m 0644 exports /etc/exports && sudo exportfs -ra`).
`fstab-export-binds` is the tail of `/etc/fstab`; merge it by hand.

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

## Checkmk agent

Installed 2026-11-03 (`check-mk-agent` 2.4.0p37, the `.deb` from `http://192.168.1.249:30500/homelab/check_mk/agents/`).
Like the other Pis it runs in legacy pull mode: `check-mk-agent.socket` on TCP 6556, no `cmk-agent-ctl` registration
(`checkmk/install-agent.sh`'s registration step does not apply on arm64). Checkmk knows the host as `omv-nas`.
After package or service changes, rerun service discovery on `omv-nas` in the Checkmk UI.

## Still manual

- The Prometheus alerts in `values-prometheus.yaml` go nowhere while Alertmanager is disabled. NFS reachability is
  covered by Checkmk's `NFS server port 2049` check on `omv-nas`, which pages (see `docs/monitoring-strategy.md`).
- An off-device backup of the NVMe.
- Cooling (above).
