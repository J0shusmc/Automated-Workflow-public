from __future__ import annotations

import csv
import io
from pathlib import Path


SAMPLE_INVENTORY_CSV = """sku,product,supplier,on_hand,reorder_point,reorder_qty,avg_daily_sales,unit_cost,lead_time_days,last_30_units_sold
BEAN-HOUSE-12,12 oz House Blend,Roaster Supply Co,22,30,72,3.1,6.25,7,94
BEAN-ESP-5LB,5 lb Espresso Beans,Roaster Supply Co,9,10,18,0.65,33.00,7,19
BOTTLE-CB,Cold Brew Bottle,Local Bottling,38,45,120,3.7,2.10,4,111
CANDLE-CEDAR,Cedar Soy Candle,Wax & Wick Wholesale,14,18,48,1.5,7.50,10,46
CANDLE-VANILLA,Vanilla Soy Candle,Wax & Wick Wholesale,41,16,24,0.42,7.25,10,13
MUG-LOGO,Logo Mug,Print House,33,12,24,0.23,6.00,14,7
TEE-LOGO,Logo T-Shirt,Print House,7,8,24,0.17,11.00,14,5
STICKER-PACK,Sticker Pack,Print House,90,20,50,0.08,2.75,14,2
"""


def input_text() -> str:
    value = (
        inputs.get("inventory_csv")
        or inputs.get("csv")
        or inputs.get("input")
        or inputs.get("value")
        or SAMPLE_INVENTORY_CSV
    )
    if isinstance(value, dict):
        value = value.get("path") or value.get("csv") or value.get("data") or ""
    text = str(value).strip()
    path = Path(text).expanduser()
    if text and path.is_file():
        return path.read_text(encoding="utf-8")
    return text or SAMPLE_INVENTORY_CSV


def money(value: float) -> str:
    return f"${value:,.2f}"


def parse_rows(text: str) -> list[dict]:
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        item = {key: (value or "").strip() for key, value in raw.items()}
        item["on_hand"] = int(float(item.get("on_hand", 0) or 0))
        item["reorder_point"] = int(float(item.get("reorder_point", 0) or 0))
        item["reorder_qty"] = int(float(item.get("reorder_qty", 0) or 0))
        item["avg_daily_sales"] = float(item.get("avg_daily_sales", 0) or 0)
        item["unit_cost"] = float(item.get("unit_cost", 0) or 0)
        item["lead_time_days"] = int(float(item.get("lead_time_days", 0) or 0))
        item["last_30_units_sold"] = int(float(item.get("last_30_units_sold", 0) or 0))
        rows.append(item)
    if not rows:
        raise ValueError("No inventory rows found. Provide CSV with sku, product, supplier, on_hand, reorder_point, and reorder_qty columns.")
    return rows


rows = parse_rows(input_text())
reorder_now = []
watch_list = []
overstock = []
purchase_orders = {}

for item in rows:
    avg_daily_sales = item["avg_daily_sales"]
    days_cover = item["on_hand"] / avg_daily_sales if avg_daily_sales else 999
    reorder_triggered = item["on_hand"] <= item["reorder_point"] or days_cover <= item["lead_time_days"] + 3
    line = {
        "sku": item["sku"],
        "product": item["product"],
        "supplier": item["supplier"],
        "on_hand": item["on_hand"],
        "reorder_point": item["reorder_point"],
        "reorder_qty": item["reorder_qty"],
        "days_cover": round(days_cover, 1),
        "estimated_cost": round(item["reorder_qty"] * item["unit_cost"], 2),
        "reason": "below reorder point" if item["on_hand"] <= item["reorder_point"] else "stock will run out before next delivery window",
    }
    if reorder_triggered:
        reorder_now.append(line)
        purchase_orders.setdefault(item["supplier"], []).append(line)
    elif days_cover <= 21:
        watch_list.append(line)
    elif days_cover >= 90 and item["last_30_units_sold"] <= 5:
        overstock.append({**line, "reason": "slow movement with high stock coverage"})

total_reorder_cost = sum(item["estimated_cost"] for item in reorder_now)
supplier_messages = []
for supplier, items in sorted(purchase_orders.items()):
    lines = [f"Draft order for {supplier}:"]
    for item in items:
        lines.append(f"- {item['reorder_qty']} x {item['product']} ({item['sku']})")
    lines.append(f"Estimated order cost: {money(sum(item['estimated_cost'] for item in items))}")
    supplier_messages.append("\n".join(lines))

report_lines = [
    "Inventory Reorder Alerts Demo",
    "",
    f"Items reviewed: {len(rows)}",
    f"Items to reorder now: {len(reorder_now)}",
    f"Estimated reorder spend: {money(total_reorder_cost)}",
    "",
    "Reorder now:",
]
for item in reorder_now:
    report_lines.append(
        f"- {item['product']} ({item['sku']}): {item['on_hand']} on hand, reorder {item['reorder_qty']}, "
        f"{item['days_cover']} days cover, {item['reason']}."
    )
report_lines.append("")
report_lines.append("Watch list:")
if watch_list:
    for item in watch_list:
        report_lines.append(f"- {item['product']}: {item['days_cover']} days cover.")
else:
    report_lines.append("- No watch-list items in this sample.")
report_lines.append("")
report_lines.append("Slow/overstock review:")
if overstock:
    for item in overstock:
        report_lines.append(f"- {item['product']}: {item['on_hand']} on hand, {item['days_cover']} days cover, low recent sales.")
else:
    report_lines.append("- No major overstock issue in this sample.")
report_lines.append("")
report_lines.append("Supplier order drafts:")
for message in supplier_messages:
    report_lines.append(message)

result = {
    "report": "\n".join(report_lines),
    "reorder_now": reorder_now,
    "watch_list": watch_list,
    "overstock": overstock,
    "supplier_messages": supplier_messages,
    "estimated_reorder_spend": round(total_reorder_cost, 2),
}
