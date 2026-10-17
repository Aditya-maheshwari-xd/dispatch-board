"""HTTP API, the board UI, and a printable dispatch slip."""

from __future__ import annotations

import html
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import services as svc
from .db import connect

app = FastAPI(title="Dispatch Board", version="1.0")
STATIC = Path(__file__).parent / "static"


def get_conn() -> Iterator:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(svc.DispatchError)
def dispatch_error(_: Request, exc: svc.DispatchError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message, **exc.details})


class LineIn(BaseModel):
    item_code: str
    item_name: str | None = None
    qty: float
    uom: str = "nos"


class OrderIn(BaseModel):
    customer: str
    city: str | None = None
    po_number: str
    order_date: str
    due_date: str
    tolerance_pct: float = 0
    notes: str | None = None
    lines: list[LineIn]


class DispatchIn(BaseModel):
    dispatch_date: str
    invoice_no: str
    lines: dict[str, float]
    vehicle_no: str | None = None
    transporter: str | None = None
    lr_no: str | None = None


class AmendIn(BaseModel):
    item_code: str
    new_qty: float
    reason: str


class CloseIn(BaseModel):
    item_code: str | None = None
    reason: str


class PodIn(BaseModel):
    pod_date: str


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/board")
def get_board(conn=Depends(get_conn)):
    b = svc.board(conn)
    return {**b, "columns": {k: [asdict(v) for v in vs] for k, vs in b["columns"].items()},
            "overdue": [asdict(v) for v in b["overdue"]]}


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, conn=Depends(get_conn)):
    return asdict(svc.order_view(conn, order_id))


@app.post("/api/orders", status_code=201)
def post_order(body: OrderIn, conn=Depends(get_conn)):
    oid = svc.create_order(conn, body.customer, body.po_number, body.order_date, body.due_date,
                           [ln.model_dump() for ln in body.lines], city=body.city,
                           tolerance_pct=body.tolerance_pct, notes=body.notes)
    return {"order_id": oid}


@app.post("/api/orders/{order_id}/dispatches", status_code=201)
def post_dispatch(order_id: int, body: DispatchIn, conn=Depends(get_conn)):
    did = svc.record_dispatch(conn, order_id, body.dispatch_date, body.invoice_no, body.lines,
                              vehicle_no=body.vehicle_no, transporter=body.transporter, lr_no=body.lr_no)
    return {"dispatch_id": did, "slip": f"/dispatches/{did}/slip"}


@app.post("/api/orders/{order_id}/amend")
def post_amend(order_id: int, body: AmendIn, conn=Depends(get_conn)):
    svc.amend_quantity(conn, order_id, body.item_code, body.new_qty, body.reason)
    return asdict(svc.order_view(conn, order_id))


@app.post("/api/orders/{order_id}/short-close")
def post_short_close(order_id: int, body: CloseIn, conn=Depends(get_conn)):
    svc.short_close(conn, order_id, body.item_code, body.reason)
    return asdict(svc.order_view(conn, order_id))


@app.post("/api/orders/{order_id}/cancel")
def post_cancel(order_id: int, body: CloseIn, conn=Depends(get_conn)):
    svc.cancel_order(conn, order_id, body.reason)
    return asdict(svc.order_view(conn, order_id))


@app.post("/api/dispatches/{dispatch_id}/pod")
def post_pod(dispatch_id: int, body: PodIn, conn=Depends(get_conn)):
    svc.confirm_delivery(conn, dispatch_id, body.pod_date)
    return {"ok": True}


@app.get("/dispatches/{dispatch_id}/slip", response_class=HTMLResponse)
def slip(dispatch_id: int, conn=Depends(get_conn)):
    s = svc.dispatch_slip(conn, dispatch_id)
    d, o = s["dispatch"], s["order"]
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(ln['item_code'])}</td><td>{e(ln['item_name'] or '')}</td>"
        f"<td class=n>{ln['ordered_qty']:,g}</td><td class=n><b>{ln['qty']:,g}</b></td>"
        f"<td class=n>{ln['balance_after']:,g}</td><td>{e(ln['uom'])}</td></tr>" for ln in s["lines"])
    fmt = lambda iso: date.fromisoformat(iso).strftime("%d %b %Y")  # noqa: E731
    return f"""<!doctype html><html><head><meta charset=utf-8><title>Dispatch Slip {e(d['invoice_no'])}</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{font:14px/1.45 system-ui,sans-serif;color:#111;background:#fff;max-width:780px;margin:24px auto;padding:0 16px}}
h1{{font-size:20px;margin:0}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px;margin:16px 0}}
.grid div span{{color:#555;display:inline-block;min-width:110px}}
table{{border-collapse:collapse;width:100%;margin-top:8px}} th,td{{border:1px solid #bbb;padding:6px 8px;text-align:left}}
th{{background:#f0f0f0}} .n{{text-align:right}} .sign{{display:flex;justify-content:space-between;margin-top:60px}}
.sign div{{border-top:1px solid #333;padding-top:4px;width:200px;text-align:center}}
button{{margin-top:20px}} @media print{{button{{display:none}}}}</style></head><body>
<h1>Dispatch Slip</h1><div style="color:#555">Order #{o.id} · generated by Dispatch Board</div>
<div class=grid>
<div><span>Customer</span><b>{e(o.customer)}</b>{', ' + e(o.city) if o.city else ''}</div>
<div><span>Invoice no.</span><b>{e(d['invoice_no'])}</b></div>
<div><span>Customer PO</span>{e(o.po_number)}</div><div><span>Dispatch date</span>{fmt(d['dispatch_date'])}</div>
<div><span>Vehicle no.</span>{e(d['vehicle_no'] or '—')}</div><div><span>Transporter</span>{e(d['transporter'] or '—')}</div>
<div><span>LR no.</span>{e(d['lr_no'] or '—')}</div><div><span>Due date</span>{fmt(o.due_date)}</div></div>
<table><thead><tr><th>Item</th><th>Description</th><th class=n>Ordered</th><th class=n>This dispatch</th>
<th class=n>Balance after</th><th>Unit</th></tr></thead><tbody>{rows}</tbody></table>
<div class=sign><div>Dispatched by</div><div>Driver</div><div>Received by</div></div>
<button onclick="print()">Print</button></body></html>"""
