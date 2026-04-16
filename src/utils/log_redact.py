"""Redact sensitive identifiers from log output.

Capital API log payloads embed the futures account ID (``F020...``) and
client/order ID (``L124...``) in multiple fields. Naively logging the
raw payloads leaks these to the debug log, which users sometimes share
when reporting issues.

This module provides:

- :func:`redact_acct` — fixed redaction for a known-single ID string
  (returns ``***9366`` keeping the last 4 digits).
- :func:`scrub_ids` — regex-based scrubber that masks any L-prefix or
  F-prefix long numeric token anywhere in a string. Used on raw
  broker payload blobs.
- :func:`redact_open_interest` — structured redaction of the
  ``TF,<acct>,<product>,...`` OpenInterest payload.
- :func:`redact_future_rights` — structured redaction of the
  ``,...,<client>,<acct>`` FutureRights payload (account at the end).

All functions return a new string; inputs are never mutated.
"""

from __future__ import annotations

import re


# L-prefix = client/order ID (e.g. "L3333344444")
# F-prefix = futures account ID (e.g. "F1111111112222")
# Require >= 5 trailing digits to avoid matching words like "LIVE" or
# tickers. Anchored to word boundaries so substrings inside other tokens
# are left alone.
_ID_LEAK_RE = re.compile(r"\b([LF])\d{5,}\b")


def redact_acct(s: str | None) -> str:
    """Redact a known identifier string, keeping only the last 4 digits.

    Examples:
        "F1111111112222" -> "***9366"
        "A12345"         -> "***2345"
        "abc"            -> "***" (too short to keep any)
        ""               -> ""
    """
    if not s:
        return ""
    s = str(s)
    if len(s) <= 4:
        return "***"
    return f"***{s[-4:]}"


def scrub_ids(text: str | None) -> str:
    """Mask any L- or F-prefix long numeric IDs anywhere in ``text``.

    Keeps the prefix + last 4 digits for correlation (e.g.
    ``L3333344444`` -> ``L***3388``).  Shorter matches (6-5 digits total)
    become ``***``.
    """
    if not text or not isinstance(text, str):
        return str(text) if text is not None else ""

    def _mask(m: re.Match[str]) -> str:
        token = m.group(0)
        # token is "L" or "F" + >=5 digits, so always length >= 6
        return f"{token[0]}***{token[-4:]}"

    return _ID_LEAK_RE.sub(_mask, text)


def redact_open_interest(data: str | None) -> str:
    """Redact IDs in raw OnOpenInterest callback data.

    Format: ``"TF,F1111111112222,TM04,B,1,...,L3333344444"``. The account
    is at field 1; the client ID can appear in a later field. Uses
    structured redaction for the account (reliable) and the regex
    scrubber for anywhere else.
    """
    if not data or not isinstance(data, str):
        return str(data) if data is not None else ""
    parts = data.split(",")
    if len(parts) >= 2 and parts[0] in ("TF", "TS"):
        parts[1] = redact_acct(parts[1])
        data = ",".join(parts)
    return scrub_ids(data)


def redact_future_rights(data: str | None) -> str:
    """Redact IDs in raw OnFutureRights callback data.

    Format is ``,``-delimited with the client ID and futures account
    as the last two fields. Example tail:
    ``...0,L3333344444,F1111111112222``. Uses ``scrub_ids`` so field
    positions don't matter — both IDs get masked wherever they are.
    """
    if not data or not isinstance(data, str):
        return str(data) if data is not None else ""
    return scrub_ids(data)
