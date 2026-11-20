"""Attribute the account's lots on one product to the bots' own position logs.

Several real bots can share one Capital account and one product. The
OpenInterest book only shows the account's lots, never whose they are.
Every bot keeps its own position in ``<bot dir>/position_log.jsonl``
(#250), built from fills of the orders it sent. A lot is accounted for
when the account's open rows on the product equal the logged positions
over every bot dir under the same base dir: one open row by that signed
sum (a netted book still matches opposite logs); more than one open row
only when its long qty and short qty equal the positive and negative
logged qtys.

One rule for deploy, resume and the dashboard switch to auto, used only
for bots in ``trading.shared_book_opt_in``: when every lot is accounted
for, the account's other lots belong to other bots and are not this
bot's business. Anything that cannot be accounted for (another product,
an options row, an unreadable log, a mismatch) answers "not accounted"
and the caller keeps master's behaviour (refuse / manual review).

Pure functions. Reads files only; never writes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

from src.live.account_monitor import oi_product_match
from src.live.position_log import replay_position


def fleet_positions(base_dir: str, prefix: str, *, own_name: str = "",
                    account: str = "",
                    alive: Callable[[str], bool] | None = None,
                    opener: Callable | None = None) -> dict[str, int] | None:
    """Own logged position of every bot dir under ``base_dir`` on ``prefix``.

    Keys are bot dir names; flat bots are left out. A sibling's lot counts
    only while that bot is running (``alive(bot_dir)``): a stopped or
    crashed bot's journal is either stale (its stop-close already flattened
    the lot) or names a lot nobody manages, and neither is "accounted for".
    ``own_name`` (the deploying / resuming bot) is never filtered by
    ``alive``. A journal written for another ``account`` is left out; a
    journal with no account field is kept (pre-#250 lines).

    ``None`` when the base dir cannot be listed, a counted journal cannot
    be read, or a counted bot with a non-zero position names no product —
    the whole answer is unknown then.
    """
    if not prefix:
        return None
    try:
        names = sorted(os.listdir(base_dir))
    except OSError:
        return None
    out: dict[str, int] = {}
    for name in names:
        path = os.path.join(base_dir, name)
        if not os.path.isdir(path):
            continue
        if name != own_name and alive is not None and not alive(path):
            continue
        got = replay_position(path, opener=opener)
        if got is None:
            return None
        position, product, acct = got
        if position == 0:
            continue
        if account and acct and acct != account:
            continue
        if not product:
            return None
        if str(product).startswith(prefix):
            out[name] = position
    return out


def _open_book(rows: Iterable[dict], prefix: str, market: str
               ) -> tuple[int, int, int] | None:
    """``(long_qty, short_qty, n_open)`` on this product, or None.

    ``None`` when any open row is not a usable B/S lot on this product
    (another product, an options row, a bad side or qty). Qty 0 is not
    an open row.
    """
    long_qty = 0
    short_qty = 0
    n_open = 0
    for row in rows or ():
        if not isinstance(row, dict):
            return None
        try:
            qty = int(row.get("qty", 0) or 0)
        except (TypeError, ValueError):
            return None
        if qty == 0:
            continue
        if not oi_product_match(row, prefix, market):
            return None
        side = row.get("side")
        if side == "B":
            long_qty += qty
        elif side == "S":
            short_qty += qty
        else:
            return None
        n_open += 1
    return long_qty, short_qty, n_open


def account_lots(rows: Iterable[dict], prefix: str, market: str) -> int | None:
    """Signed sum of the account's open rows for this product and market.

    ``None`` when any other open row exists (another product, an options
    row, a row with no usable side or qty): those lots are no bot's here.
    Every matching row counts, including split B/S rows. ``attribute``
    keeps this sum for a single open row.
    """
    book = _open_book(rows, prefix, market)
    if book is None:
        return None
    long_qty, short_qty, _n_open = book
    return long_qty - short_qty


@dataclass(frozen=True)
class Attribution:
    ok: bool
    reason: str
    account: int | None = None
    own: int = 0
    others: dict[str, int] = field(default_factory=dict)


def attribute(rows: Iterable[dict], prefix: str, market: str,
              positions: dict[str, int] | None, own_name: str, *,
              own_may_hold: bool) -> Attribution:
    """Is every lot on the account accounted for by the bots' own logs?

    One open row is the signed sum, so a netted book still matches
    opposite logs. More than one open row on the product matches only
    when the book's long qty and short qty equal the positive and
    negative logged qtys.

    ``own_may_hold`` is False when this bot must be flat to proceed (a new
    deploy, a switch from paper to auto): then any position in its own
    log is refused, because this process would not manage that lot.
    """
    if positions is None:
        return Attribution(False, "bot position logs unreadable")
    book = _open_book(rows, prefix, market)
    if book is None:
        return Attribution(False, "account holds another product or row")
    long_qty, short_qty, n_open = book
    lots = long_qty - short_qty
    own = int(positions.get(own_name, 0))
    others = {k: v for k, v in positions.items() if k != own_name}
    if own and not own_may_hold:
        return Attribution(False, f"own log still shows {own:+d}",
                           lots, own, others)
    total = own + sum(others.values())
    if n_open > 1:
        logged = (own, *others.values())
        logged_long = sum(v for v in logged if v > 0)
        logged_short = -sum(v for v in logged if v < 0)
        if long_qty != logged_long or short_qty != logged_short:
            return Attribution(
                False,
                f"account long {long_qty} short {short_qty} != "
                f"logs long {logged_long} short {logged_short}",
                lots, own, others)
    elif lots != total:
        return Attribution(
            False, f"account {lots:+d} != bots' logs {total:+d}",
            lots, own, others)
    named = ", ".join(f"{k} {v:+d}" for k, v in sorted(others.items()))
    return Attribution(
        True,
        f"account {lots:+d} = own {own:+d}" + (f" + {named}" if named else ""),
        lots, own, others)
