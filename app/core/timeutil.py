"""Timezone normalization for datetimes read back from the database.

Exists because of a bug class that has now bitten three separate call sites: a
`DateTime(timezone=True)` column comes back **aware** from Postgres but **naive** from
SQLite (which has no native `timestamptz`), and arithmetic mixing the two raises
`TypeError: can't subtract offset-naive and offset-aware datetimes`.

That asymmetry means the failure is invisible in the unit suite (SQLite-backed) whenever
the code path is only exercised against Postgres, and invisible in production whenever
it's only exercised in tests — so it surfaces late and in whichever environment wasn't
being watched. `app/api/deps.py`'s API-key bookkeeping hit it, `SandboxService.
destroy_sandbox`'s billing arithmetic had it latent behind a feature flag, and the
destroy-time audit entry hit it again.

Assuming UTC for a naive value is correct throughout this codebase: every write to a
timestamp column goes through `datetime.now(UTC)`, and Postgres normalizes to UTC on
storage regardless. There is no path that persists a local-time naive value.
"""

from __future__ import annotations

from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime, tagging a naive one rather than converting.

    Naive input is *assumed* to already be UTC (see the module docstring) — it is stamped,
    not shifted, so this is a no-op for a value that came from `datetime.now(UTC)` and
    lost its tzinfo on a SQLite round trip.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def elapsed_seconds(since: datetime, until: datetime) -> float:
    """`until - since` in seconds, safe across the naive/aware boundary.

    Clamped at zero: a negative duration is always a clock or data problem (a backdated
    row, an NTP step), and letting it through produces negative billed usage or a negative
    lifetime in an audit entry — both worse than reporting zero.
    """
    return max(0.0, (ensure_utc(until) - ensure_utc(since)).total_seconds())
