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

Baseline after Phase 1 (2026-10-15): **132 services, all OK** (was 231 with 3 WARN / 18 UNKNOWN before the
cleanup). Email notifications deliver (see §3a).

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
| Disabled services: `kubeprime`, `kube2`, `yoga-node` / `NFS mount` and `Filesystem` under `/var/lib/kubelet/pods/`, `/run/k3s/containerd/`, `/run/containerd/`, `/var/lib/rancher/k3s/` | silenced | Per-pod mounts and container rootfs churn on every pod restart. **NAS/NFS health must therefore be checked directly** (omv-nas host, TCP 2049, `/export` disk), not via node mounts |
| `inventory_if_rules`: `gateway` discover by name (`item_appearance: descr`), only `eth0`, `eth1`, `br-lan`, `lan2`, `lan3`, `lan5` | stable item names | The router's ifIndex numbers shift between scans, which produced "Item not found" UNKNOWNs |
| `active_checks:httpv2` on `kube2`: `NPM proxy HTTP :80` (`:30774`), `NPM proxy HTTPS :443` (`https://cmk.ferrinhouse.org:32316/` via server `192.168.1.248`, expects 200/401/403, no cert validation), `NPM admin UI` (`:30943`) | **pages** | Phase 1, NPM down |
| `active_checks:httpv2` on `homeassistant`: `Home Assistant UI` (`http://192.168.1.254:8123/`, expects 200) | **pages** | Phase 1, HA down |
| `active_checks:tcp` on `omv-nas`: `NFS server port 2049` | **pages** | Phase 1, NAS/NFS down |
| `active_checks:icmp` on `gateway`: `Internet ping 1.1.1.1`, `Internet ping 8.8.8.8` (`address: ("explicit", ip)`) | **pages** | Phase 1, router/internet |
| `extra_host_conf:notes_url` on `kubeprime`, `kube2`, `yoga-node` | link | Phase 2: Checkmk host page -> Grafana *Node Exporter Full* (`rYdddlPWk`, `var-node=$HOSTADDRESS$:9100`) |
| `extra_service_conf:notes_url` on `kube2` services `^NPM ` | link | Phase 2: -> Grafana *Pods & Containers Deep Dive* (`var-namespace=default&var-container=npm`, metrics and Loki logs) |
| Notification rule (stock mail rule, edited): host down + service CRIT only for `^NPM `, `^Home Assistant UI`, `^Internet ping`, `^NFS server port`, `^Interface eth1$` | paging scope | Everything else is dashboard-only, per the top-5 decision |
| Disabled services: `gateway` / `Interface [0-9]+$` | silenced | Index-numbered leftovers (wifi netdevs, `br-iot`). Unused ports `lan1`/`lan4` (no link) are not discovered |

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

### 3a. Email delivery (verified 2026-10-15)

| | |
|---|---|
| Recipient | `cmkadmin` contact email `mike.ferrin.84+homelab-alerts@gmail.com` |
| Rule | Stock "HTML email to all contacts about service/host status changes", contact selection **all users with an email address** (the default "contacts of the notified object" matched nobody: no host has contacts) |
| Transport | Native SMTP in the `mail` plugin: `smtp.gmail.com:587`, STARTTLS, auth user `mike.ferrin.84@gmail.com`, app password in the Checkmk **password store** (`gmail_smtp`) |
| Test | `SEND_CUSTOM_HOST_NOTIFICATION;<host>;0;<author>;<comment>` via `unixcat ~/tmp/run/live`, then `~/var/log/notify.log`: `success 250 ... gsmtp` |

**Gotchas**
- Raw has **no UI for SMTP**; the option only exists in the REST API (`enable_sync_smtp` in the rule's plugin
  params), and the API only accepts the password as a **plain string**, not a store reference. The 2.4 mail plugin
  then crashes with `list index out of range`.
- Fix used: set the rule via the API (auth disabled or a temporary string), then rewrite the stored password in
  `~/etc/check_mk/conf.d/wato/notification_parameter.mk` inside the pod to
  `('cmk_postprocessed', 'stored_password', ('gmail_smtp', ''))`. This also removes the plaintext password
  from disk. **Do not re-save that parameter set through the API** or the plain string comes back.
- The global fallback address (`mike.ferrin.84+cmk@gmail.com`) uses local `asciimail`/postfix, which has no
  transport and silently drops mail. It only fires when no rule matches.
- Passwords entered in the UI are not "activated" until you press Activate changes.
- **Any API `PUT` of the mail notification rule re-breaks the credential**: the API creates a new parameter set
  and stringifies the reference to `"['cmk_postprocessed', 'stored_password', ['gmail_smtp', '']]"`. After
  such an edit, re-run the on-disk fix (replace that string with the tuple above) and send a test. Hit once
  on 2026-10-15 when scoping the rule to the top-5; prefer a UI edit if the rule must change.
- Paging test: `SEND_CUSTOM_SVC_NOTIFICATION;kube2;NPM proxy HTTP :80;0;<author>;<comment>`. A service outside
  the top-5 (e.g. `Uptime`) logs "does not match by the list of allowed services".

---

## 4. Phased plan

