"""What the agent knows how to investigate and fix.

An alert only tells the agent *where to look*. Every decision is made from the live cluster state,
so a forged or stale alert can never cause a fix on something that is actually healthy.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Grafana alert rule titles (k8s/monitoring/values-grafana.yaml) that have a pod playbook.
POD_ALERTS = {
    "Pod restarting repeatedly": "crashloop",
    "Pod running but not Ready": "notready",
}
BAD_WAITING = {"CrashLoopBackOff", "Error", "RunContainerError"}
RECENT_CRASH_SEC = 20 * 60


@dataclass(frozen=True)
class Fix:
    action: str            # "rollout_restart" | "delete_pod"
    namespace: str
    pod: str
    controller_kind: str   # Deployment | StatefulSet | DaemonSet | ReplicaSet
    controller_name: str

    def describe(self):
        if self.action == "rollout_restart":
            return f"rollout restart deployment/{self.controller_name} -n {self.namespace}"
        return (f"delete pod {self.pod} -n {self.namespace} "
                f"({self.controller_kind.lower()}/{self.controller_name} recreates it)")

    def target(self):
        # Stable across restarts (pod names change, the controller does not).
        return f"{self.namespace}/{self.controller_kind}/{self.controller_name}"


@dataclass
class Finding:
    alertname: str
    subject: str
    still_failing: bool
    facts: list = field(default_factory=list)
    fix: Fix | None = None
    no_fix_reason: str = ""
    hints: list = field(default_factory=list)
    logs: str = ""


def rule_key(alertname, fix):
    """Identity of a "Fix always" rule: this alert + this action + this controller, nothing broader."""
    return f"{alertname}|{fix.action}|{fix.target()}"


def investigate(alert, k8s, cfg):
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    name = labels.get("alertname", "Unknown alert")
    kind = POD_ALERTS.get(name)
    if kind and labels.get("namespace") and labels.get("pod"):
        return _investigate_pod(name, kind, labels["namespace"], labels["pod"],
                                labels.get("container"), k8s, cfg)
    facts = []
    if annotations.get("summary"):
        facts.append(annotations["summary"])
    facts += [f"{k}={v}" for k, v in sorted(labels.items())
              if not k.startswith("__") and k not in ("alertname", "grafana_folder")]
    if annotations.get("description"):
        facts.append(annotations["description"])
    return Finding(name, annotations.get("summary") or name, True, facts,
                   no_fix_reason="The agent has no automatic fix for this alert type yet.")


def _parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _investigate_pod(alertname, kind, ns, pod_name, container, k8s, cfg):
    subject = f"{ns}/{pod_name}"
    pod = k8s.get_pod(ns, pod_name)
    if pod is None:
        return Finding(alertname, subject, False, ["The pod no longer exists (already replaced)."])

    spec, status = pod.get("spec", {}), pod.get("status", {})
    facts = [f"Node {spec.get('nodeName', '?')}, phase {status.get('phase', '?')}"]
    hints = []
    failing = False
    crashed_container = None
    now = datetime.now(timezone.utc)

    for cs in status.get("containerStatuses") or []:
        if container and cs.get("name") != container:
            continue
        waiting = (cs.get("state") or {}).get("waiting", {}).get("reason")
        last = (cs.get("lastState") or {}).get("terminated") or {}
        line = f"{cs.get('name')}: {cs.get('restartCount', 0)} restarts, ready={cs.get('ready')}"
        if waiting:
            line += f", waiting: {waiting}"
        if last:
            line += f", last exit: {last.get('reason')} (code {last.get('exitCode')})"
            crashed_container = cs.get("name")
        facts.append(line)

        if kind == "crashloop":
            finished = _parse_time(last.get("finishedAt"))
            recent = finished is not None and (now - finished).total_seconds() < RECENT_CRASH_SEC
            failing |= waiting in BAD_WAITING or not cs.get("ready") or recent
        else:
            failing |= not cs.get("ready")

        if last.get("reason") == "OOMKilled":
            limit = next((c.get("resources", {}).get("limits", {}).get("memory")
                          for c in spec.get("containers", []) if c.get("name") == cs.get("name")), None)
            hints.append(f"It was OOMKilled (memory limit {limit or 'none'}). A restart is only "
                         "temporary relief; raise the limit in the repo manifest.")

    if not failing:
        return Finding(alertname, subject, False, facts + ["The pod looks healthy now."])

    try:
        events = [e for e in k8s.pod_events(ns, pod_name) if e.get("type") == "Warning"]
    except Exception:  # noqa: BLE001 - events are nice-to-have
        events = []
    for event in events[-3:]:
        facts.append(f"Event {event.get('reason')}: {(event.get('message') or '')[:160]}")

    logs = k8s.pod_logs(ns, pod_name, container or crashed_container, tail=30,
                        previous=crashed_container is not None)

    finding = Finding(alertname, subject, True, facts, hints=hints, logs=logs[-3000:])
    if ns not in cfg.allowed_namespaces:
        finding.no_fix_reason = f"Namespace {ns} is not on the agent's allowed list."
        return finding

    owner = next((o for o in pod["metadata"].get("ownerReferences") or [] if o.get("controller")), None)
    if owner is None:
        finding.no_fix_reason = "It is a bare pod with no controller, so deleting it would not bring it back."
        return finding

    owner_kind, owner_name = owner.get("kind"), owner.get("name")
    if owner_kind == "ReplicaSet":
        rs = k8s.get_apps(ns, "ReplicaSet", owner_name)
        dep = next((o for o in rs["metadata"].get("ownerReferences") or []
                    if o.get("controller") and o.get("kind") == "Deployment"), None)
        if dep:
            finding.fix = Fix("rollout_restart", ns, pod_name, "Deployment", dep["name"])
        else:
            finding.fix = Fix("delete_pod", ns, pod_name, "ReplicaSet", owner_name)
    elif owner_kind in ("StatefulSet", "DaemonSet"):
        finding.fix = Fix("delete_pod", ns, pod_name, owner_kind, owner_name)
    else:
        finding.no_fix_reason = f"It is owned by a {owner_kind}; the agent has no fix for that."
    return finding


def apply_fix(fix, k8s):
    if fix.action == "rollout_restart":
        k8s.rollout_restart(fix.namespace, fix.controller_name)
    elif fix.action == "delete_pod":
        k8s.delete_pod(fix.namespace, fix.pod)
    else:
        raise ValueError(f"unknown fix action {fix.action}")


def verify(fix, k8s):
    """Return (healthy, detail) for the controller the fix acted on."""
    obj = k8s.get_apps(fix.namespace, fix.controller_kind, fix.controller_name)
    spec, status = obj.get("spec", {}), obj.get("status", {})
    if fix.controller_kind == "DaemonSet":
        want, ready = status.get("desiredNumberScheduled", 0), status.get("numberReady", 0)
        return ready >= want, f"{ready}/{want} pods ready"
    want = spec.get("replicas", 1)
    ready = status.get("readyReplicas", 0)
    ok = ready >= want
    if fix.controller_kind == "Deployment":
        updated = status.get("updatedReplicas", 0)
        ok = ok and updated >= want
        return ok, f"{ready}/{want} ready, {updated}/{want} updated"
    return ok, f"{ready}/{want} ready"
