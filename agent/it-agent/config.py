"""Settings for the IT agent, read from environment variables (see k8s/configs/it-agent.yaml)."""
import os
from dataclasses import dataclass, field


def _list(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # Home Assistant: where notifications go. The notify service is e.g. "mobile_app_pixel_8".
    ha_url: str = "http://192.168.1.254:8123"
    ha_token: str = ""
    ha_notify_service: str = ""
    # Shared with Home Assistant's rest_command. Empty = the decision endpoint refuses everything.
    webhook_secret: str = ""
    # Optional local model for the "likely cause" text. Empty = skip it.
    ollama_url: str = ""
    ollama_model: str = "qwen3"
    # Namespaces the agent may propose fixes in (RBAC enforces the same list).
    allowed_namespaces: list = field(default_factory=lambda: ["default", "monitoring"])
    proposal_ttl_sec: int = 7200       # an unanswered proposal expires after this
    dedup_window_sec: int = 21600      # don't re-propose the same alert within this window
    max_fixes_per_hour: int = 2        # per target, for both approved and "always" fixes
    verify_delay_sec: int = 180        # wait this long after a fix before checking it worked
    alert_port: int = 8080             # Grafana webhook (ClusterIP only)
    decision_port: int = 8081          # Home Assistant button callbacks (NodePort)
    namespace: str = "it-agent"
    state_configmap: str = "it-agent-state"
    # false = approved fixes are only announced, never run (a safe mode for testing the loop)
    execute_fixes: bool = True

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        cfg = cls()
        strings = {
            "HA_URL": "ha_url", "HA_TOKEN": "ha_token", "HA_NOTIFY_SERVICE": "ha_notify_service",
            "WEBHOOK_SECRET": "webhook_secret", "OLLAMA_URL": "ollama_url",
            "OLLAMA_MODEL": "ollama_model", "POD_NAMESPACE": "namespace",
            "STATE_CONFIGMAP": "state_configmap",
        }
        ints = {
            "PROPOSAL_TTL_SEC": "proposal_ttl_sec", "DEDUP_WINDOW_SEC": "dedup_window_sec",
            "MAX_FIXES_PER_HOUR": "max_fixes_per_hour", "VERIFY_DELAY_SEC": "verify_delay_sec",
            "ALERT_PORT": "alert_port", "DECISION_PORT": "decision_port",
        }
        for var, attr in strings.items():
            if env.get(var):
                setattr(cfg, attr, env[var].strip())
        for var, attr in ints.items():
            if env.get(var):
                setattr(cfg, attr, int(env[var]))
        if env.get("ALLOWED_NAMESPACES"):
            cfg.allowed_namespaces = _list(env["ALLOWED_NAMESPACES"])
        if env.get("EXECUTE_FIXES"):
            cfg.execute_fixes = _bool(env["EXECUTE_FIXES"])
        cfg.ha_url = cfg.ha_url.rstrip("/")
        cfg.ollama_url = cfg.ollama_url.rstrip("/")
        return cfg
