"""Reading the recipient CSV.

Three things bite when parsing a CSV somebody exported from a spreadsheet:

  - a UTF-8 BOM, which turns the first header into "﻿email" and makes every
    lookup of "email" miss
  - quoted fields containing commas, which csv handles but a naive split does not
  - blank rows, which a spreadsheet leaves at the end of the file
"""

import csv
import io
import re

# Matched against lower-cased header names. "email" wins over "mail" so that a
# file with both `email` and `mailing_list` picks the right one regardless of
# column order.
EMAIL_HINTS = ("email", "e-mail", "mail")

# Good enough to catch typos and empty cells. Deliberately not RFC 5322: that
# grammar accepts addresses no mail server will take, and rejecting a valid
# address is worse than letting the API reject an invalid one.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RecipientError(Exception):
    """Raised for problems the user has to fix in their CSV or flags."""


def detect_email_column(fieldnames):
    """Pick the column holding email addresses.

    Prefers an exact 'email', then any header containing 'email'/'e-mail', then
    any containing 'mail'. Raises if nothing matches, listing what was available
    so the fix is obvious.
    """
    if not fieldnames:
        raise RecipientError("The CSV has no header row.")

    headers = [(name, (name or "").strip().lower()) for name in fieldnames]

    for name, lowered in headers:
        if lowered == "email":
            return name
    for hint in EMAIL_HINTS:
        for name, lowered in headers:
            if hint in lowered:
                return name

    raise RecipientError(
        "No email column found. Looked for a header containing 'email' or 'mail'. "
        f"Columns present: {', '.join(repr(n) for n in fieldnames)}. "
        "Pass --email-column to name it explicitly."
    )


def detect_name_column(fieldnames):
    """Best-effort display name, used only to make the log readable."""
    for candidate in ("name", "full_name", "fullname", "first_name", "firstname"):
        for name in fieldnames or []:
            if (name or "").strip().lower() == candidate:
                return name
    return None


def load(path, email_column=None):
    """Parse the CSV into a list of recipient dicts.

    Each is ``{"row": <1-based row number>, "email": ..., "name": ..., "data": {...}}``
    where ``data`` is the full row, so a template can reference any column.

    Rows that are blank or have an unusable address are returned with
    ``skip_reason`` set rather than dropped. The caller logs them as SKIPPED,
    which means a 500-row CSV that yields 480 sends can account for the other 20.
    """
    # Decode the whole file first so every decoding failure -- header or body --
    # surfaces here as one clear message rather than from inside csv's iterator.
    # A cold-email list is thousands of rows at most, so holding it is fine.
    try:
        # utf-8-sig strips the BOM if present and behaves like utf-8 if not.
        with open(path, newline="", encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise RecipientError(f"Cannot read {path}: {exc.strerror}.") from None
    except UnicodeDecodeError as exc:
        raise RecipientError(
            f"{path} is not valid UTF-8 (byte {exc.object[exc.start]:#04x} at "
            f"position {exc.start}). Re-export it as UTF-8."
        ) from None

    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames

    if email_column and email_column not in (fieldnames or []):
        raise RecipientError(
            f"--email-column {email_column!r} is not in the CSV. "
            f"Columns present: {', '.join(repr(n) for n in fieldnames or [])}."
        )
    column = email_column or detect_email_column(fieldnames)
    name_column = detect_name_column(fieldnames)

    out = []
    seen = set()
    for index, raw in enumerate(reader, start=1):
        # DictReader yields None keys for extra columns; drop them so the
        # template never sees a key it cannot name.
        row = {k: (v or "") for k, v in raw.items() if k is not None}

        if not any(value.strip() for value in row.values()):
            continue  # entirely blank line, usually trailing

        email = row.get(column, "").strip()
        name = (row.get(name_column, "").strip() if name_column else "") or None

        skip_reason = None
        if not email:
            skip_reason = f"no value in column {column!r}"
        elif not EMAIL_RE.match(email):
            skip_reason = f"not a valid email address: {email!r}"
        elif email.lower() in seen:
            skip_reason = "duplicate address earlier in this CSV"
        else:
            seen.add(email.lower())

        out.append(
            {
                "row": index,
                "email": email,
                "name": name,
                "data": row,
                "skip_reason": skip_reason,
            }
        )

    if not out:
        raise RecipientError(f"No rows found in {path}.")
    return out, column
