"""Watchdog: keeps the Stellantis Vehicles config entry authenticated.

Every check_interval:
  1. Optionally reload the config entry ("reload probe"). The integration only
     notices an invalid refresh token during setup - at runtime the failed
     refresh is swallowed and stale data is kept. A reload turns the silent
     failure into setup_error plus a pending reauth flow.
  2. If the entry is in setup_error (invalid_grant) or a reauth flow is
     pending, drive the integration's reauth flow through the REST API:
       reauth_confirm -> oauth_mode (automatic) -> oauth_remote (own worker URL,
       credentials from the options) -> get_access_token (remote commands) ->
       options -> abort: reauth_successful.
  3. Publish the outcome (status object -> MQTT sensors).
"""
from __future__ import annotations

import asyncio
import logging
import time

from ha_api import HaApi, HaApiError

_LOGGER = logging.getLogger(__name__)

DOMAIN = "stellantis_vehicles"
NOTIFICATION_ID = "stellantis_token_keeper"
RELOAD_SETTLE_S = 45
POST_REAUTH_SETTLE_S = 30
MAX_FLOW_STEPS = 12


class ReauthError(RuntimeError):
    pass


class Watchdog:
    def __init__(self, settings, ha: HaApi, status):
        self._settings = settings
        self._ha = ha
        self._status = status
        self._wake = asyncio.Event()

    def trigger(self) -> None:
        """Run a check now (e.g. from the HTTP API)."""
        self._wake.set()

    async def run(self) -> None:
        # First check shortly after start, then on the configured interval.
        await asyncio.sleep(20)
        while True:
            try:
                await self.check()
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Watchdog check failed: %s", err)
                self._status.check_result(False, f"check failed: {err}")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._settings.check_interval_seconds)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------

    async def check(self) -> None:
        _LOGGER.info("Watchdog check started")
        entries = await self._ha.get_entries(DOMAIN)
        if not entries:
            self._status.check_result(False, "no stellantis_vehicles config entry found")
            _LOGGER.warning("No %s config entry found", DOMAIN)
            return

        all_ok = True
        messages = []
        for entry in entries:
            ok, message = await self._check_entry(entry)
            all_ok = all_ok and ok
            messages.append(f"{entry.get('title')}: {message}")
        summary = "; ".join(messages)
        self._status.check_result(all_ok, summary)
        _LOGGER.info("Watchdog check finished: %s", summary)

        if self._settings.dry_run:
            return
        try:
            if all_ok:
                await self._ha.dismiss_notification(NOTIFICATION_ID)
            else:
                await self._ha.persistent_notification(
                    NOTIFICATION_ID, "Stellantis Token Keeper",
                    f"Automatische Neuanmeldung fehlgeschlagen: {summary}")
        except HaApiError as err:
            _LOGGER.debug("Notification update failed: %s", err)

    async def _check_entry(self, entry: dict) -> tuple[bool, str]:
        entry_id = entry["entry_id"]
        state = entry.get("state")
        _LOGGER.info("Entry %s (%s): state=%s reason=%s", entry.get("title"), entry_id, state, entry.get("reason"))

        if state == "loaded" and self._settings.reload_probe:
            if self._settings.dry_run:
                _LOGGER.info("[dry-run] would reload entry %s", entry_id)
            else:
                _LOGGER.info("Reload probe: reloading entry %s", entry_id)
                await self._ha.reload_entry(entry_id)
                await asyncio.sleep(RELOAD_SETTLE_S)
                entry = await self._ha.get_entry(DOMAIN, entry_id) or entry
                state = entry.get("state")
                _LOGGER.info("After reload: state=%s reason=%s", state, entry.get("reason"))

        flow = await self._find_reauth_flow(entry_id)
        needs_reauth = flow is not None or state in ("setup_error", "setup_retry")
        if not needs_reauth:
            return True, f"ok ({state})"

        if state == "loaded" and flow is not None:
            # Entry is loaded but a stale reauth flow lingers: complete it anyway,
            # it refreshes the tokens.
            _LOGGER.info("Entry loaded but reauth flow pending, completing it")

        reason = str(entry.get("reason") or "")
        if flow is None and "invalid_grant" not in reason and state != "setup_retry":
            return False, f"{state}: {reason or 'unknown reason'} (not an auth problem, nothing to do)"

        if not self._settings.email or not self._settings.password:
            return False, "re-authentication needed but no credentials configured"
        if self._settings.dry_run:
            return False, f"[dry-run] would re-authenticate (state={state}, flow={'yes' if flow else 'no'})"

        try:
            await self._reauth(entry_id, flow)
        except (ReauthError, HaApiError) as err:
            _LOGGER.error("Re-authentication failed: %s", err)
            self._status.reauth_result(False, str(err))
            return False, f"re-authentication failed: {err}"

        await asyncio.sleep(POST_REAUTH_SETTLE_S)
        entry = await self._ha.get_entry(DOMAIN, entry_id) or {}
        state = entry.get("state")
        if state == "loaded":
            self._status.reauth_result(True, "re-authenticated")
            return True, "re-authenticated, entry loaded"
        self._status.reauth_result(False, f"entry state after reauth: {state} {entry.get('reason')}")
        return False, f"re-authentication done but entry is {state}: {entry.get('reason')}"

    # ------------------------------------------------------------------

    async def _find_reauth_flow(self, entry_id: str) -> dict | None:
        for flow in await self._ha.list_flows():
            ctx = flow.get("context") or {}
            if flow.get("handler") == DOMAIN and ctx.get("source") == "reauth" and ctx.get("entry_id") == entry_id:
                return flow
        return None

    async def _reauth(self, entry_id: str, flow: dict | None) -> None:
        if flow is None:
            # A failed setup starts the reauth flow; make sure one exists.
            _LOGGER.info("No reauth flow pending, reloading entry to trigger one")
            await self._ha.reload_entry(entry_id)
            for _ in range(6):
                await asyncio.sleep(5)
                flow = await self._find_reauth_flow(entry_id)
                if flow:
                    break
            if flow is None:
                raise ReauthError("integration did not start a reauth flow after reload")

        flow_id = flow["flow_id"]
        _LOGGER.info("Driving reauth flow %s (current step: %s)", flow_id, flow.get("step_id"))
        # Do not GET the flow: Home Assistant re-renders the current step for a GET,
        # which for get_access_token would exchange the code a second time. The
        # progress list carries the step id, and the forms we answer without
        # looking at their schema; later steps come back as POST results.
        current = {"type": "form", "step_id": flow.get("step_id") or "reauth_confirm"}
        started = time.monotonic()
        for _ in range(MAX_FLOW_STEPS):
            kind = current.get("type")
            if kind == "abort":
                reason = current.get("reason")
                if reason == "reauth_successful":
                    _LOGGER.info("Reauth flow finished successfully in %.0fs", time.monotonic() - started)
                    return
                raise ReauthError(f"flow aborted: {reason} {current.get('description_placeholders') or ''}")
            if kind == "create_entry":
                _LOGGER.info("Reauth flow created entry")
                return
            if kind != "form":
                raise ReauthError(f"unexpected flow result type {kind}")

            errors = current.get("errors") or {}
            step = current.get("step_id")
            if errors:
                raise ReauthError(f"step {step} returned errors: {errors}")

            user_input = self._answer(step, current)
            if user_input is None:
                raise ReauthError(f"step {step} cannot be completed automatically")
            _LOGGER.info("Submitting step %s", step)
            current = await self._ha.post_flow(flow_id, user_input)
        raise ReauthError("too many flow steps")

    def _answer(self, step: str, form: dict) -> dict | None:
        s = self._settings
        if step == "reauth_confirm":
            return {}
        if step == "oauth_mode":
            return {"oauth_manual_mode": False}
        if step == "oauth_remote":
            if not s.worker_url:
                raise ReauthError("worker_url is not set")
            return {"email": s.email, "password": s.password, "oauth_code_url": s.worker_url}
        if step == "get_access_token":
            return {"remote_commands": bool(s.remote_commands)}
        if step == "otp":
            # SMS code + PIN: cannot be automated. The flow stays open for the UI.
            return None
        if step == "options":
            return self._defaults(form)
        if step == "oauth_manual":
            return None
        # Unknown step: try the schema defaults, fail if a required field has none.
        return self._defaults(form, strict=True)

    @staticmethod
    def _defaults(form: dict, strict: bool = False) -> dict | None:
        result = {}
        for field in form.get("data_schema") or []:
            name = field.get("name")
            if "default" in field:
                result[name] = field["default"]
            elif field.get("required"):
                if strict:
                    return None
        return result
