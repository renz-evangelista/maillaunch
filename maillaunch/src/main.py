"""CLI entry point: auth, send, status, resume, log.

argparse rather than click or typer -- six commands with a handful of flags each
does not need a dependency, and the brief asks for a minimal one.
"""

import argparse
import sys
import time
from pathlib import Path

from . import auth, config as config_mod, recipients, sendlog, sender, store, template, tokens

PROVIDERS = ("gmail", "microsoft")


class Abort(Exception):
    """A user-facing error: printed without a traceback, exits non-zero."""


# --------------------------------------------------------------------------- #
# send / resume
# --------------------------------------------------------------------------- #

def _provider_credentials(cfg, provider):
    client_id, secret = config_mod.oauth_client(cfg, provider)
    tenant = (cfg.get("microsoft") or {}).get("tenant", "common")
    return client_id, secret, tenant


def _authenticate(cfg, provider):
    """Get a usable access token, or explain exactly what is missing.

    Called before a campaign is written to the database, so a credentials
    problem cannot leave a phantom campaign behind for `resume` to find.

    The "not authenticated" check comes first on purpose: for someone who has
    never run `auth`, that is the actionable message, not a complaint about an
    environment variable they would only need afterwards.
    """
    if not tokens.load(provider):
        raise Abort(
            f"Not authenticated with {provider}. Run `maillaunch auth {provider}` first."
        )
    client_id, secret, _ = _provider_credentials(cfg, provider)
    return auth.access_token(provider, client_id, secret)


def _run_campaign(connection, cfg, campaign, args):
    """The send loop, shared by `send` and `resume`.

    Order of checks per recipient, and why:
      1. daily limit   -- stop before doing any work we would have to undo
      2. render        -- a missing {{variable}} skips the recipient, it never
                          sends a half-personalised email
      3. send + retry  -- transient failures back off; a 401 refreshes once
    """
    campaign_id = campaign["id"]
    provider = campaign["provider"]
    send_cfg = cfg["send"]
    limit = cfg["daily_limit"]
    dry_run = args.dry_run

    queue = store.pending(connection, campaign_id)
    if not queue:
        print(f"Campaign {campaign_id} has nothing pending.")
        store.set_campaign_status(connection, campaign_id, store.DONE)
        return 0

    token = None if dry_run else _authenticate(cfg, provider)

    print(
        f"Campaign {campaign_id}: {len(queue)} pending, "
        f"{store.sent_today(connection)}/{limit} sent today"
        + (" [DRY RUN - nothing will be sent]" if dry_run else "")
    )

    attempts_allowed = send_cfg["retry_attempts"] if send_cfg["retry_failed"] else 0
    first = True

    for item in queue:
        if store.sent_today(connection) >= limit:
            sendlog.write(
                sendlog.LIMIT_REACHED,
                campaign_id,
                detail=f"{limit}/day reached; run `maillaunch resume` tomorrow",
            )
            store.set_campaign_status(connection, campaign_id, store.PAUSED)
            return 0

        subject, missing_subject = template.render(campaign["subject"], item["data"])
        body, missing_body = template.render(campaign["body"], item["data"])
        missing = list(dict.fromkeys(missing_subject + missing_body))
        if missing:
            reason = f"missing template values: {', '.join(missing)}"
            store.mark(connection, campaign_id, item["row"], store.SKIPPED, error=reason)
            sendlog.write(
                sendlog.SKIPPED, campaign_id, item["email"], item["name"], detail=reason
            )
            continue

        # The delay goes *between* sends, so the first email leaves immediately.
        if not first:
            pause = sender.random_delay(
                send_cfg["min_delay_seconds"], send_cfg["max_delay_seconds"]
            )
            if dry_run:
                print(f"  (dry run: would wait {pause:.0f}s)")
            else:
                time.sleep(pause)
        first = False

        if dry_run:
            store.mark(connection, campaign_id, item["row"], store.SENT, attempts=1)
            sendlog.write(
                sendlog.SENT, campaign_id, item["email"], item["name"], detail="dry run"
            )
            continue

        send_fn = sender.send_gmail if provider == "gmail" else sender.send_microsoft

        def attempt_send(current_token=None):
            send_fn(current_token or token, item["email"], subject, body)

        def on_retry(n, total, delay, exc):
            sendlog.write(
                sendlog.RETRY,
                campaign_id,
                item["email"],
                item["name"],
                detail=f"attempt {n}/{total} in {delay:.0f}s | {exc}",
            )

        try:
            try:
                used = sender.send_with_retry(
                    attempt_send,
                    attempts_allowed,
                    on_retry=on_retry,
                    base=send_cfg["retry_backoff_seconds"],
                )
            except sender.AuthExpired:
                # One refresh, then one more try. If that fails, stop the whole
                # campaign: every subsequent send would fail the same way.
                sendlog.write(
                    sendlog.RETRY,
                    campaign_id,
                    item["email"],
                    item["name"],
                    detail="401 - refreshing access token",
                )
                client_id, secret, _ = _provider_credentials(cfg, provider)
                token = auth.access_token(provider, client_id, secret, force=True)
                used = sender.send_with_retry(
                    attempt_send,
                    attempts_allowed,
                    on_retry=on_retry,
                    base=send_cfg["retry_backoff_seconds"],
                )
        except sender.AuthExpired as exc:
            store.set_campaign_status(connection, campaign_id, store.PAUSED)
            raise Abort(
                f"Authentication failed after refresh: {exc}\n"
                f"Run `maillaunch auth {provider}`, then `maillaunch resume`."
            ) from None
        except sender.SendFailed as exc:
            store.mark(
                connection,
                campaign_id,
                item["row"],
                store.FAILED,
                error=str(exc),
                attempts=attempts_allowed + 1,
            )
            sendlog.write(
                sendlog.FAILED, campaign_id, item["email"], item["name"], detail=f"error: {exc}"
            )
            continue
        except auth.AuthError as exc:
            store.set_campaign_status(connection, campaign_id, store.PAUSED)
            raise Abort(str(exc)) from None

        store.mark(connection, campaign_id, item["row"], store.SENT, attempts=used + 1)
        sendlog.write(sendlog.SENT, campaign_id, item["email"], item["name"])

    store.set_campaign_status(connection, campaign_id, store.DONE)
    _print_summary(connection, campaign_id)
    return 0


