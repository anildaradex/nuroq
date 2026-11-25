"""SQLite persistence for the open-bell pipeline: runs, the daily eligible set, picks, state."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS open_bell_runs (
    run_date     TEXT PRIMARY KEY,      -- date of the daily bars the run used
    regime_on    INTEGER NOT NULL,
    regime_ratio REAL,
    n_candidates INTEGER, n_eligible INTEGER, n_buy INTEGER,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS open_bell_eligible (
    run_date TEXT NOT NULL, ticker TEXT NOT NULL,
    rank INTEGER, score INTEGER, rating TEXT,
    score_no_ai INTEGER, rating_no_ai TEXT,
    sector TEXT, breakdown TEXT, features TEXT, ai TEXT,
    fwd_1 REAL, fwd_5 REAL, fwd_10 REAL,
    PRIMARY KEY (run_date, ticker)
);

CREATE TABLE IF NOT EXISTS open_bell_picks (
    run_date TEXT NOT NULL, ticker TEXT NOT NULL,
    rank INTEGER, score INTEGER, sector TEXT,
    atr REAL, prev_close REAL, prev_low REAL, pullback_low REAL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | notified | skipped
    skip_reason TEXT, checked_at REAL,
    day_open REAL, bid REAL, ask REAL, mid REAL, quote_age_s REAL,
    gap_atr REAL, spread_pct REAL,
    ref_entry REAL, stop REAL, target REAL, shares INTEGER, risk_dollars REAL,
    entry_date TEXT, fwd_1 REAL, fwd_5 REAL, fwd_10 REAL,
    exit_date TEXT, exit_reason TEXT, r_multiple REAL, mae_r REAL, mfe_r REAL,
    PRIMARY KEY (run_date, ticker)
);
"""


def db_path() -> str:
    return os.getenv("NUROQ_DB_PATH", "nuroq.db")


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def get_state(key: str, default=None, path: Optional[str] = None):
    with connect(path) as c:
        row = c.execute("SELECT value FROM pipeline_state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value, path: Optional[str] = None) -> None:
    with connect(path) as c:
        c.execute("INSERT OR REPLACE INTO pipeline_state (key, value) VALUES (?, ?)",
                  (key, json.dumps(value)))


def save_run(run_date: str, regime: dict, n_candidates: int, eligible_rows: list,
             pick_rows: list, path: Optional[str] = None) -> None:
    """Replaces everything stored for run_date."""
    with connect(path) as c:
        for t in ("open_bell_runs", "open_bell_eligible", "open_bell_picks"):
            c.execute(f"DELETE FROM {t} WHERE run_date=?", (run_date,))
        c.execute(
            "INSERT INTO open_bell_runs VALUES (?,?,?,?,?,?,?)",
            (run_date, int(regime["on"]), regime.get("ratio"), n_candidates,
             len(eligible_rows), len(pick_rows), time.time()),
        )
        c.executemany(
            "INSERT INTO open_bell_eligible (run_date, ticker, rank, score, rating, "
            "score_no_ai, rating_no_ai, sector, breakdown, features, ai) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_date, r["ticker"], r["rank"], r["score"], r["rating"], r["score_no_ai"],
              r["rating_no_ai"], r.get("sector"), json.dumps(r["breakdown"]),
              json.dumps(r["features"]), json.dumps(r.get("ai"))) for r in eligible_rows],
        )
        c.executemany(
            "INSERT INTO open_bell_picks (run_date, ticker, rank, score, sector, atr, "
            "prev_close, prev_low, pullback_low) VALUES (?,?,?,?,?,?,?,?,?)",
            [(run_date, r["ticker"], r["rank"], r["score"], r.get("sector"),
              r["features"]["atr"], r["features"]["close"], r["features"]["day_low"],
              r["features"]["pullback_low"]) for r in pick_rows],
        )


def latest_run(path: Optional[str] = None) -> Optional[dict]:
    with connect(path) as c:
        row = c.execute("SELECT * FROM open_bell_runs ORDER BY run_date DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def picks(run_date: str, path: Optional[str] = None) -> list:
    with connect(path) as c:
        rows = c.execute("SELECT * FROM open_bell_picks WHERE run_date=? ORDER BY rank",
                         (run_date,)).fetchall()
    return [dict(r) for r in rows]


def eligible(run_date: str, path: Optional[str] = None) -> list:
    with connect(path) as c:
        rows = c.execute("SELECT * FROM open_bell_eligible WHERE run_date=? ORDER BY rank",
                         (run_date,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("breakdown", "features", "ai"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out


def update_row(table: str, run_date: str, ticker: str, fields: dict,
               path: Optional[str] = None) -> None:
    if table not in ("open_bell_picks", "open_bell_eligible") or not fields:
        raise ValueError("bad table or empty update")
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect(path) as c:
        c.execute(f"UPDATE {table} SET {cols} WHERE run_date=? AND ticker=?",
                  (*fields.values(), run_date, ticker))
