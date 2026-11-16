"""Helpers for the opt-in live tests (real herdr + claude)."""
import time

TRUST_MARK = "Quick safety check"


def wait_ready(h, names, timeout=120.0, stable_s=8.0):
    """Wait until every agent has been idle/done for `stable_s` (claude keeps initialising for a
    few seconds after its folder-trust dialog). The first-run trust dialog (cursor starts on
    "No, exit", so a bare Enter would quit) is accepted with down+enter for these throwaway dirs."""
    deadline = time.time() + timeout
    since: dict[str, float] = {}
    accepted: set[str] = set()  # the dialog text lingers in scrollback: answer it only once
    pending = set(names)
    while pending and time.time() < deadline:
        live = {a.get("name"): a for a in h.agent_list()}
        for n in list(pending):
            st = (live.get(n) or {}).get("agent_status")
            if st in ("idle", "done"):
                since.setdefault(n, time.time())
                if time.time() - since[n] >= stable_s:
                    pending.discard(n)
                continue
            since.pop(n, None)
            if st == "blocked" and n not in accepted and TRUST_MARK in h.agent_read(n, lines=20):
                h.agent_send_keys(n, "down", "enter")
                accepted.add(n)
        time.sleep(1)
    return pending