def _print_summary(connection, campaign_id):
    tally = store.counts(connection, campaign_id)
    parts = [f"{status.lower()}: {count}" for status, count in sorted(tally.items())]
    print(f"\nCampaign {campaign_id} finished -- {', '.join(parts)}")


def cmd_send(args, cfg, connection):
    provider = args.provider or cfg["provider"]
    if provider not in PROVIDERS:
        raise Abort(f"--provider must be one of {', '.join(PROVIDERS)}.")

    body_path = Path(args.body_file)
    if not body_path.is_file():
        raise Abort(f"Body file not found: {body_path}")
    body = body_path.read_text(encoding="utf-8")

    try:
        rows, column = recipients.load(args.csv, args.email_column)
    except recipients.RecipientError as exc:
        raise Abort(str(exc)) from None

    # Fail before sending anything if the template references a column the CSV
    # does not have -- that is a typo in the template, not a per-recipient issue.
    needed = set(template.placeholders(args.subject)) | set(template.placeholders(body))
    available = set(rows[0]["data"])
    unknown = sorted(needed - available)
    if unknown:
        raise Abort(
            f"Template uses {', '.join('{{' + u + '}}' for u in unknown)}, "
            f"which the CSV does not have. Columns: {', '.join(sorted(available))}."
        )

    if args.min_delay is not None:
        cfg["send"]["min_delay_seconds"] = args.min_delay
    if args.max_delay is not None:
        cfg["send"]["max_delay_seconds"] = args.max_delay
    if cfg["send"]["min_delay_seconds"] > cfg["send"]["max_delay_seconds"]:
        raise Abort("--min-delay cannot be greater than --max-delay.")

    # Fail before any state is written: a credentials problem should not leave a
    # half-created campaign that `resume` would later offer to continue.
    if not args.dry_run:
        _authenticate(cfg, provider)

    campaign_id = store.new_campaign_id(connection)
    store.create_campaign(
        connection, campaign_id, provider, args.subject, body, args.csv, rows
    )

    skipped = [r for r in rows if r["skip_reason"]]
    for row in skipped:
        sendlog.write(
            sendlog.SKIPPED, campaign_id, row["email"] or f"row {row['row']}",
            row["name"], detail=row["skip_reason"],
        )
    print(
        f"Loaded {len(rows)} rows from {args.csv} (email column: {column!r}); "
        f"{len(rows) - len(skipped)} sendable, {len(skipped)} skipped."
    )
    return _run_campaign(connection, cfg, store.get_campaign(connection, campaign_id), args)