**Phase 0: clean baseline**
- ~~Rediscover services on kubeprime, kube2, yoga-node, gateway to clear WARN/UNKNOWN.~~ Done 2026-10-15.
- ~~Add a rule excluding pod mounts from `df`/`nfsmounts` discovery.~~ Done (see §1).
- ~~Verify Checkmk email notifications deliver.~~ Done (§3a); Gmail accepted the test (`250 2.0.0 OK`).

**Phase 1: top-5 alerts in Checkmk** (§3 table), with email notifications. **Done 2026-10-15** (checks and
paging scope in §1; verified by a custom notification). Not yet exercised with a real outage; do not break
production to test it. Node-down relies on host DOWN, which Checkmk reports after the check retries.

**Phase 2: cross-links** (Checkmk -> Grafana done 2026-10-15; Grafana -> Checkmk is PR #21, deploys on merge)
- Checkmk "Notes URL" rules (see §1) link the three k3s nodes and the NPM services to Grafana. The public
  Grafana URL is `https://grafana.ferrinhouse.org`. omv-nas, gateway and homeassistant have no useful Grafana
  dashboard yet, so they have no link; add them with the Phase 3 overview.
- The three repo dashboards get two links each (Checkmk problems view, all-hosts view). The imported
  community dashboards (Node Exporter Full etc.) are not in the repo and are left alone. Notes URLs are
  single-valued in Raw, so Frigate has no host-level link from `yoga-node`.

**Phase 3: unified "Homelab Overview" dashboard**
- **Built, deployed and verified 2026-10-15 (PR #23):** Checkmk data via the Grafana **Infinity** datasource
  (`yesoreyeram-infinity-datasource` 4.1.0, uid `checkmk-infinity`) calling the in-cluster Checkmk REST API with the
  read-only `grafana_ro` (guest) user. Secret flow: GitHub secret `CHECKMK_GRAFANA_SECRET` -> k8s Secret
  `checkmk-grafana` (key `bearer` = `grafana_ro <secret>`, created by `ci.yaml` before the Grafana upgrade) ->
  env `CHECKMK_GRAFANA_BEARER` -> datasource `secureJsonData`. Dashboard `homelab-overview` is **provisioned**
  from ConfigMap `homelab-dashboards` (built by CI from `k8s/monitoring/dashboards/homelab-overview.json`).
- The other three JSON files in `k8s/monitoring/dashboards/` are **not** provisioned (imported by hand; the
  repo copies are a record). Editing them in the repo does not change the running Grafana.
- Add Checkmk state to Grafana. The official Checkmk data source plugin, as far as we know, needs a commercial
  edition; **verify against Raw before ruling out**. Raw workarounds: Grafana **Infinity** data source on the REST API
  (current problems table), or a small community exporter publishing states to Prometheus.
- Use a **new read-only automation user** for Grafana, not `automation`.
- Panels: Checkmk problems + top-5 status, k8s health, Loki error rate, Frigate/HA.
- Provision the data source and dashboard in `k8s/monitoring/` (CI-deployed), with the secret via GitHub
  Actions secret, like `grafana-smtp`.

**Phase 4: watch the watchers**
- **Done 2026-10-15:** Checkmk `active_checks:httpv2` on `yoga-node` (services `Grafana health`, `Prometheus health`,
  `Loki API`, via in-cluster DNS, expects 200). These are **dashboard-only** (not in the paging regex);
  extend the notification rule's service list through the UI if you want a page. Grafana alert rule
  `checkmk-unreachable` (`values-grafana.yaml`, Infinity query, `noData`/`execErr` = Alerting) emails via the
  Gmail contact point, independent of Checkmk. Pipeline A->B->C verified against live Grafana (`C = 0`).
- **Not done:** external heartbeat (needs an account on an outside service, e.g. a dead-man's-switch URL that
  Checkmk or Grafana pings), so a whole-LAN/cluster/NAS outage still goes unreported.
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

- [x] Phase 0: rediscovery, pod-mount exclusion, email delivery (2026-10-15).
- [ ] Remove the leftover plaintext-password helper script (`~/set-smtp-auth.sh`) if it still exists.
- [ ] Clean up the global fallback notification address (local postfix drops mail).
- [x] Phase 1: NPM / HA / router / NAS-NFS / node-down checks and notifications (2026-10-15).
- [ ] Phase 2: merge PR #21 (Grafana dashboard links; deploys Grafana provisioning). Checkmk side is done.
- [ ] Consider a second paging path for a total-LAN/cluster failure (see Phase 4); Checkmk runs on yoga-node.
- [x] Phase 3: Homelab Overview deployed; all nine data panels return data through `grafana_ro` (8 top-5 checks, 6 hosts, 0 problems). Verified via `POST /api/ds/query`.
- [ ] Phase 3 follow-up: add omv-nas / gateway / homeassistant panels (disk, WAN throughput) and a screenshot-level visual review in the UI.
- [ ] Disable SSH password auth on the gateway.
- [ ] Remove stale `24.220.158.77` from `~/.ssh/known_hosts`.
- [ ] Optional: remove `CMK_SECRET` from `~/.bash_profile` and use the Bitwarden CLI instead.
