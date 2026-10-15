"""`{{variable}}` substitution.

Deliberately not a template engine. The original tool did a regex replace of
`{{key}}` from a CSV row, and that is all a cold-email template needs: no loops,
no conditionals, no filters, nothing that can execute.
"""

import re

# A placeholder is {{ key }} where key is a column name. Whitespace inside the
# braces is tolerated because people type it. Anything else -- nested braces,
# an unclosed brace, an expression -- is not a placeholder and is left alone.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_\- ]+?)\s*\}\}")


def placeholders(template):
    """Every distinct column name referenced by the template, in first-seen order."""
    seen = {}
    for match in PLACEHOLDER.finditer(template):
        seen.setdefault(match.group(1), None)
    return list(seen)


def render(template, row):
    """Substitute `{{key}}` from `row`.

    Returns ``(text, missing)``. ``missing`` lists the placeholders that had no
    value, and the caller decides what to do about it -- the sender skips those
    recipients rather than mailing somebody "Hi ,". Keeping that decision out of
    here leaves this a pure function that is trivial to test.

    A value that is present but empty counts as missing: a blank cell in a CSV is
    not a personalisation, it is a hole.
    """
    missing = []

    def substitute(match):
        key = match.group(1)
        value = row.get(key)
        if value is None or str(value).strip() == "":
            if key not in missing:
                missing.append(key)
            return ""
        return str(value)

    return PLACEHOLDER.sub(substitute, template), missing