def cmd_resume(args, cfg, connection):
    campaign = (
        store.get_campaign(connection, args.campaign)
        if args.campaign
        else store.latest_resumable(connection)
    )
    if not campaign:
        raise Abort(
            f"No campaign {args.campaign!r} found."
            if args.campaign
            else "No interrupted campaign to resume. `maillaunch status` lists what exists."
        )
    store.set_campaign_status(connection, campaign["id"], store.RUNNING)
    return _run_campaign(connection, cfg, campaign, args)


# --------------------------------------------------------------------------- #
# auth / status / log
# --------------------------------------------------------------------------- #

def cmd_auth(args, cfg, connection):
    client_id, secret, tenant = _provider_credentials(cfg, args.provider)
    where = auth.authorize(args.provider, client_id, secret, tenant)
    print(f"Authenticated with {args.provider}. Tokens stored in: {where}")
    return 0


def cmd_status(args, cfg, connection):
    limit = cfg["daily_limit"]
    used = store.sent_today(connection)
    print(f"Sent today:  {used}/{limit}   ({max(limit - used, 0)} remaining)")

    for provider in PROVIDERS:
        state = "authenticated" if tokens.load(provider) else "not authenticated"
        marker = " (default)" if provider == cfg["provider"] else ""
        print(f"  {provider:<10} {state}{marker}")

    rows = connection.execute(
        "SELECT * FROM campaigns ORDER BY created_at DESC, id DESC LIMIT 5"
    ).fetchall()
    if not rows:
        print("\nNo campaigns yet.")
        return 0

    print("\nRecent campaigns:")
    for row in rows:
        tally = store.counts(connection, row["id"])
        summary = ", ".join(f"{k.lower()}: {v}" for k, v in sorted(tally.items()))
        print(f"  {row['id']}  {row['status']:<7} {row['provider']:<10} {summary}")
        if row["status"] == store.PAUSED:
            print(f"      resume with: maillaunch resume --campaign {row['id']}")
    return 0


def cmd_log(args, cfg, connection):
    text = sendlog.read()
    if not text:
        print("No sends logged today.")
        return 0
    print(text, end="")
    return 0


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #

def build_parser():
    parser = argparse.ArgumentParser(
        prog="maillaunch", description="Send personalised cold email from a CSV."
    )
    parser.add_argument("--config", help="Path to maillaunch.config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_auth = subparsers.add_parser("auth", help="Authenticate with a provider")
    p_auth.add_argument("provider", choices=PROVIDERS)
    p_auth.set_defaults(func=cmd_auth)

    p_send = subparsers.add_parser("send", help="Start a send campaign")
    p_send.add_argument("--provider", choices=PROVIDERS)
    p_send.add_argument("--csv", required=True, help="Recipient CSV")
    p_send.add_argument("--subject", required=True, help="Subject, may use {{column}}")
    p_send.add_argument("--body-file", required=True, help="Body template file")
    p_send.add_argument("--min-delay", type=float, help="Override send.min_delay_seconds")
    p_send.add_argument("--max-delay", type=float, help="Override send.max_delay_seconds")
    p_send.add_argument("--email-column", help="Name the email column instead of detecting it")
    p_send.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk the whole campaign without calling the provider or waiting",
    )
    p_send.set_defaults(func=cmd_send)

    p_resume = subparsers.add_parser("resume", help="Resume an interrupted campaign")
    p_resume.add_argument("--campaign", help="Campaign id (default: the most recent unfinished)")
    p_resume.add_argument("--dry-run", action="store_true")
    p_resume.set_defaults(func=cmd_resume)

    p_status = subparsers.add_parser("status", help="Today's send count and queue status")
    p_status.set_defaults(func=cmd_status)

    p_log = subparsers.add_parser("log", help="Print today's send log")
    p_log.set_defaults(func=cmd_log)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    connection = None
    try:
        cfg = config_mod.load(args.config)
        connection = store.connect()
        return args.func(args, cfg, connection)
    except KeyboardInterrupt:
        # Every send is committed as it happens, so there is nothing to flush --
        # just tell the user how to pick the campaign back up.
        print("\nInterrupted. Run `maillaunch resume` to continue where this stopped.")
        return 130
    except (Abort, config_mod.ConfigError, auth.AuthError, tokens.TokenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
