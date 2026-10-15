"""End-to-end tests against a mock Gmail / Microsoft Graph server.

The unit tests in test_maillaunch.py cover the pieces. These drive the real CLI
through a whole campaign against a local HTTP server standing in for both
providers, which is the only way to exercise the parts that only exist when the
pieces are wired together:

  - the exact request each provider receives, decoded and asserted
  - a 401 mid-campaign triggering a token refresh and a successful retry
  - a 429 backing off and then succeeding
  - a permanent failure being recorded without killing the campaign
  - resume after an interrupt contacting nobody twice

Still local, still no credentials, still runs in CI. What it cannot prove is that
Google and Microsoft accept these request shapes -- for that see "Verifying
against the real APIs" in the README.
"""

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, main, sender, sendlog, store, tokens  # noqa: E402


class MockProvider:
    """A local stand-in for the Gmail, Graph and OAuth token endpoints.

    Responses are scripted per path: queue up `(status, body)` pairs and each
    request pops the next one, falling back to a success once the script runs
    out. Every request is recorded so tests can assert what went over the wire.
    """

    def __init__(self):
        self.received = []
        self.script = {}
        self._server = None
        self._thread = None

    def queue(self, path, *responses):
        self.script.setdefault(path, []).extend(responses)

    def start(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - name fixed by the base class
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                provider.received.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": raw.decode("utf-8", "replace"),
                    }
                )
                queued = provider.script.get(self.path) or []
                if queued:
                    status, body = queued.pop(0)
                else:
                    status, body = 200, {"id": "ok"}
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def sends(self):
        return [r for r in self.received if "send" in r["path"].lower()]

    def token_calls(self):
        return [r for r in self.received if r["path"] == "/token"]


