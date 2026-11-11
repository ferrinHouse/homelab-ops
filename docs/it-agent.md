# IT agent: approve-as-you-go fixes on your phone

A small service in k3s (namespace `it-agent`) that acts as a first-line IT department:

1. A **Grafana alert** in the `Homelab` folder is sent to the agent (email still goes out as before).
2. The agent **investigates the live cluster**: pod state, restarts, last exit reason, warning events and
   the crashed container's last log lines. The alert only says where to look; nothing in it is executed.
3. Your local **qwen3** on the laptop writes a short "likely cause" if the laptop is on. If it is off, the
   notification goes out without it.
4. You get a **Home Assistant notification**:

   > **🔧 Pod restarting repeatedly**
   > default/plex-abc · plex: 7 restarts, waiting: CrashLoopBackOff, last exit: OOMKilled ...
   > Likely cause: ... · Proposed fix: rollout restart deployment/plex -n default
   > **[Fix once] [Fix always] [I'll handle it]**

5. **Fix once** runs it. **Fix always** runs it and saves a rule so the same alert on the same
   controller is fixed automatically next time (you still get a notice). **I'll handle it** runs nothing.
   No answer in 2 hours = the proposal expires and nothing runs.
6. Three minutes after a fix, the agent **checks the controller is healthy** and reports ✅ or ❌.

Everything runs locally and costs nothing: no cloud model, no API credits.

## What it can and cannot do

| | |
|---|---|
| Alerts with a fix | `Pod restarting repeatedly`, `Pod running but not Ready` |
| Fixes | `rollout restart` of the owning Deployment; delete the pod for StatefulSet/DaemonSet/plain ReplicaSet |
| Namespaces it may fix | `default`, `monitoring` (env `ALLOWED_NAMESPACES` **and** the `it-agent-fix` RoleBindings) |
| Advice only | Every other Homelab alert (node not Ready, disk/memory low, scrape target down, Checkmk unreachable), pods in other namespaces, bare pods, Jobs |
| Rate limit | 2 fixes per controller per hour, for approved and fix-always alike; past that it asks you |
| Never | Edits manifests, changes resource limits, touches nodes/NFS/kube-system, or runs anything the AI wrote |

OOMKilled pods get a hint to raise the memory limit in the repo; the restart is only temporary relief.

## Safety design

- **Two ports.** `8080` (alerts) is ClusterIP-only and a NetworkPolicy accepts it only from the
  `monitoring` namespace. `8081` (decisions, NodePort `30985`) needs the `X-Agent-Secret` header; with no
  secret configured it refuses everything.
- **A button press only picks a stored proposal** (`ITAGENT_ONCE_<incident id>`). The fix was decided
  by the agent's playbook from live cluster state, not by the alert, the phone or the AI.
- **RBAC is the hard limit**: read-only cluster-wide, plus `delete pods` / `patch deployments` only in
  the allowed namespaces.
- Fix-always rules are stored in ConfigMap `it-agent-state` (not on the NAS, so they survive an NFS
  outage). Review or revoke:

  ```bash
  sudo k3s kubectl get configmap it-agent-state -n it-agent -o jsonpath='{.data.always_rules\.json}'
  sudo k3s kubectl edit configmap it-agent-state -n it-agent
  ```

- Every investigation, decision and fix is logged to stdout, so it is in Loki: `{namespace="it-agent"}`.
- `EXECUTE_FIXES: "false"` in the manifest makes approved fixes announce-only.

## One-time setup

### 1. Laptop: let the cluster reach Ollama (optional, for the AI summary)

The agent calls `http://192.168.1.11:11434` (this laptop). Give the laptop a DHCP reservation on the
router so the IP does not change, then in PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
```

Quit Ollama from the tray and start it again. Then allow inbound TCP 11434 in Windows Defender Firewall,
scoped to the **Private** profile and your LAN (`192.168.1.0/24`) only. Ollama has no authentication, so
never expose that port beyond the LAN.

### 2. Home Assistant

1. Create a long-lived access token (Profile > Security). Using a dedicated non-admin HA user named
   `it-agent` limits what a leaked token could do.
2. Find your phone's notify service: Developer tools > Actions, search `notify.mobile_app_`. Put the part
   after `notify.` into `HA_NOTIFY_SERVICE` in [`k8s/configs/it-agent.yaml`](../k8s/configs/it-agent.yaml).
3. Generate a random secret (`openssl rand -hex 32`), add `it_agent_secret: "<it>"` to `/config/secrets.yaml`.
4. Copy [`hosts/homeassistant/it_agent.yaml`](../hosts/homeassistant/it_agent.yaml) to
   `/config/packages/it_agent.yaml` (see its header for the `packages:` line) and restart HA.

### 3. GitHub secrets

```bash
gh secret set IT_AGENT_HA_TOKEN
gh secret set IT_AGENT_WEBHOOK_SECRET
```

`IT_AGENT_WEBHOOK_SECRET` must equal `it_agent_secret` in HA.

### 4. Deploy

Merging to `main` runs the `Deploy IT agent` step in `ci.yaml` (code ConfigMap, secret, manifest,
restart) and also re-deploys Grafana with the new `it-agent` contact point and notification routes.

## Testing it end to end

Create a pod that crashes on purpose, wait for `Pod restarting repeatedly` (~5-10 min), tap a button:

```bash
sudo k3s kubectl create deployment it-agent-test --image=busybox -- sh -c 'echo boom; exit 1'
# ...after the test...
sudo k3s kubectl delete deployment it-agent-test
```

The restart will not fix this one, so expect "🔧 Fixing" followed by "❌ Still failing", which is
the escalation path working.

## Code

[`agent/it-agent/`](../agent/it-agent/): standard-library Python only, mounted from a ConfigMap into the
stock multi-arch `python:3.12-slim` image (no image build).
`playbooks.py` (investigate/fix/verify), `agent.py` (the loop), `main.py` (HTTP), `k8s.py`,
`notify.py`, `llm.py`, `state.py`. Tests:

```bash
python -m unittest discover -s agent/it-agent/tests -v
```

To add a playbook: map the Grafana alert title in `POD_ALERTS` or add a branch in `investigate()`,
return a `Finding` with a `Fix`, and add the matching RBAC verb if it needs a new one.

## Known limits

- Incidents live in memory: if the agent restarts, outstanding buttons answer "no such incident".
- Grafana is the only alert source. Checkmk notifications and the Prometheus-only rules in
  `values-prometheus.yaml` (`NfsWorkloadsStuck`, etc.; Alertmanager is disabled) do not reach it yet.
- An expired proposal is not re-sent until Grafana repeats the alert (every 12h) and the 6h dedup window has passed.
