# 🩺 Monitoring Strategy: Grafana + Checkmk

Working notes and plan for making Grafana and Checkmk cover the homelab together. Written 2026-10-14 from a
long Claude Code session; kept here so future sessions (human or Claude) can pick up without re-deriving it.

> **Not deployed by CI.** `docs/**` is not watched by `ci.yaml`. Nothing in this file changes the cluster.
> No secrets live here: the Checkmk `automation` secret is in Bitwarden, `cmkadmin`'s password is the
> `CHECKMK_PASSWORD` GitHub secret.

---

## 1. Current state

### Checkmk
| | |
|---|---|
| Edition / version | Checkmk **Raw** (`2.4.0p37.cre`), site `homelab` |
| Runs on | `yoga-node`, namespace `monitoring`, NodePort `30500`, manifest [`k8s/configs/checkmk.yaml`](../k8s/configs/checkmk.yaml) |
| LAN UI | `http://192.168.1.249:30500/homelab/` (user `cmkadmin`) |
| Public UI | `https://cmk.ferrinhouse.org`, NPM proxy host, wildcard cert, Force SSL, HSTS, Block Common Exploits, Websockets, **access list `cmk_access`** (basic auth in front of Checkmk's own login) |
| API | `http://192.168.1.249:30500/homelab/check_mk/api/1.0`, user `automation` (secret in Bitwarden). LAN only: the public URL's basic auth collides with the `Authorization: Bearer` header |
| Monitored hosts | kubeprime, kube2, yoga-node, omv-nas, homeassistant (agent); gateway (SNMP-style, no agent). Source list: [`checkmk/hosts.txt`](../checkmk/hosts.txt) |
| Backup | `checkmk-backup` CronJob, 03:30 daily to the NAS (see README) |

Baseline after the 2026-10-14 cleanup: **0 CRIT**, 3 WARN (Check_MK Discovery: stale `df`/`nfsmounts` from pod
mounts on the k3s nodes), 18 UNKNOWN (14 vanished k3s mounts, 4 removed gateway interfaces 11/12/16/17).

### Grafana stack (see [`k8s/monitoring/`](../k8s/monitoring/))
- **Prometheus** scrapes k8s (api-servers, nodes, cAdvisor, pods, services) plus `frigate` (`192.168.1.249:5000`)
  and `homeassistant` (`192.168.1.254:8123`). **No Alertmanager and no Prometheus alert rules** (`{}`).
- **Loki** receives logs from Alloy (one pod per node).
- **Grafana**: provisioned datasources Loki + Prometheus; Grafana-managed alerting provisioned from
  `values-grafana.yaml`; email contact point via Gmail SMTP (`grafana-smtp` secret). NodePort `30001`.

### Rules created in Checkmk during the 2026-10-14 session (all via REST API)
| Rule (ruleset) | Effect | Reason |
|---|---|---|
| Disabled services: `omv-nas` / `Postfix status default` | silenced | Postfix not configured, stale `master.pid` from Jun 2025 |
| Disabled services: `gateway` / `Filesystem /rom`, `Interface 31`, `Interface 26` | silenced | `/rom` is read-only squashfs (always 100%); 31/26 are unused/virtual wifi netdevs |
| `memory_linux`: `yoga-node` committed memory warn **250%** / crit **300%** | thresholds raised | k3s overcommit; real usage ~57% |
| `interfaces`: `gateway` items `09$`, `22$`, `23$`, error levels `{"both": ("perc", (100.0, 100.0))}` | effectively off | wifi radio "input errors" are RF noise (see §5). Up/down still monitored |

---

## 2. Principle: one owner per question

| Question | Owner | Why |
|---|---|---|
| Host/device up; disk, memory, NFS, network health | **Checkmk** | Covers non-k8s boxes (NAS, gateway, HA) that Prometheus doesn't |
| Workload health (pods, restarts, Frigate, HA metrics) | **Grafana / Prometheus** | Native to k8s |
| Logs / what happened | **Grafana / Loki** | Only place with logs |
| Trends, dashboards | **Grafana** | Better visualisation |
| Alerting on infrastructure | **Checkmk** | Avoids duplicate disk/CPU alerts |

Node CPU/memory/disk exists in both node-exporter and the Checkmk agent. Alert on it **only in Checkmk**;
use Grafana to *visualise* it.

---

## 3. Decisions (2026-10-14)

- **Notification channel:** email only for now.
- **Dashboard:** a single unified **"Homelab Overview"** in Grafana, with deep-link buttons into Checkmk and
  Loki when something is wrong.
- **What must page (top 5):**
  1. **NPM down**: the front door for all public services.
  2. **Home Assistant down.**
  3. **Router / network health.**
  4. **NAS / NFS down**: every PVC and the Checkmk backups depend on it.
  5. **A k3s node down.**
  - Nothing else is day-to-day critical, so everything else stays at WARN/dashboard only.

### How each top alert would be detected (owner: Checkmk)
| Alert | Check |
|---|---|
| NPM down | Active HTTP/TCP checks against kube2 NPM NodePorts `30774` (80), `32316` (443), `30943` (admin). The kube2 host being up does **not** prove NPM is up |
| HA down | HTTP check `http://192.168.1.254:8123`, plus the host ping already present |
| Router / network | Gateway host reachability, WAN interface (`eth1`, public IP) up/down, and an internet reachability check (ping `1.1.1.1` / HTTP). Wifi error levels stay off |
| NAS / NFS | omv-nas host + `nfsmounts` on the nodes + disk usage on `/export` |
| Node down | Host UP/DOWN for kubeprime, kube2, yoga-node |

Blind spot: these checks run from **inside** the LAN, so they say nothing about whether the public hostnames
work from outside (Cloudflare → router → NPM).

---

## 4. Phased plan

**Phase 0: clean baseline**
- Rediscover services on kubeprime, kube2, yoga-node, gateway to clear WARN/UNKNOWN.
- Add a rule excluding pod mounts (`/var/lib/kubelet/pods/...`, `/run/k3s/...`) from `df`/`nfsmounts` discovery.
- **Verify Checkmk email notifications are configured and actually deliver** (not yet checked).

**Phase 1: top-5 alerts in Checkmk** (§3 table), with email notifications.

**Phase 2: cross-links**
- Checkmk "Notes URL" rule linking hosts/services to the matching Grafana dashboard / Loki query.
- Grafana dashboard links back to the Checkmk host page.

**Phase 3: unified "Homelab Overview" dashboard**
- Add Checkmk state to Grafana. The official Checkmk data source plugin, as far as we know, needs a commercial
  edition; **verify against Raw before ruling out**. Raw workarounds: Grafana **Infinity** data source on the REST API
  (current problems table), or a small community exporter publishing states to Prometheus.
- Use a **new read-only automation user** for Grafana, not `automation`.
- Panels: Checkmk problems + top-5 status, k8s health, Loki error rate, Frigate/HA.
- Provision the data source and dashboard in `k8s/monitoring/` (CI-deployed), with the secret via GitHub
  Actions secret, like `grafana-smtp`.

**Phase 4: watch the watchers**
- Checkmk HTTP-checks Grafana, Prometheus and Loki.
- Grafana/blackbox probes Checkmk `/homelab/`.
- Optional external heartbeat. Checkmk (yoga-node), Prometheus (kubeprime) and Grafana all depend on the same
  cluster and NAS, so a cluster-wide failure silences everything.

**Phase 5: optional**
- Checkmk Prometheus special agent for selected PromQL results (verify availability in Raw).
- Prometheus rules for pod crash loops / PVC fill, if wanted outside Grafana.

### Risks
- Single point of failure (see Phase 4).
- Raw edition limits some integrations; Phase 3 may need a workaround.
- Every new API credential goes in Bitwarden + a GitHub secret, never in files.
- Pushing `k8s/**` to `main` deploys to the live cluster (see CLAUDE.md).

---

## 5. Gateway (GL-MT6000 "Flint 2") findings and changes

| Item | Detail |
|---|---|
| Address / login | `192.168.8.1`, `ssh gateway` as `root` with key `~/.ssh/paperclip_plum` |
| SSH exposure | Dropbear bound to LAN only (`dropbear.main.Interface=lan`); WAN zone `input=DROP`; backup `/etc/config/dropbear.bak-*`. Password auth is **still on** (LAN only); disabling is an open hardening item |
| 2.4 GHz radio | Changed `HE40`/`auto` → **`HE20` / channel 6**, kept after testing. Backup `/etc/config/wireless.bak-20261014-1914` |
| Wifi "input errors" | ~5-10 errors/s on `ra0` (family 2.4G) with almost no traffic. **Not caused by any client**: eight Wyze bulbs (`2c:aa:8e:*`, hostname `LWIP`, left on the main SSID deliberately) and the printer were toggled one group at a time with no meaningful change. Most likely the MediaTek driver counting corrupt/foreign frames from ~27 neighbouring APs. Channel 6 vs 10 made no real difference |
| Laptop link | Router sees this laptop at ~-90 dBm while the laptop sees the router at -60 dBm (Realtek RTL8821CE, weak uplink). Unresolved |

Station data on this driver: `iwinfo <radio> assoclist` works; `iw station dump` returns nothing; per-station
error counters are not exposed.

---

## 6. SSH config (`~/.ssh/config`)

| Alias | Address | User | Key |
|---|---|---|---|
| `kubeprime` | 192.168.1.247 | mferrin | `id_ed25519` |
| `kube2` | 192.168.1.248 | mferrin | `id_ed25519` |
| `yoga-node` | 192.168.1.249 | mferrin | `id_ed25519` |
| `omv-nas`, `pinas` | 192.168.1.253 | mferrin | `id_ed25519` |
| `homeassistant`, `ha` | 192.168.1.254 | root | `id_ed25519` |
| `gateway` | 192.168.8.1 | root | `paperclip_plum` |

The TL-SG108E switch (`192.168.8.177`) has no SSH; web UI only.

---

## 7. Gotchas learned (for scripts and future sessions)

- **Checkmk REST API**
  - `activate-changes/invoke` needs an `If-Match` header with the ETag from
    `GET /domain-types/activation_run/collections/pending_changes`; then call
    `.../objects/activation_run/<id>/actions/wait-for-completion/invoke`.
  - Parameter rules (interfaces, filesystems) match the **item** (`09$`), not the full service description.
  - Updating a rule: `PUT /objects/rule/<id>` with `If-Match` (ETag from a `GET`).
  - `memory_linux` committed memory: `{"levels_committed": ("perc_used", (250.0, 300.0))}`. The `interfaces`
    ruleset rejects "no levels", so `{"errors": {"both": ("perc", (100.0, 100.0))}}` is used instead.
  - `service` collection queries use the Livestatus-style `query` JSON; `state` 2 = CRIT.
- **Gateway** is BusyBox/ash with no `setsid`/`nohup`: start background jobs with `start-stop-daemon -S -b`.
- **Gateway uses MediaTek wifi:** ra0/ra2 = 2.4 GHz SSIDs, rax0/rax2 = 5 GHz; `iwpriv ra0 show stainfo` writes
  to `dmesg`, not stdout.
- **Chrome extension:** screenshots right after a navigation can fail with "Detached"; `get_page_text` works.
  NPM login must be done by the user.
- **Checkmk-first, Grafana-second** alerting rule: do not add a Grafana alert for something Checkmk already owns.

---

## 8. Open items

- [ ] Phase 0: rediscovery + pod-mount exclusion rule; verify Checkmk email notifications.
- [ ] Phase 1: NPM / HA / router / NAS-NFS / node-down checks and notifications.
- [ ] Phase 3: unified overview dashboard (needs the Checkmk-to-Grafana data path decision).
- [ ] Disable SSH password auth on the gateway.
- [ ] Remove stale `24.220.158.77` from `~/.ssh/known_hosts`.
- [ ] Optional: remove `CMK_SECRET` from `~/.bash_profile` and use the Bitwarden CLI instead.
