"""Attribute the account's lots on one product to the bots' own position logs.

Several real bots can share one Capital account and one product. The
OpenInterest book only shows the account's lots, never whose they are.
Every bot keeps its own position in ``<bot dir>/position_log.jsonl``
(#250), built from fills of the orders it sent. A lot is accounted for
when the account's lots on the product equal the sum of those logged
positions over every bot dir under the same base dir.

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


def account_lots(rows: Iterable[dict], prefix: str, market: str) -> int | None:
    """Signed sum of the account's open rows for this product and market.

    ``None`` when any other open row exists (another product, an options
    row, a row with no usable side or qty): those lots are no bot's here.
    Every matching row counts, so split B/S rows add up and a repeated
    row shows up as a mismatch, never as a match.
    """
    total = 0
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
            total += qty
        elif side == "S":
            total -= qty
        else:
            return None
    return total


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

    ``own_may_hold`` is False when this bot must be flat to proceed (a new
    deploy, a switch from paper to auto): then any position in its own
    log is refused, because this process would not manage that lot.
    """
    if positions is None:
        return Attribution(False, "bot position logs unreadable")
    lots = account_lots(rows, prefix, market)
    if lots is None:
        return Attribution(False, "account holds another product or row")
    own = int(positions.get(own_name, 0))
    others = {k: v for k, v in positions.items() if k != own_name}
    if own and not own_may_hold:
        return Attribution(False, f"own log still shows {own:+d}",
                           lots, own, others)
    total = own + sum(others.values())
    if lots != total:
        return Attribution(
            False, f"account {lots:+d} != bots' logs {total:+d}",
            lots, own, others)
    named = ", ".join(f"{k} {v:+d}" for k, v in sorted(others.items()))
    return Attribution(
        True,
        f"account {lots:+d} = own {own:+d}" + (f" + {named}" if named else ""),
        lots, own, others)
