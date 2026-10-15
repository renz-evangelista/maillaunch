"""Unit tests for MailLaunch.

Run with `python -m pytest` (or `python -m unittest discover tests`).

Covers the five areas the brief names -- CSV parsing, template rendering, email
column detection, daily limit enforcement, retry/backoff with a mocked send --
plus the parts most likely to break quietly: resume skipping already-sent
recipients, and MIME subject encoding.

Nothing here touches the network or the real ~/.maillaunch directory.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import recipients, sender, sendlog, store, template  # noqa: E402


def write_csv(text, suffix=".csv", encoding="utf-8"):
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding=encoding)
    handle.write(text)
    handle.close()
    return handle.name


# --------------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------------- #

class CsvParsingTests(unittest.TestCase):
    def test_standard_csv(self):
        path = write_csv("email,name\njohn@acme.com,John Smith\njane@corp.com,Jane Doe\n")
        rows, column = recipients.load(path)
        self.assertEqual(column, "email")
        self.assertEqual([r["email"] for r in rows], ["john@acme.com", "jane@corp.com"])
        self.assertEqual(rows[0]["name"], "John Smith")

    def test_bom_prefixed_header_is_stripped(self):
        """Without utf-8-sig the first header becomes '﻿email' and nothing matches."""
        path = write_csv("﻿email,name\njohn@acme.com,John\n")
        rows, column = recipients.load(path)
        self.assertEqual(column, "email")
        self.assertEqual(rows[0]["email"], "john@acme.com")

    def test_quoted_commas_stay_in_one_field(self):
        path = write_csv('email,company\njohn@acme.com,"Acme, Inc."\n')
        rows, _ = recipients.load(path)
        self.assertEqual(rows[0]["data"]["company"], "Acme, Inc.")

    def test_blank_rows_are_dropped(self):
        path = write_csv("email,name\njohn@acme.com,John\n\n,\njane@corp.com,Jane\n")
        rows, _ = recipients.load(path)
        self.assertEqual(len(rows), 2)

    def test_invalid_and_duplicate_addresses_are_flagged_not_dropped(self):
        path = write_csv(
            "email,name\njohn@acme.com,John\nnot-an-email,Bad\njohn@acme.com,Dupe\n"
        )
        rows, _ = recipients.load(path)
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0]["skip_reason"])
        self.assertIn("not a valid email", rows[1]["skip_reason"])
        self.assertIn("duplicate", rows[2]["skip_reason"])

    def test_utf8_values_survive(self):
        path = write_csv("email,name\njosé@acme.com,José Álvarez\n")
        rows, _ = recipients.load(path)
        self.assertEqual(rows[0]["name"], "José Álvarez")

    def test_empty_file_raises(self):
        path = write_csv("email,name\n")
        with self.assertRaises(recipients.RecipientError):
            recipients.load(path)

    def test_missing_file_raises_a_user_facing_error(self):
        """Not an OSError: the CLI prints RecipientError, so a typo'd --csv path
        must not reach the user as a traceback."""
        with self.assertRaises(recipients.RecipientError) as ctx:
            recipients.load("/definitely/not/here.csv")
        self.assertIn("Cannot read", str(ctx.exception))

    def test_non_utf8_file_raises_a_user_facing_error(self):
        """A latin-1 export fails on the header, before any row is parsed."""
        path = write_csv("email,name\nj@x.com,Jos\xe9\n", encoding="latin-1")
        with self.assertRaises(recipients.RecipientError) as ctx:
            recipients.load(path)
        self.assertIn("not valid UTF-8", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Email column auto-detection
# --------------------------------------------------------------------------- #

class EmailColumnTests(unittest.TestCase):
    def test_exact_email_header(self):
        self.assertEqual(recipients.detect_email_column(["name", "email"]), "email")

    def test_case_insensitive(self):
        self.assertEqual(recipients.detect_email_column(["Name", "EMAIL"]), "EMAIL")

    def test_header_containing_mail(self):
        self.assertEqual(recipients.detect_email_column(["name", "work_mail"]), "work_mail")

    def test_exact_email_beats_other_mail_columns(self):
        """Column order must not decide this -- 'email' wins wherever it sits."""
        self.assertEqual(
            recipients.detect_email_column(["mailing_list", "email", "backup_mail"]), "email"
        )

    def test_no_match_raises_with_available_columns(self):
        with self.assertRaises(recipients.RecipientError) as ctx:
            recipients.detect_email_column(["name", "company"])
        self.assertIn("company", str(ctx.exception))

    def test_explicit_override(self):
        path = write_csv("contact,name\njohn@acme.com,John\n")
        rows, column = recipients.load(path, email_column="contact")
        self.assertEqual(column, "contact")
        self.assertEqual(rows[0]["email"], "john@acme.com")

    def test_override_with_unknown_column_raises(self):
        path = write_csv("email,name\njohn@acme.com,John\n")
        with self.assertRaises(recipients.RecipientError):
            recipients.load(path, email_column="nope")


# --------------------------------------------------------------------------- #
# Template rendering
# --------------------------------------------------------------------------- #

class TemplateTests(unittest.TestCase):
    def test_simple_substitution(self):
        text, missing = template.render("Hi {{name}}", {"name": "John"})
        self.assertEqual(text, "Hi John")
        self.assertEqual(missing, [])

    def test_multiple_and_repeated_variables(self):
        text, _ = template.render(
            "{{name}} at {{company}}, hi {{name}}", {"name": "Jo", "company": "Acme"}
        )
        self.assertEqual(text, "Jo at Acme, hi Jo")

    def test_whitespace_inside_braces_is_tolerated(self):
        text, _ = template.render("Hi {{ name }}", {"name": "John"})
        self.assertEqual(text, "Hi John")

    def test_missing_key_is_reported(self):
        text, missing = template.render("Hi {{name}} of {{company}}", {"name": "John"})
        self.assertEqual(missing, ["company"])
        self.assertEqual(text, "Hi John of ")

    def test_blank_value_counts_as_missing(self):
        """An empty cell is a hole, not a personalisation -- never mail 'Hi ,'."""
        _, missing = template.render("Hi {{name}}", {"name": "   "})
        self.assertEqual(missing, ["name"])

    def test_nested_braces_are_left_alone(self):
        text, missing = template.render("{{{name}}}", {"name": "John"})
        self.assertEqual(text, "{John}")
        self.assertEqual(missing, [])

    def test_unclosed_brace_is_not_a_placeholder(self):
        text, missing = template.render("Hi {{name", {"name": "John"})
        self.assertEqual(text, "Hi {{name")
        self.assertEqual(missing, [])

    def test_placeholders_lists_names_in_order(self):
        self.assertEqual(
            template.placeholders("{{a}} {{b}} {{a}}"), ["a", "b"]
        )

    def test_value_containing_braces_is_not_re_expanded(self):
        text, _ = template.render("{{name}}", {"name": "{{company}}", "company": "Acme"})
        self.assertEqual(text, "{{company}}")


# --------------------------------------------------------------------------- #
# Daily limit
# --------------------------------------------------------------------------- #

class DailyLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = store.connect(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _campaign(self, rows, campaign_id="c_test_001"):
        store.create_campaign(
            self.connection, campaign_id, "gmail", "s", "b", "x.csv", rows
        )
        return campaign_id

    def _rows(self, n):
        return [
            {
                "row": i,
                "email": f"a{i}@x.com",
                "name": None,
                "data": {},
                "skip_reason": None,
            }
            for i in range(1, n + 1)
        ]

    def test_zero_sent_initially(self):
        self._campaign(self._rows(3))
        self.assertEqual(store.sent_today(self.connection), 0)

    def test_counts_only_sent_rows(self):
        cid = self._campaign(self._rows(4))
        store.mark(self.connection, cid, 1, store.SENT)
        store.mark(self.connection, cid, 2, store.FAILED, error="boom")
        store.mark(self.connection, cid, 3, store.SKIPPED, error="no email")
        self.assertEqual(store.sent_today(self.connection), 1)

    def test_retries_do_not_consume_quota(self):
        """Three attempts that end in one delivery is one email against the limit."""
        cid = self._campaign(self._rows(1))
        store.mark(self.connection, cid, 1, store.SENT, attempts=3)
        self.assertEqual(store.sent_today(self.connection), 1)

    def test_yesterdays_sends_do_not_count(self):
        cid = self._campaign(self._rows(2))
        store.mark(self.connection, cid, 1, store.SENT)
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(
            timespec="seconds"
        )
        with self.connection:
            self.connection.execute(
                "UPDATE recipients SET sent_at = ? WHERE row_index = 1", (yesterday,)
            )
        self.assertEqual(store.sent_today(self.connection), 0)

    def test_limit_counts_across_campaigns(self):
        """The cap protects one human's sending reputation, not one campaign."""
        a = self._campaign(self._rows(2), "c_test_001")
        b = self._campaign(self._rows(2), "c_test_002")
        store.mark(self.connection, a, 1, store.SENT)
        store.mark(self.connection, b, 1, store.SENT)
        self.assertEqual(store.sent_today(self.connection), 2)


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #

