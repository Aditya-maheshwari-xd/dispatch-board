"""Order and dispatch rules.

What a dispatch in-charge has to keep straight, written down as rules:

* Orders often go out in several trucks. Each dispatch is recorded against
  the order's lines, and the board shows what's still pending.
* You can't send more than was ordered — unless the customer allows a
  tolerance (common for cut-to-length or weighed material).
* Customers change quantities after ordering. That's allowed, but never
  below what has already been sent, and the change is logged.
* "We don't need the rest" is a short-close: the balance is cancelled with
  a reason, and the order stops showing as pending.
* An order is only "delivered" once proof of delivery (POD) is back for
  every truck, because that's when the invoice can be chased.
* The same customer PO or the same invoice number can't be entered twice.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def plant_today() -> date:
    """Today's date at the plant, not on the server.

    The server may run in UTC; at 2 AM in Chennai it is still "yesterday" in UTC,
    and a dispatch dated today would be rejected as being in the future.
    """
    return datetime.now(ZoneInfo(os.environ.get("PLANT_TZ", "Asia/Kolkata"))).date()


def q(x: float) -> float:
    return round(x, 3) + 0.0


class DispatchError(Exception):
    status = 400

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(DispatchError):
    status = 404


class Conflict(DispatchError):
    status = 409


def _d(value: str | date, what: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DispatchError(f"{what} must be a date like 2026-10-18")


def plant_now() -> str:
    return datetime.now(ZoneInfo(os.environ.get("PLANT_TZ", "Asia/Kolkata"))).strftime("%Y-%m-%d %H:%M")


def _log(conn, order_id: int, event: str) -> None:
    conn.execute("INSERT INTO order_events (order_id, at, event) VALUES (?, ?, ?)", (order_id, plant_now(), event))


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

def get_or_create_customer(conn, name: str, city: str | None = None) -> int:
    name = " ".join((name or "").split())
    if not name:
        raise DispatchError("Customer name is required")
    row = conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO customers (name, city) VALUES (?, ?)", (name, city)).lastrowid


def create_order(conn, customer: str, po_number: str, order_date: str | date, due_date: str | date,
                 lines: list[dict], *, city: str | None = None, tolerance_pct: float = 0,
                 notes: str | None = None, today: date | None = None) -> int:
    """lines: [{item_code, item_name?, qty, uom?}]"""
    today = today or plant_today()
    od, dd = _d(order_date, "Order date"), _d(due_date, "Due date")
    if od > today:
        raise DispatchError("Order date can't be in the future")
    if dd < od:
        raise DispatchError("Due date can't be before the order date")
    if not po_number or not po_number.strip():
        raise DispatchError("Customer PO number is required")
    if not 0 <= tolerance_pct <= 20:
        raise DispatchError("Tolerance must be between 0% and 20%")
    if not lines:
        raise DispatchError("An order needs at least one line")
    codes = [ln["item_code"].strip().upper() for ln in lines]
    if len(set(codes)) != len(codes):
        raise DispatchError("The same item appears twice on this order — combine the quantities")
    for ln in lines:
        if ln.get("qty") is None or ln["qty"] <= 0:
            raise DispatchError(f"Quantity for {ln['item_code']} must be more than zero")

    with conn:
        cust_id = get_or_create_customer(conn, customer, city)
        try:
            order_id = conn.execute(
                """INSERT INTO orders (customer_id, po_number, order_date, due_date, tolerance_pct, notes)
                   VALUES (?,?,?,?,?,?)""",
                (cust_id, po_number.strip(), od.isoformat(), dd.isoformat(), tolerance_pct, notes)).lastrowid
        except sqlite3.IntegrityError:
            existing = conn.execute("SELECT id FROM orders WHERE customer_id = ? AND po_number = ? COLLATE NOCASE",
                                    (cust_id, po_number.strip())).fetchone()
            raise Conflict(f"PO {po_number.strip()} from {customer} is already entered as order #{existing['id']}",
                           existing_order_id=existing["id"])
        for ln, code in zip(lines, codes):
            conn.execute("INSERT INTO order_lines (order_id, item_code, item_name, ordered_qty, uom) VALUES (?,?,?,?,?)",
                         (order_id, code, ln.get("item_name"), ln["qty"], ln.get("uom", "nos")))
        _log(conn, order_id, f"Order entered: PO {po_number.strip()}, due {dd:%d %b %Y}")
    return order_id


def _order_row(conn, order_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise NotFound(f"No order #{order_id}")
    return row


def _lines(conn, order_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT l.*, COALESCE((SELECT SUM(qty) FROM dispatch_lines dl WHERE dl.line_id = l.id), 0) AS sent
           FROM order_lines l WHERE l.order_id = ? ORDER BY l.id""", (order_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sent"] = q(d["sent"])
        d["pending"] = max(0.0, q(d["ordered_qty"] - d["closed_qty"] - d["sent"]))
        out.append(d)
    return out


def amend_quantity(conn, order_id: int, item_code: str, new_qty: float, reason: str) -> None:
    order = _order_row(conn, order_id)
    if order["cancelled"]:
        raise DispatchError("This order is cancelled")
    if not reason or not reason.strip():
        raise DispatchError("Give a reason for the change (e.g. 'customer revised PO, email 12 Oct')")
    line = next((ln for ln in _lines(conn, order_id) if ln["item_code"] == item_code.strip().upper()), None)
    if line is None:
        raise NotFound(f"{item_code} is not on order #{order_id}")
    if new_qty <= 0:
        raise DispatchError("Use short-close or cancel instead of setting quantity to zero")
    if new_qty < line["sent"] + line["closed_qty"]:
        raise DispatchError(f"{line['sent']:g} {line['uom']} of {line['item_code']} has already been dispatched; "
                            f"the order can't be reduced below that", already_sent=line["sent"])
    with conn:
        conn.execute("UPDATE order_lines SET ordered_qty = ? WHERE id = ?", (new_qty, line["id"]))
        _log(conn, order_id, f"{line['item_code']} quantity changed {line['ordered_qty']:g} → {new_qty:g}: {reason.strip()}")


def short_close(conn, order_id: int, item_code: str | None, reason: str) -> None:
    """Cancel the undispatched balance of one line, or of every line when item_code is None."""
    order = _order_row(conn, order_id)
    if order["cancelled"]:
        raise DispatchError("This order is cancelled")
    if not reason or not reason.strip():
        raise DispatchError("Give a reason for short-closing")
    lines = _lines(conn, order_id)
    if item_code:
        lines = [ln for ln in lines if ln["item_code"] == item_code.strip().upper()]
        if not lines:
            raise NotFound(f"{item_code} is not on order #{order_id}")
    lines = [ln for ln in lines if ln["pending"] > 0]
    if not lines:
        raise DispatchError("Nothing pending to short-close")
    with conn:
        for ln in lines:
            conn.execute("UPDATE order_lines SET closed_qty = closed_qty + ? WHERE id = ?", (ln["pending"], ln["id"]))
            _log(conn, order_id, f"{ln['item_code']}: balance {ln['pending']:g} short-closed: {reason.strip()}")


def cancel_order(conn, order_id: int, reason: str) -> None:
    order = _order_row(conn, order_id)
    if order["cancelled"]:
        raise Conflict("Order is already cancelled")
    if not reason or not reason.strip():
        raise DispatchError("Give a reason for cancelling")
    if any(ln["sent"] > 0 for ln in _lines(conn, order_id)):
        raise DispatchError("Part of this order has already been dispatched — short-close the balance instead")
    with conn:
        conn.execute("UPDATE orders SET cancelled = 1 WHERE id = ?", (order_id,))
        _log(conn, order_id, f"Order cancelled: {reason.strip()}")


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def record_dispatch(conn, order_id: int, dispatch_date: str | date, invoice_no: str, lines: dict[str, float], *,
                    vehicle_no: str | None = None, transporter: str | None = None, lr_no: str | None = None,
                    today: date | None = None) -> int:
    """lines: {item_code: qty}. Everything is checked before anything is written."""
    today = today or plant_today()
    order = _order_row(conn, order_id)
    if order["cancelled"]:
        raise DispatchError("This order is cancelled")
    dd = _d(dispatch_date, "Dispatch date")
    if dd > today:
        raise DispatchError("Dispatch date can't be in the future")
    if dd < date.fromisoformat(order["order_date"]):
        raise DispatchError("Dispatch date is before the order date")
    if not invoice_no or not invoice_no.strip():
        raise DispatchError("Invoice number is required")
    if conn.execute("SELECT 1 FROM dispatches WHERE invoice_no = ?", (invoice_no.strip(),)).fetchone():
        raise Conflict(f"Invoice {invoice_no.strip()} is already recorded")
    lines = {k.strip().upper(): v for k, v in lines.items() if v}
    if not lines:
        raise DispatchError("Nothing to dispatch — enter a quantity for at least one item")

    by_code = {ln["item_code"]: ln for ln in _lines(conn, order_id)}
    tol = order["tolerance_pct"] / 100
    problems = []
    for code, qty in lines.items():
        ln = by_code.get(code)
        if ln is None:
            problems.append(f"{code} is not on this order")
            continue
        if qty < 0:
            problems.append(f"{code}: quantity can't be negative")
            continue
        open_qty = ln["ordered_qty"] - ln["closed_qty"]
        if open_qty <= ln["sent"] and ln["closed_qty"] > 0:
            problems.append(f"{code}: balance was short-closed, nothing left to send")
            continue
        allowed = q(open_qty * (1 + tol) - ln["sent"])
        if qty > allowed:
            extra = f" (incl. {order['tolerance_pct']:g}% tolerance)" if tol else ""
            problems.append(f"{code}: sending {qty:g} but only {max(allowed, 0):g} {ln['uom']} can go{extra} "
                            f"— ordered {ln['ordered_qty']:g}, already sent {ln['sent']:g}")
    if problems:
        raise DispatchError("; ".join(problems), problems=problems)

    plate = vehicle_no.strip().upper().replace(" ", "") if vehicle_no else None
    with conn:
        did = conn.execute(
            """INSERT INTO dispatches (order_id, dispatch_date, invoice_no, vehicle_no, transporter, lr_no)
               VALUES (?,?,?,?,?,?)""",
            (order_id, dd.isoformat(), invoice_no.strip(), plate, transporter, lr_no)).lastrowid
        for code, qty in lines.items():
            conn.execute("INSERT INTO dispatch_lines VALUES (?,?,?)", (did, by_code[code]["id"], qty))
        summary = ", ".join(f"{c} {v:g}" for c, v in lines.items())
        _log(conn, order_id, f"Dispatched on invoice {invoice_no.strip()}{f' ({plate})' if plate else ''}: {summary}")
    return did


def confirm_delivery(conn, dispatch_id: int, pod_date: str | date, *, today: date | None = None) -> None:
    today = today or plant_today()
    d = conn.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    if d is None:
        raise NotFound(f"No dispatch #{dispatch_id}")
    if d["pod_date"]:
        raise Conflict(f"POD for invoice {d['invoice_no']} was already recorded on {d['pod_date']}")
    pd_ = _d(pod_date, "Delivery date")
    if pd_ < date.fromisoformat(d["dispatch_date"]):
        raise DispatchError("Delivery date is before the dispatch date")
    if pd_ > today:
        raise DispatchError("Delivery date can't be in the future")
    with conn:
        conn.execute("UPDATE dispatches SET pod_date = ? WHERE id = ?", (pd_.isoformat(), dispatch_id))
        _log(conn, d["order_id"], f"POD received for invoice {d['invoice_no']} (delivered {pd_:%d %b})")


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

STAGES = ["open", "partial", "in_transit", "delivered", "closed", "cancelled"]


@dataclass
class OrderView:
    id: int
    customer: str
    city: str | None
    po_number: str
    order_date: str
    due_date: str
    stage: str
    overdue_days: int
    age_days: int
    lines: list[dict]
    dispatches: list[dict]
    events: list[dict]
    tolerance_pct: float


def order_view(conn, order_id: int, today: date | None = None) -> OrderView:
    today = today or plant_today()
    o = conn.execute("""SELECT o.*, c.name AS customer, c.city FROM orders o
                        JOIN customers c ON c.id = o.customer_id WHERE o.id = ?""", (order_id,)).fetchone()
    if o is None:
        raise NotFound(f"No order #{order_id}")
    lines = _lines(conn, order_id)
    dispatches = [dict(r) for r in conn.execute(
        "SELECT * FROM dispatches WHERE order_id = ? ORDER BY dispatch_date, id", (order_id,)).fetchall()]
    for d in dispatches:
        d["lines"] = [dict(r) for r in conn.execute(
            """SELECT l.item_code, dl.qty, l.uom FROM dispatch_lines dl JOIN order_lines l ON l.id = dl.line_id
               WHERE dl.dispatch_id = ?""", (d["id"],)).fetchall()]
    events = [dict(r) for r in conn.execute(
        "SELECT at, event FROM order_events WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()]

    pending = sum(ln["pending"] for ln in lines)
    sent = sum(ln["sent"] for ln in lines)
    if o["cancelled"]:
        stage = "cancelled"
    elif pending > 0:
        stage = "partial" if sent > 0 else "open"
    elif sent == 0:
        stage = "closed"                       # short-closed before anything was sent
    elif any(d["pod_date"] is None for d in dispatches):
        stage = "in_transit"
    else:
        stage = "delivered"
    due = date.fromisoformat(o["due_date"])
    overdue = (today - due).days if stage in ("open", "partial") and today > due else 0
    age = (today - date.fromisoformat(o["order_date"])).days
    return OrderView(o["id"], o["customer"], o["city"], o["po_number"], o["order_date"], o["due_date"], stage,
                     overdue, age, lines, dispatches, events, o["tolerance_pct"])


def board(conn, today: date | None = None) -> dict:
    """Everything the dispatch screen needs in one call."""
    today = today or plant_today()
    views = [order_view(conn, r["id"], today) for r in
             conn.execute("SELECT id FROM orders ORDER BY due_date, id").fetchall()]
    columns = {s: [] for s in STAGES}
    for v in views:
        columns[v.stage].append(v)

    # What production still has to make, earliest due first.
    pending_items: dict[str, dict] = {}
    for v in views:
        if v.stage not in ("open", "partial"):
            continue
        for ln in v.lines:
            if ln["pending"] <= 0:
                continue
            p = pending_items.setdefault(ln["item_code"], {"item_code": ln["item_code"], "item_name": ln["item_name"],
                                                           "uom": ln["uom"], "pending": 0.0, "orders": 0,
                                                           "earliest_due": v.due_date})
            p["pending"] = q(p["pending"] + ln["pending"])
            p["orders"] += 1
            p["earliest_due"] = min(p["earliest_due"], v.due_date)

    def bucket(days: int) -> str:
        return "0–7 days" if days <= 7 else "8–15 days" if days <= 15 else "16–30 days" if days <= 30 else "30+ days"
    aging: dict[str, int] = {b: 0 for b in ("0–7 days", "8–15 days", "16–30 days", "30+ days")}
    for v in views:
        if v.stage in ("open", "partial"):
            aging[bucket(v.age_days)] += 1

    awaiting_pod = [d | {"order_id": v.id, "customer": v.customer, "days_since": (today - date.fromisoformat(d["dispatch_date"])).days}
                    for v in views for d in v.dispatches if d["pod_date"] is None]
    return {
        "today": today.isoformat(),
        "columns": columns,
        "pending_items": sorted(pending_items.values(), key=lambda p: (p["earliest_due"], p["item_code"])),
        "aging": aging,
        "overdue": sorted([v for v in views if v.overdue_days > 0], key=lambda v: -v.overdue_days),
        "awaiting_pod": sorted(awaiting_pod, key=lambda d: -d["days_since"]),
    }


def dispatch_slip(conn, dispatch_id: int) -> dict:
    d = conn.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    if d is None:
        raise NotFound(f"No dispatch #{dispatch_id}")
    view = order_view(conn, d["order_id"])
    lines = conn.execute(
        """SELECT l.item_code, l.item_name, l.uom, l.ordered_qty, dl.qty FROM dispatch_lines dl
           JOIN order_lines l ON l.id = dl.line_id WHERE dl.dispatch_id = ?""", (dispatch_id,)).fetchall()
    # Balance after *this* dispatch, not after later ones, so a reprinted slip shows the same numbers.
    out_lines = []
    for ln in lines:
        sent_upto = conn.execute(
            """SELECT COALESCE(SUM(dl.qty), 0) FROM dispatch_lines dl JOIN dispatches x ON x.id = dl.dispatch_id
               JOIN order_lines l ON l.id = dl.line_id
               WHERE l.item_code = ? AND x.order_id = ? AND (x.dispatch_date < ? OR (x.dispatch_date = ? AND x.id <= ?))""",
            (ln["item_code"], d["order_id"], d["dispatch_date"], d["dispatch_date"], dispatch_id)).fetchone()[0]
        out_lines.append({**dict(ln), "balance_after": max(0.0, q(ln["ordered_qty"] - sent_upto))})
    return {"dispatch": dict(d), "order": view, "lines": out_lines}


def days_ago(n: int, today: date | None = None) -> str:
    return ((today or plant_today()) - timedelta(days=n)).isoformat()
