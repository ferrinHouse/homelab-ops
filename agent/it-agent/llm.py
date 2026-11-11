"""Optional plain-English "likely cause" from a local Ollama model.

The model only writes explanatory text. It never chooses or runs a fix: fixes come from playbooks.py.
If Ollama is unreachable (e.g. the laptop is asleep), the agent sends its notification without it.
"""
import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("it-agent.llm")

SYSTEM = (
    "You are the on-call engineer for a small home Kubernetes (k3s) lab. Given an alert and the facts "
    "an automated investigation collected, reply with: one sentence 'Likely cause: ...', then up to "
    "three short numbered next steps. Use only the facts given; say so if they are not enough. "
    "Plain text, under 500 characters."
)


class Explainer:
    def __init__(self, url, model, timeout=90):
        self.url = url.rstrip("/") if url else ""
        self.model = model
        self.timeout = timeout

    def _post(self, path, body, timeout):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def available(self):
        if not self.url:
            return False
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=3):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def explain(self, finding):
        if not self.available():
            return None
        parts = [f"Alert: {finding.alertname} on {finding.subject}", "Facts:"]
        parts += [f"- {fact}" for fact in finding.facts]
        parts += [f"- {hint}" for hint in finding.hints]
        if finding.fix:
            parts.append(f"Proposed automatic fix: {finding.fix.describe()}")
        elif finding.no_fix_reason:
            parts.append(f"No automatic fix: {finding.no_fix_reason}")
        if finding.logs:
            parts.append("Recent container log lines:\n" + finding.logs[-2500:])
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "options": {"num_predict": 220, "temperature": 0.2},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "\n".join(parts)}],
        }
        try:
            reply = self._post("/api/chat", body, self.timeout)
        except (urllib.error.URLError, OSError, ValueError) as err:
            log.warning("ollama call failed: %s", err)
            return None
        text = (reply.get("message") or {}).get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        return text[:600] or None