class IntegrationTestCase(unittest.TestCase):
    """Runs the CLI with every endpoint and storage path redirected locally."""

    def setUp(self):
        self.provider = MockProvider()
        self.base = self.provider.start()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        self.csv = root / "recipients.csv"
        self.csv.write_text(
            "email,name,company\n"
            "a@x.com,Alice,Acme\n"
            "b@x.com,Bob,Beta\n"
            "c@x.com,Carol,Gamma\n",
            encoding="utf-8",
        )
        self.body = root / "body.txt"
        self.body.write_text("Hi {{name}}, about {{company}}.\n", encoding="utf-8")
        self.config = root / "config.yaml"
        self.config.write_text(
            "provider: gmail\n"
            "daily_limit: 100\n"
            "gmail:\n  client_id: test-client\n"
            "microsoft:\n  client_id: test-ms\n"
            "send:\n"
            "  min_delay_seconds: 0\n"
            "  max_delay_seconds: 0\n"
            "  retry_failed: true\n"
            "  retry_attempts: 2\n"
            "  retry_backoff_seconds: 1\n",
            encoding="utf-8",
        )

        # An in-memory credential store: never touch the real OS keychain.
        self.vault = {
            "gmail": {"refresh_token": "r0", "access_token": "tok-0", "expires_at": 2 ** 40},
            "microsoft": {
                "refresh_token": "r0",
                "access_token": "tok-0",
                "expires_at": 2 ** 40,
                "tenant": "common",
            },
        }

        patches = [
            mock.patch.object(sender, "GMAIL_SEND", f"{self.base}/gmail/send"),
            mock.patch.object(sender, "GRAPH_SEND", f"{self.base}/graph/sendMail"),
            mock.patch.dict(auth.GMAIL, {"token": f"{self.base}/token"}),
            mock.patch.dict(auth.MICROSOFT, {"token": self.base + "/token"}),
            mock.patch.object(store, "HOME", root),
            mock.patch.object(sendlog, "HOME", root),
            mock.patch.object(tokens, "save", lambda p, d: self.vault.__setitem__(p, d) or "mock"),
            mock.patch.object(tokens, "load", lambda p: self.vault.get(p)),
            mock.patch.dict(os.environ, {"MAILLAUNCH_GMAIL_CLIENT_SECRET": "shh"}),
            # No real waiting: delays are configured to 0, backoff is patched out.
            mock.patch("time.sleep", lambda s: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.provider.stop)

    def run_cli(self, *argv):
        """Invoke the CLI exactly as a shell would, returning its exit code."""
        return main.main(["--config", str(self.config), *argv])

    def send(self, *extra):
        return self.run_cli(
            "send", "--csv", str(self.csv), "--subject", "Hi {{name}}",
            "--body-file", str(self.body), *extra,
        )


class CampaignTests(IntegrationTestCase):
    def test_every_recipient_is_sent_exactly_once(self):
        self.assertEqual(self.send(), 0)
        sends = self.provider.sends()
        self.assertEqual(len(sends), 3)
        self.assertEqual(len(self.provider.token_calls()), 0)  # token was still valid

    def test_gmail_receives_a_valid_base64url_mime_message(self):
        self.send()
        payload = json.loads(self.provider.sends()[0]["body"])
        self.assertIn("raw", payload)
        # Decode it the way Gmail would and check the message actually formed.
        mime = base64.urlsafe_b64decode(payload["raw"].encode()).decode("utf-8", "replace")
        self.assertIn("To: a@x.com", mime)
        self.assertIn("Subject: Hi Alice", mime)
        self.assertIn("about Acme", mime)

    def test_bearer_token_is_sent(self):
        self.send()
        self.assertEqual(self.provider.sends()[0]["auth"], "Bearer tok-0")

    def test_each_recipient_gets_their_own_personalisation(self):
        self.send()
        subjects = []
        for request in self.provider.sends():
            raw = json.loads(request["body"])["raw"]
            mime = base64.urlsafe_b64decode(raw.encode()).decode("utf-8", "replace")
            subjects.append([l for l in mime.splitlines() if l.startswith("Subject:")][0])
        self.assertEqual(
            sorted(subjects),
            ["Subject: Hi Alice", "Subject: Hi Bob", "Subject: Hi Carol"],
        )

    def test_microsoft_receives_the_graph_json_contract(self):
        self.config.write_text(
            self.config.read_text().replace("provider: gmail", "provider: microsoft"),
            encoding="utf-8",
        )
        self.send()
        payload = json.loads(self.provider.sends()[0]["body"])
        message = payload["message"]
        self.assertEqual(message["subject"], "Hi Alice")
        self.assertEqual(message["body"]["contentType"], "Text")
        self.assertEqual(
            message["toRecipients"][0]["emailAddress"]["address"], "a@x.com"
        )
        self.assertTrue(payload["saveToSentItems"])
        # Graph takes JSON, never a MIME blob.
        self.assertNotIn("raw", payload)


class FailureHandlingTests(IntegrationTestCase):
    def test_401_refreshes_the_token_and_retries_the_same_recipient(self):
        """The failure the browser tool could not survive: an hour into a send."""
        self.provider.queue("/gmail/send", (401, {"error": {"message": "Invalid Credentials"}}))
        self.provider.queue("/token", (200, {"access_token": "tok-1", "expires_in": 3600}))

        self.assertEqual(self.send(), 0)

        self.assertEqual(len(self.provider.token_calls()), 1)
        sends = self.provider.sends()
        self.assertEqual(len(sends), 4)  # 3 recipients + 1 replay of the rejected one
        # The retry and everything after it carry the refreshed token.
        self.assertEqual(sends[0]["auth"], "Bearer tok-0")
        self.assertEqual(sends[1]["auth"], "Bearer tok-1")
        self.assertEqual(self.vault["gmail"]["access_token"], "tok-1")

    def test_429_backs_off_and_then_succeeds(self):
        self.provider.queue(
            "/gmail/send", (429, {"error": {"message": "Too Many Requests"}})
        )
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(self.provider.sends()), 4)  # one retried

        connection = store.connect()
        self.addCleanup(connection.close)
        row = connection.execute(
            "SELECT status FROM recipients WHERE email = 'a@x.com'"
        ).fetchone()
        self.assertEqual(row["status"], store.SENT)

    def test_permanent_failure_is_recorded_and_the_campaign_continues(self):
        """One bad address must not cost the other 99 recipients."""
        failure = (500, {"error": {"message": "Backend Error"}})
        self.provider.queue("/gmail/send", failure, failure, failure)

        self.assertEqual(self.send(), 0)

        connection = store.connect()
        self.addCleanup(connection.close)
        tally = {
            r["email"]: r["status"]
            for r in connection.execute("SELECT email, status FROM recipients")
        }
        self.assertEqual(tally["a@x.com"], store.FAILED)
        self.assertEqual(tally["b@x.com"], store.SENT)
        self.assertEqual(tally["c@x.com"], store.SENT)

        log = sendlog.read()
        self.assertIn("FAILED", log)
        self.assertIn("Backend Error", log)
        self.assertIn("RETRY", log)

    def test_failed_refresh_pauses_rather_than_burning_the_list(self):
        """If re-auth cannot fix it, stop -- every later send would fail the same."""
        self.provider.queue("/gmail/send", (401, {"error": {"message": "bad token"}}))
        self.provider.queue("/token", (400, {"error": "invalid_grant"}))

        self.assertEqual(self.send(), 1)

        connection = store.connect()
        self.addCleanup(connection.close)
        campaign = connection.execute("SELECT * FROM campaigns").fetchone()
        self.assertEqual(campaign["status"], store.PAUSED)
        # Only the one rejected attempt went out; the rest of the list is untouched.
        self.assertEqual(len(self.provider.sends()), 1)


