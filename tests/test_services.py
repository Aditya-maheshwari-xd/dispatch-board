from datetime import date

import pytest

from dispatch import services as svc
from dispatch.db import connect

TODAY = date(2026, 10, 18)


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def order(conn, qty=1000, tol=0, po="PO-1", lines=None, order_date="2026-10-01", due="2026-10-10"):
    return svc.create_order(conn, "Acme Motors", po, order_date, due,
                            lines or [{"item_code": "BRK", "qty": qty}], tolerance_pct=tol, today=TODAY)


def ship(conn, oid, inv, lines, when="2026-10-05"):
    return svc.record_dispatch(conn, oid, when, inv, lines, today=TODAY)


def view(conn, oid):
    return svc.order_view(conn, oid, TODAY)


def test_partial_then_full_then_delivered(conn):
    oid = order(conn)
    assert view(conn, oid).stage == "open"
    d1 = ship(conn, oid, "INV-1", {"BRK": 600})
    v = view(conn, oid)
    assert (v.stage, v.lines[0]["sent"], v.lines[0]["pending"]) == ("partial", 600, 400)
    d2 = ship(conn, oid, "INV-2", {"brk": 400})  # item code case doesn't matter
    assert view(conn, oid).stage == "in_transit"  # all sent, POD not back
    svc.confirm_delivery(conn, d1, "2026-10-06", today=TODAY)
    assert view(conn, oid).stage == "in_transit"  # one truck still without POD
    svc.confirm_delivery(conn, d2, "2026-10-07", today=TODAY)
    assert view(conn, oid).stage == "delivered"


def test_cannot_over_dispatch(conn):
    oid = order(conn)
    ship(conn, oid, "INV-1", {"BRK": 900})
    with pytest.raises(svc.DispatchError, match="only 100"):
        ship(conn, oid, "INV-2", {"BRK": 150})
    assert view(conn, oid).lines[0]["sent"] == 900  # nothing written


def test_tolerance_allows_small_excess(conn):
    oid = order(conn, qty=1000, tol=5)
    ship(conn, oid, "INV-1", {"BRK": 1040})       # within +5%
    v = view(conn, oid)
    assert v.lines[0]["pending"] == 0 and v.stage == "in_transit"
    oid2 = order(conn, qty=1000, tol=5, po="PO-2")
    with pytest.raises(svc.DispatchError, match="5% tolerance"):
        ship(conn, oid2, "INV-2", {"BRK": 1060})


def test_multi_line_dispatch_is_all_or_nothing(conn):
    oid = order(conn, lines=[{"item_code": "BRK", "qty": 100}, {"item_code": "HNG", "qty": 50}])
    with pytest.raises(svc.DispatchError) as err:
        ship(conn, oid, "INV-1", {"BRK": 100, "HNG": 80, "XYZ": 1})
    assert len(err.value.details["problems"]) == 2
    assert all(ln["sent"] == 0 for ln in view(conn, oid).lines)


def test_duplicate_po_and_invoice(conn):
    oid = order(conn)
    with pytest.raises(svc.Conflict, match="already entered"):
        svc.create_order(conn, "acme  motors", "po-1", "2026-10-02", "2026-10-12",
                         [{"item_code": "X", "qty": 1}], today=TODAY)  # same customer, spacing/case differ
    svc.create_order(conn, "Other Ltd", "PO-1", "2026-10-02", "2026-10-12",
                     [{"item_code": "X", "qty": 1}], today=TODAY)       # other customer may reuse the number
    ship(conn, oid, "INV/26-27/001", {"BRK": 10})
    with pytest.raises(svc.Conflict):
        ship(conn, oid, "inv/26-27/001", {"BRK": 10})


def test_amend_quantity_not_below_dispatched(conn):
    oid = order(conn)
    ship(conn, oid, "INV-1", {"BRK": 700})
    with pytest.raises(svc.DispatchError, match="already been dispatched"):
        svc.amend_quantity(conn, oid, "BRK", 600, "customer cut PO")
    svc.amend_quantity(conn, oid, "BRK", 800, "customer cut PO")
    v = view(conn, oid)
    assert v.lines[0]["pending"] == 100
    assert "1000 → 800" in v.events[-1]["event"]


def test_short_close_stops_pending_and_blocks_more_dispatch(conn):
    oid = order(conn)
    ship(conn, oid, "INV-1", {"BRK": 700})
    svc.short_close(conn, oid, None, "Model discontinued")
    v = view(conn, oid)
    assert v.lines[0]["pending"] == 0 and v.lines[0]["closed_qty"] == 300
    assert v.stage == "in_transit"  # what was sent still needs its POD
    with pytest.raises(svc.DispatchError):
        ship(conn, oid, "INV-2", {"BRK": 10})