class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.connection = store.connect(Path(self.tmp.name) / "state.db")
        rows = [
            {"row": 1, "email": "a@x.com", "name": None, "data": {}, "skip_reason": None},
            {"row": 2, "email": "b@x.com", "name": None, "data": {}, "skip_reason": None},
            {"row": 3, "email": "bad", "name": None, "data": {}, "skip_reason": "invalid"},
        ]
        store.create_campaign(self.connection, "c_1", "gmail", "s", "b", "x.csv", rows)

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def test_unsendable_rows_start_skipped_not_pending(self):
        self.assertEqual([r["row"] for r in store.pending(self.connection, "c_1")], [1, 2])

    def test_sent_recipients_are_not_returned_again(self):
        store.mark(self.connection, "c_1", 1, store.SENT)
        self.assertEqual([r["row"] for r in store.pending(self.connection, "c_1")], [2])

    def test_failed_recipients_are_not_retried_on_resume(self):
        """A row that exhausted its retries is terminal; resume is for interruptions."""
        store.mark(self.connection, "c_1", 1, store.FAILED, error="HTTP 500")
        self.assertEqual([r["row"] for r in store.pending(self.connection, "c_1")], [2])

    def test_latest_resumable_finds_a_paused_campaign(self):
        store.set_campaign_status(self.connection, "c_1", store.PAUSED)
        self.assertEqual(store.latest_resumable(self.connection)["id"], "c_1")

    def test_finished_campaigns_are_not_resumable(self):
        store.set_campaign_status(self.connection, "c_1", store.DONE)
        self.assertIsNone(store.latest_resumable(self.connection))

    def test_campaign_id_increments_within_a_day(self):
        now = datetime(2026, 10, 9)
        self.assertEqual(store.new_campaign_id(self.connection, now), "c_20261009_001")
        store.create_campaign(
            self.connection, "c_20261009_001", "gmail", "s", "b", "x.csv", []
        )
        self.assertEqual(store.new_campaign_id(self.connection, now), "c_20261009_002")