class ResumeIntegrationTests(IntegrationTestCase):
    def test_resume_contacts_nobody_twice(self):
        """The core promise: interrupt a campaign, resume it, no duplicate email."""
        with mock.patch.object(
            sender, "send_gmail", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(self.send(), 130)

        # Nothing was delivered, so everyone is still pending.
        self.assertEqual(len(self.provider.sends()), 0)

        self.assertEqual(self.run_cli("resume"), 0)
        delivered = []
        for request in self.provider.sends():
            raw = json.loads(request["body"])["raw"]
            mime = base64.urlsafe_b64decode(raw.encode()).decode("utf-8", "replace")
            delivered.append([l for l in mime.splitlines() if l.startswith("To:")][0])
        self.assertEqual(len(delivered), 3)
        self.assertEqual(len(set(delivered)), 3)

    def test_resume_after_a_partial_send_only_sends_the_remainder(self):
        calls = {"n": 0}
        real_send = sender.send_gmail

        def fail_after_one(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real_send(*args, **kwargs)

        with mock.patch.object(sender, "send_gmail", side_effect=fail_after_one):
            self.assertEqual(self.send(), 130)
        self.assertEqual(len(self.provider.sends()), 1)

        self.assertEqual(self.run_cli("resume"), 0)
        self.assertEqual(len(self.provider.sends()), 3)  # 1 before + 2 after

        addresses = []
        for request in self.provider.sends():
            raw = json.loads(request["body"])["raw"]
            mime = base64.urlsafe_b64decode(raw.encode()).decode("utf-8", "replace")
            addresses.append([l for l in mime.splitlines() if l.startswith("To:")][0])
        self.assertEqual(len(set(addresses)), 3, "a recipient was contacted twice")

    def test_daily_limit_pauses_and_resume_finishes_the_campaign(self):
        self.config.write_text(
            self.config.read_text().replace("daily_limit: 100", "daily_limit: 2"),
            encoding="utf-8",
        )
        self.assertEqual(self.send(), 0)
        self.assertEqual(len(self.provider.sends()), 2)
        self.assertIn("LIMIT_REACHED", sendlog.read())

        # Next day, in effect: the cap is lifted and the rest goes out.
        self.config.write_text(
            self.config.read_text().replace("daily_limit: 2", "daily_limit: 100"),
            encoding="utf-8",
        )
        self.assertEqual(self.run_cli("resume"), 0)
        self.assertEqual(len(self.provider.sends()), 3)


if __name__ == "__main__":
    unittest.main()
