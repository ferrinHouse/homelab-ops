"""Run from the repo root:  python -m unittest discover -s agent/it-agent/tests -v"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent import Agent  # noqa: E402
from config import Config  # noqa: E402
from k8s import K8sError  # noqa: E402
from main import make_handler  # noqa: E402
from state import MemoryStore  # noqa: E402

RECENT = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def crashing_pod(ns="default", name="plex-abc", owner=("ReplicaSet", "plex-5d9"), reason="Error"):
    meta = {"name": name, "namespace": ns}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {
        "metadata": meta,
        "spec": {"nodeName": "yoga-node",
                 "containers": [{"name": "plex", "resources": {"limits": {"memory": "2Gi"}}}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "plex", "ready": False, "restartCount": 7,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {"reason": reason, "exitCode": 137, "finishedAt": RECENT}},
        }]},
    }


class FakeK8s:
    def __init__(self):
        self.pods = {("default", "plex-abc"): crashing_pod()}
        self.apps = {
            ("default", "ReplicaSet", "plex-5d9"): {"metadata": {"ownerReferences": [
                {"kind": "Deployment", "name": "plex", "controller": True}]}},
            ("default", "Deployment", "plex"): {"spec": {"replicas": 1},
                                                "status": {"readyReplicas": 1, "updatedReplicas": 1}},
        }
        self.calls = []
        self.fail_writes = False

    def get_pod(self, ns, name):
        return self.pods.get((ns, name))

    def pod_logs(self, ns, name, container=None, tail=30, previous=False):
        return "panic: database is locked\n"

    def pod_events(self, ns, name):
        return [{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container"}]

    def get_apps(self, ns, kind, name):
        return self.apps[(ns, kind, name)]

    def rollout_restart(self, ns, name):
        if self.fail_writes:
            raise K8sError(403, "forbidden")
        self.calls.append(("rollout_restart", ns, name))

    def delete_pod(self, ns, name):
        self.calls.append(("delete_pod", ns, name))


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.cleared = []

    def send(self, title, message, tag, actions=None):
        self.sent.append({"title": title, "message": message, "tag": tag, "actions": actions or []})

    def clear(self, tag):
        self.cleared.append(tag)


class FakeExplainer:
    def __init__(self, text="Likely cause: SQLite lock on the NFS volume."):
        self.text = text

    def explain(self, finding):
        return self.text


def alert(name="Pod restarting repeatedly", ns="default", pod="plex-abc", status="firing"):
    return {"status": status, "labels": {"alertname": name, "namespace": ns, "pod": pod,
                                         "container": "plex", "grafana_folder": "Homelab"},
            "annotations": {"summary": f"Pod {ns}/{pod} keeps restarting"}}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.k8s = FakeK8s()
        self.phone = FakeNotifier()
        self.clock = Clock()
        self.scheduled = []
        self.cfg = Config()
        self.store = MemoryStore()
        self.agent = self._agent()

    def _agent(self):
        return Agent(self.cfg, self.k8s, self.phone, FakeExplainer(), self.store, clock=self.clock,
                     schedule=lambda delay, fn: self.scheduled.append(fn))

    def _proposal(self):
        self.agent.handle_grafana({"alerts": [alert()]})
        msg = self.phone.sent[-1]
        self.assertEqual([t for _, t in msg["actions"]], ["Fix once", "Fix always", "I'll handle it"])
        return msg, {t: a for a, t in msg["actions"]}

    def test_proposal_contains_findings_and_fix(self):
        msg, _ = self._proposal()
        self.assertIn("CrashLoopBackOff", msg["message"])
        self.assertIn("rollout restart deployment/plex -n default", msg["message"])
        self.assertIn("SQLite lock", msg["message"])
        self.assertEqual(self.k8s.calls, [], "nothing may run before approval")

    def test_fix_once_runs_then_verifies(self):
        _, buttons = self._proposal()
        self.assertEqual(self.agent.decide(buttons["Fix once"]), (200, "once"))
        self.assertEqual(self.k8s.calls, [("rollout_restart", "default", "plex")])
        self.assertEqual(self.store.rules, set())
        self.scheduled.pop()()
        self.assertTrue(self.phone.sent[-1]["title"].startswith("✅ Fixed"))
        # Same button twice does nothing.
        self.assertEqual(self.agent.decide(buttons["Fix once"])[0], 409)

    def test_fix_always_saves_narrow_rule_and_next_time_runs_automatically(self):
        _, buttons = self._proposal()
        self.agent.decide(buttons["Fix always"])
        self.assertEqual(self.store.rules,
                         {"Pod restarting repeatedly|rollout_restart|default/Deployment/plex"})

        # New pod name after the restart, same controller: auto-fixed without asking.
        self.k8s.pods[("default", "plex-xyz")] = crashing_pod(name="plex-xyz")
        self.clock.t += 600
        self.agent.handle_grafana({"alerts": [alert(pod="plex-xyz")]})
        self.assertEqual(len(self.k8s.calls), 2)
        self.assertTrue(self.phone.sent[-1]["title"].startswith("🔧 Fixing"))
        self.assertEqual(self.phone.sent[-1]["actions"], [])

    def test_always_rule_respects_rate_limit_then_asks(self):
        self.store.rules = {"Pod restarting repeatedly|rollout_restart|default/Deployment/plex"}
        self.agent = self._agent()
        for i in range(3):
            pod = f"plex-{i}"
            self.k8s.pods[("default", pod)] = crashing_pod(name=pod)
            self.clock.t += 60
            self.agent.handle_grafana({"alerts": [alert(pod=pod)]})
        self.assertEqual(len(self.k8s.calls), 2)
        self.assertIn("asking instead", self.phone.sent[-1]["message"])
        self.assertEqual(len(self.phone.sent[-1]["actions"]), 3)

    def test_deny_runs_nothing(self):
        _, buttons = self._proposal()
        self.assertEqual(self.agent.decide(buttons["I'll handle it"]), (200, "denied"))
        self.assertEqual(self.k8s.calls, [])

    def test_expired_proposal_runs_nothing(self):
        _, buttons = self._proposal()
        self.clock.t += self.cfg.proposal_ttl_sec + 1
        self.assertEqual(self.agent.decide(buttons["Fix once"])[0], 410)
        self.assertEqual(self.k8s.calls, [])

    def test_sweep_clears_unanswered_notification(self):
        msg, _ = self._proposal()
        self.clock.t += self.cfg.proposal_ttl_sec + 1
        self.agent.sweep()
        self.assertEqual(self.phone.cleared, [msg["tag"]])

    def test_repeat_alert_is_deduplicated(self):
        self._proposal()
        self.agent.handle_grafana({"alerts": [alert()]})
        self.assertEqual(len(self.phone.sent), 1)

    def test_resolved_alert_cancels_proposal(self):
        _, buttons = self._proposal()
        self.agent.handle_grafana({"alerts": [alert(status="resolved")]})
        self.assertTrue(self.phone.sent[-1]["title"].startswith("✅ Cleared"))
        self.assertEqual(self.agent.decide(buttons["Fix once"])[0], 409)

    def test_healthy_pod_is_not_touched(self):
        pod = crashing_pod()
        pod["status"]["containerStatuses"][0].update(
            ready=True, state={"running": {}}, lastState={})
        self.k8s.pods[("default", "plex-abc")] = pod
        self.agent.handle_grafana({"alerts": [alert()]})
        self.assertEqual(self.phone.sent, [])

    def test_namespace_outside_allowlist_gets_advice_only(self):
        self.k8s.pods[("kube-system", "coredns-1")] = crashing_pod(ns="kube-system", name="coredns-1")
        self.agent.handle_grafana({"alerts": [alert(ns="kube-system", pod="coredns-1")]})
        msg = self.phone.sent[-1]
        self.assertEqual(msg["actions"], [])
        self.assertIn("not on the agent's allowed list", msg["message"])

    def test_bare_pod_has_no_fix(self):
        self.k8s.pods[("default", "plex-abc")] = crashing_pod(owner=None)
        self.agent.handle_grafana({"alerts": [alert()]})
        self.assertIn("bare pod", self.phone.sent[-1]["message"])

    def test_statefulset_pod_is_deleted_not_restarted(self):
        self.k8s.pods[("default", "db-0")] = crashing_pod(name="db-0", owner=("StatefulSet", "db"))
        self.k8s.apps[("default", "StatefulSet", "db")] = {"spec": {"replicas": 1},
                                                           "status": {"readyReplicas": 1}}
        self.agent.handle_grafana({"alerts": [alert(pod="db-0")]})
        buttons = {t: a for a, t in self.phone.sent[-1]["actions"]}
        self.agent.decide(buttons["Fix once"])
        self.assertEqual(self.k8s.calls, [("delete_pod", "default", "db-0")])

    def test_oom_adds_memory_hint(self):
        self.k8s.pods[("default", "plex-abc")] = crashing_pod(reason="OOMKilled")
        msg, _ = self._proposal()
        self.assertIn("OOMKilled (memory limit 2Gi)", msg["message"])

    def test_non_pod_alert_is_advice_only(self):
        self.agent.handle_grafana({"alerts": [{"status": "firing", "labels": {
            "alertname": "Node disk space low", "node": "kube2", "mountpoint": "/"},
            "annotations": {"summary": "Node kube2 has under 10% disk free on /"}}]})
        msg = self.phone.sent[-1]
        self.assertEqual(msg["actions"], [])
        self.assertIn("kube2", msg["message"])

    def test_failed_fix_is_reported(self):
        self.k8s.fail_writes = True
        _, buttons = self._proposal()
        self.agent.decide(buttons["Fix once"])
        self.assertTrue(self.phone.sent[-1]["title"].startswith("❌ Fix failed"))

    def test_still_failing_after_fix_is_reported(self):
        _, buttons = self._proposal()
        self.agent.decide(buttons["Fix once"])
        self.k8s.apps[("default", "Deployment", "plex")]["status"]["readyReplicas"] = 0
        self.scheduled.pop()()
        self.assertTrue(self.phone.sent[-1]["title"].startswith("❌ Still failing"))

    def test_execute_fixes_off_only_announces(self):
        self.cfg.execute_fixes = False
        _, buttons = self._proposal()
        self.agent.decide(buttons["Fix once"])
        self.assertEqual(self.k8s.calls, [])
        self.assertTrue(self.phone.sent[-1]["title"].startswith("🧪 Dry run"))

    def test_garbage_actions_rejected(self):
        for bad in ["", "ITAGENT_RM_x", "rm -rf /", "ITAGENT_ONCE_nope"]:
            self.assertIn(self.agent.decide(bad)[0], (400, 404))
        self.assertEqual(self.k8s.calls, [])

    def test_offline_model_still_notifies(self):
        self.agent.explainer = FakeExplainer(text=None)
        msg, _ = self._proposal()
        self.assertIn("AI summary unavailable", msg["message"])


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(webhook_secret="s3cret")
        self.k8s = FakeK8s()
        self.phone = FakeNotifier()
        self.agent = Agent(self.cfg, self.k8s, self.phone, FakeExplainer(), MemoryStore(),
                           schedule=lambda d, fn: None)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.agent, self.cfg, "decision"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/decision"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, action, secret):
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["X-Agent-Secret"] = secret
        req = urllib.request.Request(self.url, data=json.dumps({"action": action}).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_secret_required(self):
        self.agent.handle_grafana({"alerts": [alert()]})
        once = self.phone.sent[-1]["actions"][0][0]
        self.assertEqual(self.post(once, None), 401)
        self.assertEqual(self.post(once, "wrong"), 401)
        self.assertEqual(self.k8s.calls, [])
        self.assertEqual(self.post(once, "s3cret"), 200)
        self.assertEqual(self.k8s.calls, [("rollout_restart", "default", "plex")])

    def test_empty_secret_disables_decisions(self):
        self.cfg.webhook_secret = ""
        self.assertEqual(self.post("ITAGENT_ONCE_x", ""), 503)

    def test_alert_path_not_served_on_decision_port(self):
        req = urllib.request.Request(self.url.replace("/decision", "/alert"), data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
