"""
agent_config.py — persistent configuration for the autonomous trading agent.

A single SQLite row in `agent_config` (PRIMARY KEY = 1) holds every knob the
risk manager + live agent + EOD flattener read at runtime. Stored in the same
DB the rest of NuroQ uses (NUROQ_DB_PATH), so it survives restarts and the
Configuration view can edit it through the REST API.

Defaults are deliberately conservative for paper trading:
  • $10k budget (the "fixed amount" cap)
  • max 5 concurrent positions
  • 1% risk per trade   → ~$100 loss-at-SL on a $10k budget
  • 2% daily loss limit → halts trading on the day after ~$200 of red
  • Entry window 09:35–15:00 ET (avoids opening 5 min noise + last hour)
  • EOD flatten 15:50 ET (10 min before the close)
  • Margin OFF (cash-only buying power)
  • AUTO_TRADE OFF (explicit opt-in via the UI)
  • notify_on_trade ON (Telegram audit trail of every auto-fire)

A `halted_at` / `halt_reason` pair lets the daily-loss circuit and the
manual /api/auto-trade/halt endpoint persist the halt across restarts — the
agent reads them and refuses to enter new trades until cleared.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Optional

_TABLE = """
CREATE TABLE IF NOT EXISTS agent_config (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    budget                REAL    NOT NULL DEFAULT 10000,
    max_concurrent        INTEGER NOT NULL DEFAULT 5,
    risk_per_trade_pct    REAL    NOT NULL DEFAULT 1.0,
    daily_loss_limit_pct  REAL    NOT NULL DEFAULT 2.0,
    entry_window_start    TEXT    NOT NULL DEFAULT '09:35',
    entry_window_end      TEXT    NOT NULL DEFAULT '15:00',
    eod_flatten_time      TEXT    NOT NULL DEFAULT '15:50',
    margin_allowed        INTEGER NOT NULL DEFAULT 0,
    auto_trade_enabled    INTEGER NOT NULL DEFAULT 0,
    notify_on_trade       INTEGER NOT NULL DEFAULT 1,
    halted_at             INTEGER,
    halt_reason           TEXT,
    pending_open_flatten  INTEGER,   -- unix ts when off-hours flatten was queued
    section_475_election_filed INTEGER NOT NULL DEFAULT 0,  -- user acknowledged §475(f) on file
    -- Day-trader (intraday agent — separate from the swing crossing logic).
    -- Disabled by default. Promote: disabled → shadow → approve → auto.
    dt_mode                  TEXT    NOT NULL DEFAULT 'disabled',
    dt_max_concurrent        INTEGER NOT NULL DEFAULT 3,
    dt_risk_per_trade_pct    REAL    NOT NULL DEFAULT 0.5,
    dt_entry_window_end      TEXT    NOT NULL DEFAULT '14:30',
    dt_volume_multiplier     REAL    NOT NULL DEFAULT 2.0,
    dt_require_vwap          INTEGER NOT NULL DEFAULT 1,
    dt_time_stop_bars        INTEGER NOT NULL DEFAULT 30,
    dt_target_r_multiple     REAL    NOT NULL DEFAULT 2.0,
    dt_universe              TEXT    NOT NULL DEFAULT '',   -- comma-separated tickers; '' = auto/scanner
    updated_at            INTEGER NOT NULL
);
"""

# Tolerant column-add for old DBs (no full migration framework).
def _migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_config)").fetchall()}
    if "pending_open_flatten" not in cols:
        conn.execute("ALTER TABLE agent_config ADD COLUMN pending_open_flatten INTEGER")
    if "section_475_election_filed" not in cols:
        conn.execute(
            "ALTER TABLE agent_config "
            "ADD COLUMN section_475_election_filed INTEGER NOT NULL DEFAULT 0"
        )
    # Day-trader columns (added in Session 8 — keep this list extensible).
    dt_columns = [
        ("dt_mode",                  "TEXT NOT NULL DEFAULT 'disabled'"),
        ("dt_max_concurrent",        "INTEGER NOT NULL DEFAULT 3"),
        ("dt_risk_per_trade_pct",    "REAL NOT NULL DEFAULT 0.5"),
        ("dt_entry_window_end",      "TEXT NOT NULL DEFAULT '14:30'"),
        ("dt_volume_multiplier",     "REAL NOT NULL DEFAULT 2.0"),
        ("dt_require_vwap",          "INTEGER NOT NULL DEFAULT 1"),
        ("dt_time_stop_bars",        "INTEGER NOT NULL DEFAULT 30"),
        ("dt_target_r_multiple",     "REAL NOT NULL DEFAULT 2.0"),
        ("dt_universe",              "TEXT NOT NULL DEFAULT ''"),
    ]
    for col_name, col_def in dt_columns:
        if col_name not in cols:
            conn.execute(f"ALTER TABLE agent_config ADD COLUMN {col_name} {col_def}")

DEFAULTS: dict[str, Any] = {
    "budget":               10000.0,
    "max_concurrent":       5,
    "risk_per_trade_pct":   1.0,
    "daily_loss_limit_pct": 2.0,
    "entry_window_start":   "09:35",
    "entry_window_end":     "15:00",
    "eod_flatten_time":     "15:50",
    "margin_allowed":       False,
    "auto_trade_enabled":   False,
    "notify_on_trade":      True,
    "halted_at":            None,
    "halt_reason":          None,
    "section_475_election_filed": False,
    # Day-trader: ALWAYS default to disabled. Operators must explicitly
    # promote via the UI / /api/day-trader/config. Auto-deploy of new code
    # must never silently turn the day-trader on.
    "dt_mode":                  "disabled",
    "dt_max_concurrent":        3,
    "dt_risk_per_trade_pct":    0.5,
    "dt_entry_window_end":      "14:30",
    "dt_volume_multiplier":     2.0,
    "dt_require_vwap":          True,
    "dt_time_stop_bars":        30,
    "dt_target_r_multiple":     2.0,
    "dt_universe":              "",
}

# Whitelist of fields the /api/config update endpoint will accept.
EDITABLE_KEYS = {
    "budget", "max_concurrent", "risk_per_trade_pct", "daily_loss_limit_pct",
    "entry_window_start", "entry_window_end", "eod_flatten_time",
    "margin_allowed", "auto_trade_enabled", "notify_on_trade",
    "section_475_election_filed",
    # Day-trader knobs — separate /api/day-trader/config endpoint also uses
    # this whitelist, so adding here is enough to make a field editable.
    "dt_mode", "dt_max_concurrent", "dt_risk_per_trade_pct",
    "dt_entry_window_end", "dt_volume_multiplier", "dt_require_vwap",
    "dt_time_stop_bars", "dt_target_r_multiple", "dt_universe",
}


def _db_path() -> str:
    return os.environ.get("NUROQ_DB_PATH", "nuroq.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.execute(_TABLE)
    _migrate(c)
    return c


def request_open_flatten() -> None:
    """Mark that a flatten should run as soon as the market opens. Used when
    the user clicks 'Clean Slate' outside hours and Alpaca's pending_cancel
    bracket-lock prevents the SELLs from queueing tonight. The EOD daemon
    polls this flag and fires once at next market open."""
    with _conn() as c:
        _seed_if_empty(c)
        c.execute(
            "UPDATE agent_config SET pending_open_flatten=?, updated_at=? WHERE id=1",
            (int(time.time()), int(time.time())),
        )


def clear_open_flatten() -> None:
    with _conn() as c:
        _seed_if_empty(c)
        c.execute(
            "UPDATE agent_config SET pending_open_flatten=NULL, updated_at=? WHERE id=1",
            (int(time.time()),),
        )


def _seed_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM agent_config WHERE id=1").fetchone():
        return
    cols = list(DEFAULTS.keys()) + ["updated_at"]
    placeholders = ",".join("?" * len(cols))
    vals = [_coerce_store(DEFAULTS[k]) for k in DEFAULTS] + [int(time.time())]
    conn.execute(
        f"INSERT INTO agent_config (id, {','.join(cols)}) "
        f"VALUES (1, {placeholders})",
        vals,
    )


def _coerce_store(v: Any) -> Any:
    """SQLite-friendly coercion: bool → 0/1; everything else passes through."""
    if isinstance(v, bool):
        return 1 if v else 0
    return v


def _coerce_load(key: str, v: Any) -> Any:
    """DB → Python: 0/1 ints for known bool columns become True/False."""
    bool_keys = {"margin_allowed", "auto_trade_enabled", "notify_on_trade",
                 "section_475_election_filed", "dt_require_vwap"}
    if key in bool_keys:
        return bool(v) if v is not None else False
    return v


_GET_COLUMNS = [
    "budget", "max_concurrent", "risk_per_trade_pct", "daily_loss_limit_pct",
    "entry_window_start", "entry_window_end", "eod_flatten_time",
    "margin_allowed", "auto_trade_enabled", "notify_on_trade",
    "halted_at", "halt_reason", "pending_open_flatten",
    "section_475_election_filed",
    "dt_mode", "dt_max_concurrent", "dt_risk_per_trade_pct",
    "dt_entry_window_end", "dt_volume_multiplier", "dt_require_vwap",
    "dt_time_stop_bars", "dt_target_r_multiple", "dt_universe",
    "updated_at",
]


def get() -> dict:
    """Return the current config as a dict (with sane defaults if row missing)."""
    with _conn() as c:
        _seed_if_empty(c)
        row = c.execute(
            f"SELECT {','.join(_GET_COLUMNS)} FROM agent_config WHERE id=1"
        ).fetchone()
    return {k: _coerce_load(k, v) for k, v in zip(_GET_COLUMNS, row)}


def update(**kwargs) -> dict:
    """Update one or more fields. Unknown keys silently rejected (whitelist).
    Returns the post-update config dict."""
    sets, vals = [], []
    for k, v in kwargs.items():
        if k not in EDITABLE_KEYS:
            continue
        sets.append(f"{k}=?")
        vals.append(_coerce_store(v))
    if sets:
        sets.append("updated_at=?")
        vals.append(int(time.time()))
        with _conn() as c:
            _seed_if_empty(c)
            c.execute(f"UPDATE agent_config SET {','.join(sets)} WHERE id=1", vals)
    return get()


def halt(reason: str) -> dict:
    """Hard-halt auto trading. Persists across restarts. Resume via clear_halt()."""
    with _conn() as c:
        _seed_if_empty(c)
        c.execute(
            "UPDATE agent_config SET halted_at=?, halt_reason=?, "
            "auto_trade_enabled=0, updated_at=? WHERE id=1",
            (int(time.time()), reason[:240], int(time.time())),
        )
    return get()


def clear_halt() -> dict:
    """Clear a halt. Does NOT re-enable auto_trade — that's a separate user
    action (so a stale halt → resume doesn't surprise-start trading)."""
    with _conn() as c:
        _seed_if_empty(c)
        c.execute(
            "UPDATE agent_config SET halted_at=NULL, halt_reason=NULL, "
            "updated_at=? WHERE id=1",
            (int(time.time()),),
        )
    return get()


def is_halted() -> tuple[bool, Optional[str]]:
    cfg = get()
    return (cfg["halted_at"] is not None, cfg.get("halt_reason"))
