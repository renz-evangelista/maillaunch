"""OAuth for Gmail and Microsoft Graph.

Both use the authorisation-code flow with PKCE and a loopback redirect, which is
the flow Google and Microsoft both document for installed apps: open the system
browser, catch the redirect on 127.0.0.1, swap the code for tokens. No client
secret is needed for Microsoft; Google issues one for installed apps and it is
read from the environment, never the config file.

Only the refresh token really matters. Access tokens last an hour and are
refreshed on demand, so a campaign that runs for five hours does not die at the
sixty minute mark.
"""

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from . import tokens

GMAIL = {
    "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
    "token": "https://oauth2.googleapis.com/token",
    "scope": "https://www.googleapis.com/auth/gmail.send",
}
MICROSOFT = {
    "authorize": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
    "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    # offline_access is what makes Microsoft return a refresh token at all.
    "scope": "https://graph.microsoft.com/Mail.Send offline_access",
}

TIMEOUT_SECONDS = 300


class AuthError(Exception):
    """Raised when authentication or a token refresh fails."""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures ?code= from the redirect."""

    result = None

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = query
        ok = "code" in query
        body = (
            "<h2>MailLaunch is authorised.</h2><p>You can close this tab.</p>"
            if ok
            else f"<h2>Authorisation failed.</h2><pre>{query.get('error', ['unknown'])[0]}</pre>"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass  # the HTTP server's own logging would interleave with ours


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _endpoints(provider, tenant="common"):
    if provider == "gmail":
        return GMAIL
    return {key: value.format(tenant=tenant) for key, value in MICROSOFT.items()}


def authorize(provider, client_id, client_secret=None, tenant="common", open_browser=True):
    """Run the interactive flow and persist the resulting tokens."""
    endpoints = _endpoints(provider, tenant)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    # Port 0 lets the OS pick a free port. Both providers allow any port on
    # 127.0.0.1 for installed apps, so nothing needs pre-registering.
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    _CallbackHandler.result = None

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": endpoints["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if provider == "gmail":
        # Without these Google omits the refresh token on re-authorisation.
        params["access_type"] = "offline"
        params["prompt"] = "consent"

    url = f"{endpoints['authorize']}?{urllib.parse.urlencode(params)}"
    print(f"Opening your browser to authorise {provider}.")
    print(f"If it does not open, visit:\n  {url}\n")
    if open_browser:
        webbrowser.open(url)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while thread.is_alive() and time.monotonic() < deadline:
        thread.join(timeout=0.5)
    server.server_close()

    query = _CallbackHandler.result
    if not query:
        raise AuthError(f"Timed out after {TIMEOUT_SECONDS}s waiting for the browser redirect.")
    if "error" in query:
        raise AuthError(f"Authorisation was refused: {query['error'][0]}")
    if query.get("state", [None])[0] != state:
        # A mismatch means the response is not from the request we started.
        raise AuthError("State mismatch on the OAuth callback; aborting.")

    payload = {
        "client_id": client_id,
        "code": query["code"][0],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    response = requests.post(endpoints["token"], data=payload, timeout=30)
    if response.status_code != 200:
        raise AuthError(f"Token exchange failed ({response.status_code}): {response.text}")

    data = response.json()
    if "refresh_token" not in data:
        raise AuthError(
            "The provider did not return a refresh token, so sends would stop after "
            "an hour. For Gmail, revoke the app at myaccount.google.com and retry."
        )
    bundle = {
        "refresh_token": data["refresh_token"],
        "access_token": data.get("access_token"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60,
        "tenant": tenant,
    }
    where = tokens.save(provider, bundle)
    return where


def refresh(provider, client_id, client_secret=None, bundle=None):
    """Swap the refresh token for a new access token and persist it."""
    bundle = bundle or tokens.load(provider)
    if not bundle:
        raise AuthError(f"Not authenticated with {provider}. Run `maillaunch auth {provider}`.")

    endpoints = _endpoints(provider, bundle.get("tenant", "common"))
    payload = {
        "client_id": client_id,
        "refresh_token": bundle["refresh_token"],
        "grant_type": "refresh_token",
    }
    if client_secret:
        payload["client_secret"] = client_secret

    response = requests.post(endpoints["token"], data=payload, timeout=30)
    if response.status_code != 200:
        raise AuthError(
            f"Token refresh failed ({response.status_code}): {response.text}\n"
            f"Run `maillaunch auth {provider}` to re-authorise."
        )

    data = response.json()
    bundle["access_token"] = data["access_token"]
    bundle["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
    # Google keeps the same refresh token; Microsoft rotates it on every refresh.
    if data.get("refresh_token"):
        bundle["refresh_token"] = data["refresh_token"]
    tokens.save(provider, bundle)
    return bundle


def access_token(provider, client_id, client_secret=None, force=False):
    """A valid access token, refreshing first if it is expired or about to be."""
    bundle = tokens.load(provider)
    if not bundle:
        raise AuthError(f"Not authenticated with {provider}. Run `maillaunch auth {provider}`.")
    if force or not bundle.get("access_token") or time.time() >= bundle.get("expires_at", 0):
        bundle = refresh(provider, client_id, client_secret, bundle)
    return bundle["access_token"]
