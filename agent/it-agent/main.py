"""IT agent entry point: two small HTTP listeners plus a once-a-minute sweeper.

- ALERT_PORT (ClusterIP, NetworkPolicy: monitoring namespace only): POST /alert from Grafana.
  Alerts can only start an investigation; nothing they contain is ever executed.
- DECISION_PORT (NodePort, for Home Assistant): POST /decision with header X-Agent-Secret.
  The body names an incident and a choice; the fix itself was decided and stored by the agent.
"""
import hmac
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent import Agent
from config import Config
from k8s import K8s
from llm import Explainer
from notify import HomeAssistant
from state import ConfigMapStore

MAX_BODY = 1024 * 1024
log = logging.getLogger("it-agent.http")


def make_handler(agent, cfg, role):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, message):
            body = json.dumps({"result": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                return self._reply(200, "ok")
            return self._reply(404, "not found")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, "too large")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._reply(400, "invalid json")

            if role == "alert" and self.path == "/alert":
                threading.Thread(target=agent.handle_grafana, args=(payload,), daemon=True).start()
                return self._reply(202, "accepted")

            if role == "decision" and self.path == "/decision":
                if not cfg.webhook_secret:
                    return self._reply(503, "decisions disabled: WEBHOOK_SECRET is not set")
                given = self.headers.get("X-Agent-Secret", "")
                if not hmac.compare_digest(given.encode(), cfg.webhook_secret.encode()):
                    log.warning("rejected decision with a bad secret from %s", self.client_address[0])
                    return self._reply(401, "bad secret")
                status, message = agent.decide(str(payload.get("action", "")))
                log.info("decision %s -> %s %s", payload.get("action"), status, message)
                return self._reply(status, message)

            return self._reply(404, "not found")

        def log_message(self, fmt, *args):
            if "/healthz" not in (args[0] if args else ""):
                log.info("%s %s", self.client_address[0], fmt % args)

    return Handler


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    k8s = K8s()
    agent = Agent(cfg, k8s,
                  HomeAssistant(cfg.ha_url, cfg.ha_token, cfg.ha_notify_service),
                  Explainer(cfg.ollama_url, cfg.ollama_model),
                  ConfigMapStore(k8s, cfg.namespace, cfg.state_configmap))
    logging.info("it-agent starting: execute_fixes=%s allowed_namespaces=%s ollama=%s rules=%d",
                 cfg.execute_fixes, cfg.allowed_namespaces, cfg.ollama_url or "off",
                 len(agent.state.always_rules))
    if not cfg.webhook_secret:
        logging.warning("WEBHOOK_SECRET is empty: Home Assistant button presses will be refused")

    servers = [ThreadingHTTPServer(("0.0.0.0", cfg.alert_port), make_handler(agent, cfg, "alert")),
               ThreadingHTTPServer(("0.0.0.0", cfg.decision_port), make_handler(agent, cfg, "decision"))]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    while True:
        time.sleep(60)
        try:
            agent.sweep()
        except Exception:  # noqa: BLE001
            logging.exception("sweep failed")


if __name__ == "__main__":
    main()
