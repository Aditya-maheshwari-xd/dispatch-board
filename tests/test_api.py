import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCH_DB", str(tmp_path / "api.db"))
    from dispatch.api import app
    from dispatch.db import connect
    from dispatch.seed import seed
    seed(connect())
    return TestClient(app)


def test_board_has_every_stage_from_seed(client):
    b = client.get("/api/board").json()
    cols = {k: len(v) for k, v in b["columns"].items()}
    # The short-closed order already had its truck delivered, so it counts as delivered.
    assert cols["delivered"] == 2 and cols["in_transit"] == 1 and cols["cancelled"] == 1
    assert cols["open"] == 3 and cols["partial"] == 2
    assert len(b["overdue"]) == 2  # Pune balance and Rajkot


def test_home_and_slip_render(client):
    assert "Dispatch Board" in client.get("/").text
    b = client.get("/api/board").json()
    d = b["awaiting_pod"][0]
    slip = client.get(f"/dispatches/{d['id']}/slip").text
    assert d["invoice_no"] in slip and "Balance after" in slip


def test_over_dispatch_returns_400_with_reason(client):
    b = client.get("/api/board").json()
    partial = b["columns"]["partial"][0]
    line = partial["lines"][0]
    r = client.post(f"/api/orders/{partial['id']}/dispatches", json={
        "dispatch_date": b["today"], "invoice_no": "INV-T1", "lines": {line["item_code"]: line["pending"] * 3 + 1}})
    assert r.status_code == 400 and "can go" in r.json()["error"]


def test_create_order_and_dispatch_flow(client):
    today = client.get("/api/board").json()["today"]
    r = client.post("/api/orders", json={"customer": "New Customer", "po_number": "NC-1", "order_date": today,
                                         "due_date": today, "lines": [{"item_code": "BRK-220", "qty": 100}]})
    assert r.status_code == 201
    oid = r.json()["order_id"]
    r = client.post(f"/api/orders/{oid}/dispatches", json={"dispatch_date": today, "invoice_no": "INV-NC-1",
                                                           "lines": {"BRK-220": 100}, "vehicle_no": "tn 01 a 1"})
    assert r.status_code == 201 and r.json()["slip"].endswith("/slip")
    assert client.get(f"/api/orders/{oid}").json()["stage"] == "in_transit"


def test_duplicate_po_is_409(client):
    b = client.get("/api/board").json()
    o = b["columns"]["open"][0]
    r = client.post("/api/orders", json={"customer": o["customer"], "po_number": o["po_number"],
                                         "order_date": b["today"], "due_date": b["today"],
                                         "lines": [{"item_code": "X", "qty": 1}]})
    assert r.status_code == 409


def test_missing_order_is_404(client):
    assert client.get("/api/orders/999").status_code == 404
