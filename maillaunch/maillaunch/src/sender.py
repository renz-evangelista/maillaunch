"""The sending engine: provider calls, retry policy, and the send loop.

Split so the parts worth testing have no network in them. `send_with_retry`
takes the send function as an argument, so the retry and backoff behaviour is
tested against a mock rather than against Gmail.
"""

import base64
import random
import time
from email.message import EmailMessage

import requests

GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GRAPH_SEND = "https://graph.microsoft.com/v1.0/me/sendMail"

# Raised past the retry loop: a 401 is fixed by refreshing a token, not by waiting.
class AuthExpired(Exception):
    """The provider rejected our credentials."""


class SendFailed(Exception):
    """A send failed for a reason that may be worth retrying."""


def build_mime(sender, to, subject, body):
    """An RFC 2822 message, base64url-encoded the way the Gmail API wants it.

    EmailMessage does the RFC 2047 encoding of a non-ASCII subject for us, and
    picks quoted-printable or base64 for a non-ASCII body. Hand-rolling that
    header encoding is where the original single-file tool had to be careful.
    """
    message = EmailMessage()
    message["To"] = to
    if sender:
        message["From"] = sender
    message["Subject"] = subject
    message.set_content(body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send_gmail(token, to, subject, body, sender=None, session=None):
    raw = build_mime(sender, to, subject, body)
    response = (session or requests).post(
        GMAIL_SEND,
        headers={"Authorization": f"Bearer {token}"},
        json={"raw": raw},
        timeout=60,
    )
    _check(response)


def send_microsoft(token, to, subject, body, sender=None, session=None):
    """Graph takes JSON -- no MIME construction needed."""
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": True,
    }
    response = (session or requests).post(
        GRAPH_SEND,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=60,
    )
    _check(response)


def _check(response):
    if response.status_code == 401:
        raise AuthExpired(f"HTTP 401 - {_message(response)}")
    if response.status_code >= 400:
        raise SendFailed(f"HTTP {response.status_code} - {_message(response)}")


def _message(response):
    """Pull the provider's own error text out, so the log says why."""
    try:
        data = response.json()
        error = data.get("error", data)
        if isinstance(error, dict):
            return error.get("message") or error.get("error_description") or str(error)
        return str(error)
    except ValueError:
        return (response.text or response.reason or "").strip()[:200]


def backoff_delay(attempt, base=30):
    """Exponential: 30s, 60s, 120s ... Attempt numbers start at 1."""
    return base * (2 ** (attempt - 1))


def send_with_retry(send, attempts, on_retry=None, sleep=time.sleep, base=30):
    """Call `send`, retrying transient failures with exponential backoff.

    `attempts` is the number of *retries* after the first try, matching the
    config's `retry_attempts: 2` and the brief's "attempt 1/2" log line.

    An AuthExpired is never retried here -- the caller refreshes the token and
    calls again, because sleeping does not fix an expired credential.
    """
    last_error = None
    for attempt in range(attempts + 1):
        try:
            send()
            return attempt
        except AuthExpired:
            raise
        except (SendFailed, requests.RequestException) as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = backoff_delay(attempt + 1, base)
            if on_retry:
                on_retry(attempt + 1, attempts, delay, exc)
            sleep(delay)
    raise SendFailed(str(last_error))


def random_delay(minimum, maximum, rng=None):
    """Uniform delay between sends, the spam-filter avoidance from the original."""
    return (rng or random).uniform(minimum, maximum)