def test_short_close_needs_reason_and_something_pending(conn):
    oid = order(conn)
    with pytest.raises(svc.DispatchError, match="reason"):
        svc.short_close(conn, oid, None, " ")
    ship(conn, oid, "INV-1", {"BRK": 1000})
    with pytest.raises(svc.DispatchError, match="Nothing pending"):
        svc.short_close(conn, oid, None, "x")


def test_cancel_only_before_dispatch(conn):
    oid = order(conn)
    ship(conn, oid, "INV-1", {"BRK": 1})
    with pytest.raises(svc.DispatchError, match="short-close"):
        svc.cancel_order(conn, oid, "customer changed supplier")
    oid2 = order(conn, po="PO-2")
    svc.cancel_order(conn, oid2, "customer changed supplier")
    assert view(conn, oid2).stage == "cancelled"
    with pytest.raises(svc.DispatchError):
        ship(conn, oid2, "INV-2", {"BRK": 1})


@pytest.mark.parametrize("kwargs,match", [
    ({"order_date": "2026-10-25"}, "future"),
    ({"due": "2026-09-01"}, "before the order date"),
    ({"order_date": "18/10/2026"}, "must be a date"),
    ({"lines": [{"item_code": "A", "qty": 1}, {"item_code": "a", "qty": 2}]}, "twice"),
    ({"lines": [{"item_code": "A", "qty": 0}]}, "more than zero"),
    ({"tol": 50}, "Tolerance"),
])
def test_order_validation(conn, kwargs, match):
    with pytest.raises(svc.DispatchError, match=match):
        order(conn, **kwargs)


def test_dispatch_and_pod_dates_are_checked(conn):
    oid = order(conn)
    with pytest.raises(svc.DispatchError, match="future"):
        ship(conn, oid, "INV-1", {"BRK": 1}, when="2026-10-30")
    with pytest.raises(svc.DispatchError, match="before the order date"):
        ship(conn, oid, "INV-1", {"BRK": 1}, when="2026-09-30")
    d = ship(conn, oid, "INV-1", {"BRK": 1}, when="2026-10-05")
    with pytest.raises(svc.DispatchError, match="before the dispatch"):
        svc.confirm_delivery(conn, d, "2026-10-04", today=TODAY)
    svc.confirm_delivery(conn, d, "2026-10-06", today=TODAY)
    with pytest.raises(svc.Conflict):
        svc.confirm_delivery(conn, d, "2026-10-07", today=TODAY)


def test_overdue_only_counts_while_pending(conn):
    oid = order(conn, due="2026-10-10")
    assert view(conn, oid).overdue_days == 8
    ship(conn, oid, "INV-1", {"BRK": 1000})
    assert view(conn, oid).overdue_days == 0


def test_board_pending_items_and_aging(conn):
    a = order(conn, lines=[{"item_code": "BRK", "qty": 100}, {"item_code": "HNG", "qty": 50}], due="2026-10-20")
    order(conn, po="PO-2", lines=[{"item_code": "BRK", "qty": 40}], order_date="2026-09-01", due="2026-10-15")
    ship(conn, a, "INV-1", {"BRK": 30})
    b = svc.board(conn, TODAY)
    pend = {p["item_code"]: p for p in b["pending_items"]}
    assert pend["BRK"]["pending"] == 110 and pend["BRK"]["orders"] == 2
    assert pend["BRK"]["earliest_due"] == "2026-10-15"
    assert b["aging"]["30+ days"] == 1 and b["aging"]["16–30 days"] == 1
    assert len(b["overdue"]) == 1
    assert [d["invoice_no"] for d in b["awaiting_pod"]] == ["INV-1"]


def test_slip_balance_does_not_change_after_later_dispatches(conn):
    oid = order(conn)
    d1 = ship(conn, oid, "INV-1", {"BRK": 300}, when="2026-10-03")
    before = svc.dispatch_slip(conn, d1)["lines"][0]["balance_after"]
    ship(conn, oid, "INV-2", {"BRK": 500}, when="2026-10-06")
    assert before == svc.dispatch_slip(conn, d1)["lines"][0]["balance_after"] == 700


def test_vehicle_number_normalised(conn):
    oid = order(conn)
    d = ship(conn, oid, "INV-1", {"BRK": 1})
    assert svc.dispatch_slip(conn, d)["dispatch"]["vehicle_no"] is None
    d2 = svc.record_dispatch(conn, oid, "2026-10-05", "INV-2", {"BRK": 1}, vehicle_no="tn 09 ab 1234", today=TODAY)
    assert svc.dispatch_slip(conn, d2)["dispatch"]["vehicle_no"] == "TN09AB1234"


def test_plant_today_uses_plant_time_zone(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setenv("PLANT_TZ", "Asia/Kolkata")
    assert svc.plant_today() == datetime.now(ZoneInfo("Asia/Kolkata")).date()
