# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

`homelab-ops` is the infrastructure-as-config repo for the Ferrin Homelab: Kubernetes manifests and Helm
values for a hybrid k3s cluster (Raspberry Pi ARM64 + x86_64), backed by an OpenMediaVault NFS server.
There is no application source code, build step, linter, or test suite here — the repository *is* the
deployment configuration, and "correctness" means valid YAML/Helm values that a self-hosted GitHub Actions
runner applies directly to the live cluster.

Read [README.md](README.md) first — it has the full architecture diagram, node inventory, storage matrix,
port allocations, and an "operations runbook" for common debugging questions. Do not duplicate that detail
here; this file only adds what a Claude session needs to act safely and correctly.

## Two deployment models — know which one a file belongs to

- **Model A — GitOps via CI (`k8s/monitoring/**`, `k8s/storage/**`, `k8s/configs/**`)**: Anything under
  these paths is auto-deployed to the live cluster by [`.github/workflows/ci.yaml`](.github/workflows/ci.yaml)
  on every push to `main`. There is no staging environment and no dry-run gate — merging to `main` deploys.
- **Model B — direct host manifests**: Some application workloads (npm, mealie, plex, familyTravel,
  cloudflare-ddns, obsidian) are also mirrored as loose YAML files under `/home/mferrin/` directly on the
  `kubeprime` node and applied manually with `sudo k3s kubectl apply -f`. The copies under `k8s/configs/`
  in this repo are the source of truth that CI pushes out; keep them in sync with what's actually running
  if you learn the two have diverged.

Because pushing to `main` triggers a real deployment to physical hardware, treat changes under `k8s/**` as
high-blast-radius: confirm with the user before pushing changes to `main` for files that will actually
redeploy a running service, and always check whether a change is scoped to `k8s/monitoring/**`,
`k8s/storage/**`, or `k8s/configs/**` before assuming it will (or won't) trigger CI.

## Host-level files (`hosts/**`) are NOT deployed by CI

`hosts/<node>/` holds files that live on a node's own filesystem, outside Kubernetes (currently
`hosts/yoga-node/90-flannel-heal`, a NetworkManager dispatcher script). `ci.yaml` neither watches nor
applies this directory, and its runner is on `kubeprime`, so merging a change here installs nothing. The repo
copy is the source of truth and a record; a human has to install it on the node (each file's header and the
README runbook give the command). If you change one, say so plainly rather than implying it is live. The files
are pinned to LF line endings in `.gitattributes` because a CRLF shebang breaks them on Linux.

## Alloy log pipeline (`k8s/monitoring/config.alloy`)

The `config.alloy` file defines Grafana Alloy's log-processing pipeline and has a non-obvious constraint:
the Alloy Helm chart passes `configMap.content` through Helm's `tpl` function before Alloy ever parses it.
That means **Go-template delimiters (`{{ }}`) anywhere in this file — including inside comments — get
evaluated and swallowed by Helm first**, breaking Alloy's own `stage.template` syntax. Use `stage.replace`
instead of `stage.template` for simple substitutions, and never introduce `{{ }}` in this file.

The pipeline is split into three tiers by `stage.match` selectors, matched in order:
1. **Tier 1** — logfmt monitoring-stack components (`grafana`, `loki`, `prometheus-server`, `alloy`).
   Normalizes Prometheus's uppercase `level=INFO` to lowercase to match the others, since dashboards query
   on lowercase `level`.
2. **Tier 2** — the Whiskey Tracker app (`namespace="default", container="whiskey-web"`), which logs
   structured JSON with .NET's own level vocabulary (Trace/Debug/Information/Warning/Error/Critical), a
   different alphabet from Tier 1's info/warn/error. EF Core's logged SQL (`commandText`) is kept as
   structured metadata rather than a label, since it's unbounded free text.
3. **Tier 3** — everything else (postgres, mealie, npm, plex, couchdb, cloudflare-ddns, travel-site,
   kube-system addons) passes through untouched — there's deliberately no `stage.match` block for it.

Only `level` is promoted to a Loki label; message/logger/caller/query-text fields are intentionally kept
out of labels to avoid blowing up stream cardinality.

## Grafana dashboards (`k8s/monitoring/dashboards/*.json`)

Dashboard JSON files are hand-authored/exported Grafana dashboard definitions, provisioned via
`values-grafana.yaml`. When editing panels, check `config.alloy` for which `level` values and labels are
actually available for the namespace/container you're querying — a panel filtering on a label that Alloy
never emits for that tier will silently return no data rather than erroring.

## Common commands

There's no local build/lint/test cycle. The commands that matter operate on the live cluster or its CI:

```bash
# Validate a values file / manifest is well-formed YAML before pushing
# (run from a machine with kubectl/helm, or ask the user to run on kubeprime)
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl get pods -A -o wide
sudo k3s kubectl get pvc -A

# Inspect what CI actually deployed for a Helm release
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm get values <release> -n monitoring

# Manually trigger the deploy workflow without a push
gh workflow run ci.yaml
```

Because there's no CI dry-run, the closest thing to "testing" a change before it deploys is: check the
YAML is valid, check the Helm chart's values schema (`helm show values <chart>`) for the fields you're
setting, and reason about which of the three deploy stages in `ci.yaml` (Helm upgrades → secret
create/apply → `kubectl apply` of `k8s/configs/**`) your change falls into.

## Secrets

Never commit credential values. Manifests reference secrets via `secretKeyRef` only; the actual values are
GitHub Actions repository secrets that `ci.yaml` materializes into Kubernetes `Secret` objects on deploy.
`cloudflare-secrets`, `travel-secrets`, and `ghcr-secret` are the exception — they're created out-of-band
directly on the cluster and are *not* managed by `ci.yaml`, so a change to those needs a manual
`kubectl create secret` on `kubeprime`, not just a push to `main`.

## Git workflow

- `main` is protected — do not commit directly to it. Create a `feature/<description>` or
  `fix/<description>` branch, commit there, push, and open a PR (`gh pr create`).
- Remember that merging a PR to `main` immediately deploys anything under `k8s/monitoring/**`,
  `k8s/storage/**`, or `k8s/configs/**` to the live cluster via the self-hosted runner — factor that into
  when you suggest merging, not just when you suggest pushing.
