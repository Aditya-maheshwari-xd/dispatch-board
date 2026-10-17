"""Sample orders for a fictional auto-component supplier, dated relative to today
so the board always shows a realistic mix: fresh, partial, overdue, in transit.

python -m dispatch.seed [--fresh]
"""

from __future__ import annotations

import argparse
import os
from datetime import date

from . import services as svc
from .db import connect, db_path


def seed(conn, today: date | None = None) -> None:
    today = today or svc.plant_today()
    ago = lambda n: svc.days_ago(n, today)  # noqa: E731
    ahead = lambda n: svc.days_ago(-n, today)  # noqa: E731

    # 1. Delivered: two trucks, both PODs back
    o1 = svc.create_order(conn, "Sundaram Motors", "SM/PO/2026/1187", ago(34), ago(20), [
        {"item_code": "BRK-220", "item_name": "Mounting bracket", "qty": 5000},
        {"item_code": "HNG-310", "item_name": "Door hinge", "qty": 2000}], city="Chennai", today=today)
    d = svc.record_dispatch(conn, o1, ago(26), "INV/26-27/0381", {"BRK-220": 3000, "HNG-310": 2000},
                            vehicle_no="TN 09 BX 4421", transporter="VRL Logistics", lr_no="LR-55120", today=today)
    svc.confirm_delivery(conn, d, ago(24), today=today)
    d = svc.record_dispatch(conn, o1, ago(21), "INV/26-27/0397", {"BRK-220": 2000},
                            vehicle_no="TN 09 BX 4421", transporter="VRL Logistics", today=today)
    svc.confirm_delivery(conn, d, ago(19), today=today)

    # 2. Partial and overdue: customer wants the balance urgently
    o2 = svc.create_order(conn, "Pune Auto Assemblies", "PAA-4471", ago(19), ago(5), [
        {"item_code": "BRK-220", "item_name": "Mounting bracket", "qty": 8000},
        {"item_code": "CLP-118", "item_name": "Hose clamp", "qty": 12000}], city="Pune", today=today)
    svc.record_dispatch(conn, o2, ago(9), "INV/26-27/0412", {"BRK-220": 5000, "CLP-118": 12000},
                        vehicle_no="MH 12 QT 9081", transporter="Gati", lr_no="GT-778812", today=today)

    # 3. Dispatched, POD not back yet (invoice can't be chased)
    o3 = svc.create_order(conn, "Hosur Precision Pvt Ltd", "HP/7781", ago(12), ago(2), [
        {"item_code": "HNG-310", "item_name": "Door hinge", "qty": 1500}], city="Hosur", today=today)
    svc.record_dispatch(conn, o3, ago(3), "INV/26-27/0420", {"HNG-310": 1500},
                        vehicle_no="KA 51 MN 2290", transporter="Own vehicle", today=today)

    # 4. Steel blanks sold by weight, 5% tolerance, customer revised the quantity
    o4 = svc.create_order(conn, "Sundaram Motors", "SM/PO/2026/1243", ago(8), ahead(6), [
        {"item_code": "BLK-CR12", "item_name": "CRCA blanks 1.2 mm", "qty": 4000, "uom": "kg"}],
        city="Chennai", tolerance_pct=5, today=today)
    svc.amend_quantity(conn, o4, "BLK-CR12", 4500, "Customer revised PO by email on " + ago(6))
    svc.record_dispatch(conn, o4, ago(2), "INV/26-27/0426", {"BLK-CR12": 2380},
                        vehicle_no="TN 09 BX 4421", today=today)

    # 5. Fresh orders, nothing sent yet
    svc.create_order(conn, "Coimbatore Pumps", "CP-2026-0091", ago(4), ahead(10), [
        {"item_code": "CLP-118", "item_name": "Hose clamp", "qty": 6000},
        {"item_code": "BRK-220", "item_name": "Mounting bracket", "qty": 1200}], city="Coimbatore", today=today)
    svc.create_order(conn, "Pune Auto Assemblies", "PAA-4520", ago(1), ahead(14), [
        {"item_code": "HNG-310", "item_name": "Door hinge", "qty": 3000}], city="Pune", today=today)

    # 6. Old order, overdue, nothing dispatched
    svc.create_order(conn, "Rajkot Engineering Works", "REW/88", ago(40), ago(12), [
        {"item_code": "BRK-220", "item_name": "Mounting bracket", "qty": 2500}], city="Rajkot", today=today)

    # 7. Customer said they don't need the balance
    o7 = svc.create_order(conn, "Hosur Precision Pvt Ltd", "HP/7702", ago(30), ago(16), [
        {"item_code": "CLP-118", "item_name": "Hose clamp", "qty": 5000}], city="Hosur", today=today)
    d = svc.record_dispatch(conn, o7, ago(20), "INV/26-27/0390", {"CLP-118": 3500}, today=today)
    svc.confirm_delivery(conn, d, ago(18), today=today)
    svc.short_close(conn, o7, None, "Model discontinued, customer confirmed on call")

    # 8. Cancelled before anything was made
    o8 = svc.create_order(conn, "Coimbatore Pumps", "CP-2026-0077", ago(15), ago(1), [
        {"item_code": "HNG-310", "item_name": "Door hinge", "qty": 800}], city="Coimbatore", today=today)
    svc.cancel_order(conn, o8, "Customer moved this part to another vendor")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--fresh", action="store_true")
    args = p.parse_args()
    path = db_path()
    if args.fresh and os.path.exists(path):
        os.remove(path)
    conn = connect(path)
    if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
        print(f"{path} already has orders — use --fresh to start over")
        return
    seed(conn)
    print(f"Seeded {path}")


if __name__ == "__main__":
    main()
