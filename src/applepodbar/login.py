"""One-time, interactive MusicKit sign-in through a real Chromium window.

Apple ID/password and 2FA stay in the browser. We observe only the
``media-user-token`` header on requests to Apple's AMP host.
"""

from __future__ import annotations

import shlex
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from applepodbar.credentials import INSTALL_LOGIN_EXTRA, UserSession, save_session


class LoginError(RuntimeError):
    """Browser login failed or no user credential was observed."""


def capture_user_session(
    context: Any, page: Any, *, timeout_seconds: int = 300
) -> UserSession:
    """Listen across pages/popups until MusicKit makes an authenticated call.

    The listener is registered before navigation so even early XHRs are seen.
    A user may need to click Library after sign-in to provoke an AMP call.
    """

    captured: dict[str, str] = {}

    def on_request(request: Any) -> None:
        if urlsplit(request.url).hostname != "amp-api.podcasts.apple.com":
            return
        # `all_headers()` performs an async protocol round-trip even through
        # Playwright's sync API; an in-flight event during shutdown can then
        # raise TargetClosedError in the event dispatcher. The already-cached
        # request.headers includes MusicKit's custom header without that race.
        headers = request.headers
        token = headers.get("media-user-token")
        if token:
            captured["token"] = token
            if dsid := headers.get("x-dsid"):
                captured["dsid"] = dsid

    context.on("request", on_request)
    try:
        page.goto("https://podcasts.apple.com", wait_until="domcontentloaded")
        print(
            "Sign in to Apple Podcasts in the browser window. If already signed in, open Library."
        )
        # The web player's button label changes with locale/deploy. Try the
        # English label, but leave the browser open for manual navigation.
        try:
            sign_in = page.get_by_role("button", name="Sign In")
            if sign_in.count():
                sign_in.first.click(timeout=3000)
        except Exception:  # noqa: BLE001 - the user can click sign-in manually
            print("Sign-in button not found automatically; click it in the browser.")
        deadline = time.monotonic() + timeout_seconds
        while not captured and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        if not captured:
            raise LoginError(
                "No Apple Podcasts user token was observed. Sign in and open Library, then retry `applepodbar login`."
            )
        cookies = context.cookies()
        return UserSession(
            token=captured["token"],
            cookies=tuple(cookies),
            dsid=captured.get("dsid"),
        )
    finally:
        context.remove_listener("request", on_request)


def login() -> None:
    """Launch headed Chromium and save the observed user session in keyring."""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginError(
            f"Sign-in needs the login extra. {INSTALL_LOGIN_EXTRA}"
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context()
                page = context.new_page()
                session = capture_user_session(context, page)
                save_session(session)
            finally:
                browser.close()
    except PlaywrightError as exc:
        # sys.executable is the interpreter running us, so this works from a
        # `uv tool` install and a source checkout alike.
        install = f"{shlex.quote(sys.executable)} -m playwright install chromium"
        raise LoginError(
            f"Could not launch Chromium. Run `{install}` and try again."
        ) from exc
    print("Apple Podcasts sign-in saved in your OS keyring.")