# --------------------------------------------------------------------------- #
# Retry / backoff, with the send function mocked
# --------------------------------------------------------------------------- #

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.slept = []

    def _sleep(self, seconds):
        self.slept.append(seconds)

    def test_success_first_try_makes_no_retries(self):
        calls = []
        sender.send_with_retry(lambda: calls.append(1), attempts=2, sleep=self._sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])

    def test_retries_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise sender.SendFailed("HTTP 429 - Too Many Requests")

        used = sender.send_with_retry(flaky, attempts=2, sleep=self._sleep)
        self.assertEqual(len(calls), 3)
        self.assertEqual(used, 2)

    def test_gives_up_after_configured_attempts(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise sender.SendFailed("HTTP 500")

        with self.assertRaises(sender.SendFailed):
            sender.send_with_retry(always_fails, attempts=2, sleep=self._sleep)
        self.assertEqual(len(calls), 3)  # first try + 2 retries

    def test_backoff_is_exponential(self):
        def always_fails():
            raise sender.SendFailed("HTTP 500")

        with self.assertRaises(sender.SendFailed):
            sender.send_with_retry(always_fails, attempts=3, sleep=self._sleep, base=30)
        self.assertEqual(self.slept, [30, 60, 120])

    def test_auth_errors_are_not_retried(self):
        """Sleeping does not fix an expired token -- the caller refreshes instead."""
        calls = []

        def expired():
            calls.append(1)
            raise sender.AuthExpired("HTTP 401")

        with self.assertRaises(sender.AuthExpired):
            sender.send_with_retry(expired, attempts=2, sleep=self._sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])

    def test_retry_callback_reports_progress(self):
        seen = []

        def always_fails():
            raise sender.SendFailed("HTTP 429")

        with self.assertRaises(sender.SendFailed):
            sender.send_with_retry(
                always_fails,
                attempts=2,
                on_retry=lambda n, total, delay, exc: seen.append((n, total, delay)),
                sleep=self._sleep,
            )
        self.assertEqual(seen, [(1, 2, 30), (2, 2, 60)])

    def test_zero_attempts_means_no_retry(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise sender.SendFailed("HTTP 500")

        with self.assertRaises(sender.SendFailed):
            sender.send_with_retry(always_fails, attempts=0, sleep=self._sleep)
        self.assertEqual(len(calls), 1)

    def test_random_delay_stays_within_bounds(self):
        for _ in range(200):
            self.assertTrue(60 <= sender.random_delay(60, 180) <= 180)


# --------------------------------------------------------------------------- #
# MIME construction and logging
# --------------------------------------------------------------------------- #

class MimeTests(unittest.TestCase):
    def _decode(self, raw):
        import base64

        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", "replace")

    def test_ascii_subject_is_plain(self):
        raw = sender.build_mime("me@x.com", "you@y.com", "Hello there", "Body")
        self.assertIn("Subject: Hello there", self._decode(raw))

    def test_non_ascii_subject_is_rfc2047_encoded(self):
        """A raw UTF-8 subject header is illegal; it has to be =?utf-8?...?= encoded."""
        raw = sender.build_mime("me@x.com", "you@y.com", "Hola José 👋", "Body")
        decoded = self._decode(raw)
        self.assertIn("=?utf-8?", decoded.lower())
        self.assertNotIn("Subject: Hola José", decoded)

    def test_base64url_has_no_plus_or_slash(self):
        raw = sender.build_mime("me@x.com", "you@y.com", "S" * 60, "B" * 400)
        self.assertNotIn("+", raw)
        self.assertNotIn("/", raw)

    def test_recipient_and_body_round_trip(self):
        raw = sender.build_mime("me@x.com", "you@y.com", "Hi", "Hello world")
        decoded = self._decode(raw)
        self.assertIn("To: you@y.com", decoded)
        self.assertIn("Hello world", decoded)


class LogFormatTests(unittest.TestCase):
    def test_line_has_iso8601_status_recipient_and_campaign(self):
        now = datetime(2026, 10, 9, 14, 22, 1).astimezone()
        line = sendlog.format_line(
            sendlog.SENT, "c_20261009_001", "john@acme.com", "John Smith", now=now
        )
        self.assertIn("2026-10-09T14:22:01", line)
        self.assertIn("SENT", line)
        self.assertIn("john@acme.com (John Smith)", line)
        self.assertIn("campaign: c_20261009_001", line)

    def test_failure_line_carries_the_error(self):
        line = sendlog.format_line(
            sendlog.FAILED, "c_1", "bob@inc.com", None, detail="error: HTTP 429"
        )
        self.assertIn("FAILED", line)
        self.assertIn("HTTP 429", line)

    def test_limit_reached_needs_no_recipient(self):
        line = sendlog.format_line(sendlog.LIMIT_REACHED, "c_1", detail="100/day reached")
        self.assertIn("LIMIT_REACHED", line)
        self.assertIn("campaign: c_1", line)

    def test_write_appends_to_a_dated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sendlog.write(sendlog.SENT, "c_1", "a@x.com", root=tmp, echo=False)
            sendlog.write(sendlog.SENT, "c_1", "b@x.com", root=tmp, echo=False)
            path = sendlog.log_path(root=tmp)
            self.assertEqual(len(path.read_text().strip().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
