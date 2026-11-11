"""Incidents (in memory) and "Fix always" rules (persisted in a ConfigMap).

Rules live in a ConfigMap rather than on the NAS on purpose: NFS outages are one of the things this
agent exists to handle, so its memory must not depend on NFS.
"""
import json
import logging
import threading
from dataclasses import dataclass, field

log = logging.getLogger("it-agent.state")
RULES_KEY = "always_rules.json"


@dataclass
class Incident:
    id: str
    dedup: str
    finding: object
    explanation: str | None
    created: float
    status: str = "pending"   # pending|fixing|fixed|still_failing|failed|denied|expired|resolved|info|dry_run
    fix_key: str | None = None
    auto: bool = False


class MemoryStore:
    def __init__(self, rules=None):
        self.rules = set(rules or [])

    def load(self):
        return set(self.rules)

    def save(self, rules):
        self.rules = set(rules)


class ConfigMapStore:
    def __init__(self, k8s, namespace, name):
        self.k8s, self.namespace, self.name = k8s, namespace, name

    def load(self):
        try:
            data = self.k8s.get_configmap(self.namespace, self.name).get("data") or {}
            return set(json.loads(data.get(RULES_KEY, "[]")))
        except Exception as err:  # noqa: BLE001 - start with no rules rather than crash
            log.error("could not load fix-always rules: %s", err)
            return set()

    def save(self, rules):
        self.k8s.patch_configmap(self.namespace, self.name,
                                 {RULES_KEY: json.dumps(sorted(rules), indent=1)})


class State:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.always_rules = store.load()
        self.incidents = {}
        self.in_flight = set()
        self.fix_times = {}

    def add_rule(self, key):
        with self.lock:
            self.always_rules.add(key)
            self.store.save(self.always_rules)

    def can_fix(self, key, now, per_hour):
        with self.lock:
            recent = [t for t in self.fix_times.get(key, []) if now - t < 3600]
            self.fix_times[key] = recent
            return len(recent) < per_hour

    def record_fix(self, key, now):
        with self.lock:
            self.fix_times.setdefault(key, []).append(now)

    def blocks_new(self, dedup, now, window):
        """True if this alert already has an incident that should suppress a new one."""
        with self.lock:
            if dedup in self.in_flight:
                return True
            for inc in self.incidents.values():
                if inc.dedup != dedup or inc.status in ("resolved", "expired"):
                    continue
                if inc.status in ("pending", "fixing") or now - inc.created < window:
                    return True
            return False
