"""Headless-browser login to obtain the Stellantis OAuth authorization code.

Derived from taubenhorst/StellantisHAAddon, app/oauth_browser/login.py
(Copyright (c) 2026 Kilian Taubmann, MIT License). Clean-room implementation,
no code from the unlicensed upstream worker repositories.

Flow: open the OAuth authorize URL, fill the Gigya login form, confirm the
consent form, then catch the redirect to the app scheme (mym*://...?code=...)
which the browser cannot follow. Depending on the Chromium build that shows
up as a failed request, as a plain request event, as a 30x response with a
Location header or as a frame navigation - all four are watched.

Chromium is launched per call and closed afterwards to keep the memory
footprint low. Every step has a timeout and the browser teardown is bounded
too, so a stuck Chromium cannot hang the caller.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlsplit

_LOGGER = logging.getLogger(__name__)

# Selectors of the Stellantis/Gigya login page. Kept in one place because
# they are the part most likely to break on upstream UI changes.
SEL_EMAIL = "#gigya-login-form input[name='username']"
SEL_PASSWORD = "#gigya-login-form input[name='password']"
SEL_SUBMIT = "#gigya-login-form input[type='submit']"
SEL_AUTHORIZE = "#cvs_from input[type='submit']"
# Inline error of the Gigya login form (wrong password, locked account, ...).
SEL_FORM_ERROR = "#gigya-login-form .gigya-error-msg-active, #gigya-login-form .gigya-error-display-active"

APP_SCHEME_PREFIX = "mym"
CLOSE_TIMEOUT_S = 10.0

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--no-zygote",
]


class OauthBrowserError(RuntimeError):
    pass


def _code_from_url(url: str) -> str | None:
    if not url or not url.lower().startswith(APP_SCHEME_PREFIX):
        return None
    query = urlsplit(url).query
    return parse_qs(query).get("code", [None])[0]


def redact(url: str) -> str:
    """URL without query values for logging (the code must not end up in logs)."""
    if not url:
        return ""
    parts = urlsplit(url)
    keys = "&".join(f"{k}=…" for k in parse_qs(parts.query)) if parts.query else ""
    return parts._replace(query=keys).geturl()


async def _bounded(coro, timeout: float, what: str) -> None:
    try:
        await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning("%s did not finish within %.0fs, giving up on it", what, timeout)
    except Exception as err:  # noqa: BLE001 - teardown must never mask the real error
        _LOGGER.debug("%s failed: %s", what, err)


async def fetch_oauth_code(oauth_url: str, email: str, password: str,
                           timeout_s: float = 60.0, debug_dir: str | None = None,
                           on_event=None, locale: str | None = None) -> str:
    """Return the OAuth authorization code for the given credentials.

    ``locale``: browser locale (e.g. ``de-DE``). ``debug_dir``: if set, a
    screenshot and the final URL are written there when no code could be
    captured. ``on_event(source, url)`` is called for every URL seen.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover
        raise OauthBrowserError("playwright is not installed") from err

    loop = asyncio.get_running_loop()
    code_future: asyncio.Future[str] = loop.create_future()
    started = time.monotonic()

    def seen(source: str, url: str) -> None:
        if not url:
            return
        _LOGGER.debug("[%5.1fs] %-16s %s", time.monotonic() - started, source, redact(url))
        if on_event:
            on_event(source, url)
        code = _code_from_url(url)
        if code and not code_future.done():
            code_future.set_result(code)

    failure_future: asyncio.Future[str] = loop.create_future()

    async def log_body(response) -> None:
        # The IdP's own login step: its JSON answer carries the failure reason.
        try:
            text = _redact_tokens(await response.text())
        except Exception as err:  # noqa: BLE001
            text = f"<unreadable: {err}>"
        _LOGGER.debug("authenticate -> %s %s", response.status, text[:800])
        if on_event:
            on_event(f"authenticate {response.status}", text[:300])

    def on_response(response) -> None:
        if 300 <= response.status < 400:
            seen("response-location", response.headers.get("location", ""))
        if "/json/authenticate" in response.url:
            loop.create_task(log_body(response))

    def on_navigated(frame) -> None:
        seen("framenavigated", frame.url)
        if "failedlogin" in frame.url.lower() and not failure_future.done():
            failure_future.set_result(frame.url)

    pw = await async_playwright().start()
    browser = None
    page = None
    try:
        browser = await _launch(pw)
        context = await browser.new_context(viewport={"width": 1280, "height": 720},
                                            locale=locale or "en-US")
        page = await context.new_page()
        page.on("request", lambda r: seen("request", r.url))
        page.on("requestfailed", lambda r: seen("requestfailed", r.url))
        page.on("response", on_response)
        page.on("framenavigated", on_navigated)
        page.on("console", lambda m: _LOGGER.debug("console %s: %s", m.type, m.text[:300]))

        ms = int(timeout_s * 1000)
        _LOGGER.debug("Opening OAuth page")
        await page.goto(oauth_url, wait_until="domcontentloaded", timeout=ms)
        await page.wait_for_selector(SEL_EMAIL, timeout=ms)
        await page.fill(SEL_EMAIL, email)
        await page.fill(SEL_PASSWORD, password)
        await page.click(SEL_SUBMIT)
        _LOGGER.debug("Credentials submitted, waiting for consent form or app redirect")

        async def wait_consent() -> None:
            await page.wait_for_selector(SEL_AUTHORIZE, timeout=ms)
            await page.click(SEL_AUTHORIZE)
            _LOGGER.debug("Consent submitted, waiting for app redirect")

        async def wait_form_error() -> str:
            # Gigya shows wrong credentials as an inline error on the same page;
            # catch it early instead of waiting for the consent-form timeout.
            el = await page.wait_for_selector(SEL_FORM_ERROR, state="visible", timeout=ms)
            text = " ".join(((await el.inner_text()) or "").split())
            return text or "login form reported an error"

        consent_task = loop.create_task(wait_consent())
        error_task = loop.create_task(wait_form_error())
        # Wrappers so asyncio.wait() never cancels the shared futures; they are
        # cancelled below, otherwise they linger as pending tasks.
        code_wait = asyncio.ensure_future(asyncio.shield(code_future))
        failure_wait = asyncio.ensure_future(asyncio.shield(failure_future))
        try:
            # Whatever comes first: the code, the IdP's failure page, an inline
            # form error, or the consent form (after which the code follows).
            done, _ = await asyncio.wait(
                [code_wait, failure_wait, consent_task, error_task],
                timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
            if failure_future.done():
                await _dump_debug(page, debug_dir)
                raise OauthBrowserError(f"Stellantis IdP rejected the login: {await _page_text(page)}")
            if error_task in done and not error_task.exception() and not code_future.done():
                await _dump_debug(page, debug_dir)
                raise OauthBrowserError(f"Login form error: {error_task.result()}")
            if not code_future.done():
                if not done:
                    await _dump_debug(page, debug_dir)
                    raise OauthBrowserError("Neither consent form nor redirect appeared (timeout)")
                if consent_task in done and consent_task.exception():
                    await _dump_debug(page, debug_dir)
                    raise OauthBrowserError(f"Login or consent form not found: {consent_task.exception()}")
                # Consent clicked: give the redirect the remaining time (at least a few seconds).
                remaining = max(10.0, timeout_s - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(asyncio.shield(code_future), timeout=remaining)
                except asyncio.TimeoutError as err:
                    await _dump_debug(page, debug_dir)
                    if failure_future.done():
                        raise OauthBrowserError(f"Stellantis IdP rejected the login: {await _page_text(page)}") from err
                    raise OauthBrowserError("No authorization code captured (timeout)") from err
            return code_future.result()
        finally:
            for task in (consent_task, error_task, code_wait, failure_wait):
                if not task.done():
                    task.cancel()
    finally:
        if browser is not None:
            await _bounded(browser.close(), CLOSE_TIMEOUT_S, "browser.close()")
        await _bounded(pw.stop(), CLOSE_TIMEOUT_S, "playwright.stop()")


async def _launch(pw):
    """Real Chromium in the new headless mode (same UA and fingerprint as a
    normal Chrome) - the headless shell is rejected by the Stellantis IdP.
    Falls back to the shell when the full browser is not installed."""
    try:
        browser = await pw.chromium.launch(channel="chromium", headless=True, args=CHROMIUM_ARGS)
        _LOGGER.debug("Chromium (new headless) %s", browser.version)
        return browser
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Full Chromium not available (%s), using headless shell",
                        (str(err).splitlines() or [repr(err)])[0])
        browser = await pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        _LOGGER.debug("Chromium headless shell %s", browser.version)
        return browser


_TOKEN_RE = re.compile(r'("(?:authId|tokenId|token|code|password|IDToken\d|input)"\s*:\s*")([^"]{6,})"')


def _redact_tokens(text: str) -> str:
    return _TOKEN_RE.sub(lambda m: f'{m.group(1)}{m.group(2)[:6]}…"', text)


async def _page_text(page) -> str:
    try:
        text = await asyncio.wait_for(page.inner_text("body"), 10)
        return " ".join(text.split())[:400]
    except Exception as err:  # noqa: BLE001
        return f"<no page text: {err}>"


async def _dump_debug(page, debug_dir: str | None) -> None:
    if not page:
        return
    try:
        final_url = page.url
        _LOGGER.warning("Stuck on %s", redact(final_url))
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            await asyncio.wait_for(page.screenshot(path=os.path.join(debug_dir, "oauth_last_page.png")), 10)
            with open(os.path.join(debug_dir, "oauth_last_page.txt"), "w", encoding="utf-8") as f:
                f.write(final_url + "\n")
                f.write(await asyncio.wait_for(page.content(), 10))
            _LOGGER.warning("Screenshot and page source written to %s", debug_dir)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not write debug output: %s", err)


def _cli() -> None:  # pragma: no cover - manual diagnostics
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(description="Diagnose the headless Stellantis login")
    parser.add_argument("--url", required=True, help="OAuth authorize URL (as shown in the integration's manual step)")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default=None, help="Asked interactively when omitted")
    parser.add_argument("--locale", default="de-DE")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--debug-dir", default="oauth_debug")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncio").setLevel(logging.INFO)
    password = args.password or getpass.getpass("Passwort: ")

    async def run() -> int:
        events: list[tuple[str, str]] = []
        try:
            code = await fetch_oauth_code(args.url, args.email, password, args.timeout, args.debug_dir,
                                          on_event=lambda s, u: events.append((s, u)), locale=args.locale)
        except OauthBrowserError as err:
            print("FEHLER:", err)
            code = None
        print("\nGesehene URLs (Query-Werte maskiert):")
        for source, seen_url in events:
            print(f"  {source:16s} {redact(seen_url)}")
        print("\nErgebnis:", "Code erhalten (%d Zeichen)" % len(code) if code else "kein Code")
        return 0 if code else 1

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    _cli()
