"""SQLite storage for orders, dispatches and the change history."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    city  TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES customers(id),
    po_number      TEXT NOT NULL,
    order_date     TEXT NOT NULL,
    due_date       TEXT NOT NULL,
    tolerance_pct  REAL NOT NULL DEFAULT 0,   -- e.g. 5 = customer accepts up to 5% extra
    cancelled      INTEGER NOT NULL DEFAULT 0,
    notes          TEXT,
    UNIQUE (customer_id, po_number COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS order_lines (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(id),
    item_code     TEXT NOT NULL,
    item_name     TEXT,
    ordered_qty   REAL NOT NULL CHECK (ordered_qty > 0),
    uom           TEXT NOT NULL DEFAULT 'nos',
    closed_qty    REAL NOT NULL DEFAULT 0     -- balance short-closed (customer no longer wants it)
);

CREATE TABLE IF NOT EXISTS dispatches (
    id             INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES orders(id),
    dispatch_date  TEXT NOT NULL,
    invoice_no     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    vehicle_no     TEXT,
    transporter    TEXT,
    lr_no          TEXT,
    pod_date       TEXT,                      -- proof of delivery received
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS dispatch_lines (
    dispatch_id  INTEGER NOT NULL REFERENCES dispatches(id),
    line_id      INTEGER NOT NULL REFERENCES order_lines(id),
    qty          REAL NOT NULL CHECK (qty > 0),
    PRIMARY KEY (dispatch_id, line_id)
);

-- Every change to an order after it was entered, in plain words.
CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    event      TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DISPATCH_DB", str(Path(__file__).resolve().parent.parent / "dispatch.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
