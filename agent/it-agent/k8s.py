"""Minimal in-cluster Kubernetes API client (standard library only, so no image build is needed)."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# apps/v1 kinds the agent reads or acts on, mapped to their API plural.
APPS_PLURALS = {
    "ReplicaSet": "replicasets",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets",
}


class K8sError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _read_token():
    # Re-read every request: projected service-account tokens rotate.
    with open(f"{SA_DIR}/token", encoding="utf-8") as fh:
        return fh.read().strip()


class K8s:
    def __init__(self, base="https://kubernetes.default.svc", token_fn=_read_token,
                 cafile=f"{SA_DIR}/ca.crt", timeout=15):
        self.base = base.rstrip("/")
        self.token_fn = token_fn
        self.ctx = ssl.create_default_context(cafile=cafile)
        self.timeout = timeout

    def _req(self, method, path, body=None, content_type="application/json", query=None, raw=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token_fn()}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            raise K8sError(err.code, err.read().decode("utf-8", "replace")[:300]) from None
        return text if raw else json.loads(text)

    # --- reads -----------------------------------------------------------------------------

    def get_pod(self, ns, name):
        try:
            return self._req("GET", f"/api/v1/namespaces/{ns}/pods/{name}")
        except K8sError as err:
            if err.status == 404:
                return None
            raise

    def pod_logs(self, ns, name, container=None, tail=30, previous=False):
        query = {"tailLines": str(tail)}
        if container:
            query["container"] = container
        if previous:
            query["previous"] = "true"
        try:
            return self._req("GET", f"/api/v1/namespaces/{ns}/pods/{name}/log", query=query, raw=True)
        except K8sError:
            return ""

    def pod_events(self, ns, name):
        query = {"fieldSelector": f"involvedObject.name={name}"}
        return self._req("GET", f"/api/v1/namespaces/{ns}/events", query=query).get("items", [])

    def get_apps(self, ns, kind, name):
        plural = APPS_PLURALS[kind]
        return self._req("GET", f"/apis/apps/v1/namespaces/{ns}/{plural}/{name}")

    # --- the only two write actions on workloads ---------------------------------------------

    def rollout_restart(self, ns, deployment):
        """Same as `kubectl rollout restart deployment/<name>`."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": now}}}}}
        return self._req("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{deployment}",
                         body=patch, content_type="application/strategic-merge-patch+json")

    def delete_pod(self, ns, name):
        return self._req("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}")

    # --- the agent's own state ConfigMap ---------------------------------------------------

    def get_configmap(self, ns, name):
        return self._req("GET", f"/api/v1/namespaces/{ns}/configmaps/{name}")

    def patch_configmap(self, ns, name, data):
        return self._req("PATCH", f"/api/v1/namespaces/{ns}/configmaps/{name}",
                         body={"data": data}, content_type="application/merge-patch+json")
