"""Home Assistant companion-app notifications (with action buttons)."""
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("it-agent.notify")


class HomeAssistant:
    def __init__(self, url, token, service, timeout=15):
        self.url = url.rstrip("/")
        self.token = token
        self.service = service
        self.timeout = timeout

    def send(self, title, message, tag, actions=None):
        """actions: list of (action_id, button_title). Same tag replaces the earlier notification."""
        log.info("notify [%s] %s | %s", tag, title, message.replace("\n", " / "))
        if not (self.token and self.service):
            log.warning("HA_TOKEN or HA_NOTIFY_SERVICE not set; notification only logged")
            return False
        data = {"tag": tag, "group": "it-agent"}
        if actions:
            data["actions"] = [{"action": a, "title": t} for a, t in actions]
        body = {"title": title[:120], "message": message[:1000], "data": data}
        req = urllib.request.Request(
            f"{self.url}/api/services/notify/{self.service}", data=json.dumps(body).encode(),
            method="POST", headers={"Authorization": f"Bearer {self.token}",
                                    "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                return True
        except (urllib.error.URLError, OSError) as err:
            log.error("Home Assistant notify failed: %s", err)
            return False

    def clear(self, tag):
        # The companion app removes the notification with this tag.
        return self.send("", "clear_notification", tag)
