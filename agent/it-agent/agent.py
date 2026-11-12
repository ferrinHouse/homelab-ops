"""The approve-as-you-go loop: alert -> investigate -> propose (or auto-fix) -> verify -> report."""
import json
import logging
import secrets
import threading
import time

import playbooks
from k8s import K8sError
from state import Incident, State

log = logging.getLogger("it-agent")

DECISIONS = {"ONCE": "once", "ALWAYS": "always", "DENY": "deny"}


def _default_schedule(delay, fn):
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


def dedup_key(alert):
    labels = {k: v for k, v in (alert.get("labels") or {}).items() if not k.startswith("__")}
    return json.dumps(labels, sort_keys=True)


class Agent:
    def __init__(self, cfg, k8s, notifier, explainer, store, clock=time.time, schedule=_default_schedule):
        self.cfg = cfg
        self.k8s = k8s
        self.notifier = notifier
        self.explainer = explainer
        self.state = State(store)
        self.clock = clock
        self.schedule = schedule

    # --- alerts from Grafana --------------------------------------------------------------

    def handle_grafana(self, payload):
        for alert in payload.get("alerts") or []:
            try:
                if alert.get("status") == "resolved":
                    self._resolved(alert)
                else:
                    self._firing(alert)
            except Exception:  # noqa: BLE001 - one bad alert must not stop the rest
                log.exception("failed to handle alert %s", (alert.get("labels") or {}).get("alertname"))

    def _firing(self, alert):
        dedup = dedup_key(alert)
        now = self.clock()
        with self.state.lock:
            if self.state.blocks_new(dedup, now, self.cfg.dedup_window_sec):
                return
            self.state.in_flight.add(dedup)
        try:
            self._open_incident(alert, dedup)
        finally:
            with self.state.lock:
                self.state.in_flight.discard(dedup)

    def _open_incident(self, alert, dedup):
        try:
            finding = playbooks.investigate(alert, self.k8s, self.cfg)
        except Exception as err:  # noqa: BLE001 - still tell the human something is wrong
            log.exception("investigation failed")
            name = (alert.get("labels") or {}).get("alertname", "Unknown alert")
            finding = playbooks.Finding(name, name, True,
                                        [(alert.get("annotations") or {}).get("summary", name),
                                         f"The agent's investigation failed: {err}"])
        if not finding.still_failing:
            log.info("%s on %s: not failing any more, no action (%s)",
                     finding.alertname, finding.subject, "; ".join(finding.facts))
            return

        inc = Incident(secrets.token_hex(4), dedup, finding, None, self.clock(),
                       status="pending" if finding.fix else "info")
        if finding.fix:
            inc.target = finding.fix.target()
            inc.fix_key = playbooks.rule_key(finding.alertname, finding.fix)
        # Check and register in one step, so two alerts about the same workload (e.g. "restarting"
        # and "not Ready" for one crashing pod, or the replacement pod after a restart) can never
        # both produce a proposal.
        with self.state.lock:
            if inc.target:
                existing = self.state.incident_for_target(inc.target, self.clock(),
                                                          self.cfg.dedup_window_sec)
                if existing:
                    log.info("%s on %s: %s is already covered by incident %s (%s); not proposing again",
                             finding.alertname, finding.subject, inc.target, existing.id, existing.status)
                    return
            self.state.incidents[inc.id] = inc

        try:
            inc.explanation = self.explainer.explain(finding)
        except Exception:  # noqa: BLE001
            log.exception("explainer failed")

        if not finding.fix:
            self._send(inc, f"⚠️ {finding.alertname}", self._body(inc, footer=self._no_fix_footer(finding)))
            return

        note = ""
        if inc.fix_key in self.state.always_rules:
            if self.state.can_fix(inc.target, self.clock(), self.cfg.max_fixes_per_hour):
                inc.auto = True
                inc.status = "fixing"
                self._execute(inc)
                return
            note = (f"Your fix-always rule for this already ran {self.cfg.max_fixes_per_hour}x in the "
                    "last hour, so I'm asking instead.")

        footer = f"Proposed fix: {finding.fix.describe()}"
        if note:
            footer += "\n" + note
        self._send(inc, f"🔧 {finding.alertname}", self._body(inc, footer=footer), actions=[
            (f"ITAGENT_ONCE_{inc.id}", "Fix once"),
            (f"ITAGENT_ALWAYS_{inc.id}", "Fix always"),
            (f"ITAGENT_DENY_{inc.id}", "I'll handle it"),
        ])

    def _resolved(self, alert):
        dedup = dedup_key(alert)
        with self.state.lock:
            matches = [i for i in self.state.incidents.values()
                       if i.dedup == dedup and i.status in ("pending", "info")]
            for inc in matches:
                was_pending = inc.status == "pending"
                inc.status = "resolved"
                if was_pending:
                    self._send(inc, f"✅ Cleared: {inc.finding.alertname}",
                               f"{inc.finding.subject} recovered on its own. Nothing was run.")

    # --- button presses from Home Assistant ----------------------------------------------------

    def decide(self, action):
        """action is the button id, e.g. ITAGENT_ONCE_1a2b3c4d. Returns (http_status, message)."""
        parts = (action or "").split("_", 2)
        if len(parts) != 3 or parts[0] != "ITAGENT" or parts[1] not in DECISIONS:
            return 400, "unknown action"
        decision, inc_id = DECISIONS[parts[1]], parts[2]
        with self.state.lock:
            inc = self.state.incidents.get(inc_id)
            if inc is None:
                return 404, "no such incident (the agent may have restarted)"
            # "I'll handle it" is also offered on failed / still-failing reports.
            if decision == "deny" and inc.status in ("still_failing", "failed"):
                inc.status = "denied"
                self._send(inc, f"👍 Yours: {inc.finding.alertname}",
                           f"OK, {inc.finding.subject} is yours. I'll stay quiet about it for now.")
                return 200, "denied"
            if inc.status != "pending":
                return 409, f"incident is already {inc.status}"
            if self.clock() - inc.created > self.cfg.proposal_ttl_sec:
                inc.status = "expired"
                self._send(inc, f"⌛ Expired: {inc.finding.alertname}",
                           f"That proposal for {inc.finding.subject} expired, so nothing was run.")
                return 410, "proposal expired"

            if decision == "deny":
                inc.status = "denied"
                log.info("incident %s: you are handling it; details: %s", inc.id, self._body(inc))
                self._send(inc, f"👍 Yours: {inc.finding.alertname}",
                           f"OK, I won't touch {inc.finding.subject}. Full findings are in Loki "
                           f'({{namespace="it-agent"}}, incident {inc.id}).')
                return 200, "denied"

            if not self.state.can_fix(inc.target, self.clock(), self.cfg.max_fixes_per_hour):
                self._send(inc, f"⛔ Rate limit: {inc.finding.alertname}",
                           f"{inc.finding.fix.target()} was already fixed "
                           f"{self.cfg.max_fixes_per_hour}x in the last hour. Not running it again.",
                           actions=[(f"ITAGENT_DENY_{inc.id}", "I'll handle it")])
                return 429, "rate limited"

            if decision == "always":
                self.state.add_rule(inc.fix_key)
            inc.status = "fixing"
        self._execute(inc)
        return 200, decision

    # --- fixing and checking ----------------------------------------------------------------

    def _execute(self, inc):
        fix = inc.finding.fix
        how = "Fix-always rule" if inc.auto else "Approved"
        if not self.cfg.execute_fixes:
            inc.status = "dry_run"
            self._send(inc, f"🧪 Dry run: {inc.finding.alertname}",
                       f"{how}, but EXECUTE_FIXES is off, so I did not run: {fix.describe()}")
            return
        try:
            playbooks.apply_fix(fix, self.k8s)
        except (K8sError, OSError, ValueError) as err:
            inc.status = "failed"
            self._send(inc, f"❌ Fix failed: {inc.finding.alertname}",
                       f"{fix.describe()} failed: {err}", actions=[(f"ITAGENT_DENY_{inc.id}", "I'll handle it")])
            return
        # Rate limit is per workload, not per alert: two alerts must not double the allowance.
        self.state.record_fix(inc.target, self.clock())
        log.info("incident %s: ran %s (%s)", inc.id, fix.describe(), how)
        minutes = max(1, round(self.cfg.verify_delay_sec / 60))
        self._send(inc, f"🔧 Fixing: {inc.finding.alertname}",
                   f"{how}: ran {fix.describe()}. Checking again in {minutes} min.")
        self.schedule(self.cfg.verify_delay_sec, lambda: self._verify(inc))

    def _verify(self, inc):
        fix = inc.finding.fix
        try:
            ok, detail = playbooks.verify(fix, self.k8s)
        except Exception as err:  # noqa: BLE001
            ok, detail = False, f"could not check: {err}"
        inc.status = "fixed" if ok else "still_failing"
        if ok:
            self._send(inc, f"✅ Fixed: {inc.finding.alertname}", f"{fix.target()} is healthy: {detail}.")
        else:
            self._send(inc, f"❌ Still failing: {inc.finding.alertname}",
                       f"After {fix.describe()}, {fix.target()} is still unhealthy ({detail}). "
                       "Over to you.", actions=[(f"ITAGENT_DENY_{inc.id}", "I'll handle it")])

    def sweep(self):
        """Expire unanswered proposals and forget old incidents. Called every minute."""
        now = self.clock()
        with self.state.lock:
            for inc in list(self.state.incidents.values()):
                if inc.status == "pending" and now - inc.created > self.cfg.proposal_ttl_sec:
                    inc.status = "expired"
                    self.notifier.clear(self._tag(inc))
                    log.info("incident %s expired unanswered", inc.id)
                if now - inc.created > 86400:
                    del self.state.incidents[inc.id]

    # --- message helpers --------------------------------------------------------------

    @staticmethod
    def _tag(inc):
        return f"itagent-{inc.id}"

    def _send(self, inc, title, message, actions=None):
        self.notifier.send(title, message, self._tag(inc), actions)

    @staticmethod
    def _no_fix_footer(finding):
        return f"No automatic fix: {finding.no_fix_reason}" if finding.no_fix_reason else ""

    @staticmethod
    def _body(inc, footer=""):
        f = inc.finding
        lines = [f.subject] + f.facts[:4] + f.hints
        lines.append(inc.explanation or "(AI summary unavailable: local model offline)")
        if footer:
            lines.append(footer)
        return "\n".join(lines)
