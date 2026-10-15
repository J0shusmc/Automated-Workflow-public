from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from pathlib import Path


SAMPLE_SALES_CSV = """date,customer,category,product,units,unit_price,cost,channel
2026-07-01,Northside Cafe,Coffee,12 oz House Blend,18,14.00,6.25,in-store
2026-07-01,Northside Cafe,Coffee,5 lb Espresso Beans,4,68.00,33.00,phone
2026-07-02,Lakeside Gifts,Candles,Cedar Soy Candle,12,18.00,7.50,in-store
2026-07-02,Walk-in,Retail,Logo T-Shirt,3,22.00,11.00,in-store
2026-07-03,Main Street Market,Coffee,12 oz House Blend,24,14.00,6.25,delivery
2026-07-03,Walk-in,Candles,Vanilla Soy Candle,5,18.00,7.25,in-store
2026-07-04,Event Booth,Coffee,Cold Brew Bottle,48,5.50,2.10,event
2026-07-05,Lakeside Gifts,Candles,Cedar Soy Candle,16,18.00,7.50,delivery
2026-07-06,Walk-in,Retail,Logo Mug,4,16.00,6.00,in-store
2026-07-06,Northside Cafe,Coffee,5 lb Espresso Beans,5,68.00,33.00,phone
2026-07-07,Main Street Market,Coffee,Cold Brew Bottle,60,5.50,2.10,delivery
2026-07-08,Walk-in,Retail,Logo T-Shirt,2,22.00,11.00,in-store
2026-07-09,Lakeside Gifts,Candles,Vanilla Soy Candle,4,18.00,7.25,in-store
2026-07-10,Northside Cafe,Coffee,12 oz House Blend,20,14.00,6.25,phone
2026-07-11,Event Booth,Candles,Cedar Soy Candle,22,18.00,7.50,event
2026-07-12,Walk-in,Retail,Sticker Pack,2,6.00,2.75,in-store
2026-07-13,Main Street Market,Coffee,5 lb Espresso Beans,3,68.00,33.00,delivery
2026-07-14,Walk-in,Retail,Logo Mug,3,16.00,6.00,in-store
"""


def input_text() -> str:
    value = (
        inputs.get("sales_csv")
        or inputs.get("csv")
        or inputs.get("input")
        or inputs.get("value")
        or SAMPLE_SALES_CSV
    )
    if isinstance(value, dict):
        value = value.get("path") or value.get("csv") or value.get("data") or ""
    text = str(value).strip()
    path = Path(text).expanduser()
    if text and path.is_file():
        return path.read_text(encoding="utf-8")
    return text or SAMPLE_SALES_CSV


def money(value: float) -> str:
    return f"${value:,.2f}"


def parse_rows(text: str) -> list[dict]:
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        units = int(float(raw.get("units", 0) or 0))
        unit_price = float(raw.get("unit_price", 0) or 0)
        cost = float(raw.get("cost", 0) or 0)
        revenue = units * unit_price
        profit = units * (unit_price - cost)
        rows.append({**raw, "units": units, "unit_price": unit_price, "cost": cost, "revenue": revenue, "profit": profit})
    if not rows:
        raise ValueError("No sales rows found. Provide CSV with product, units, unit_price, and cost columns.")
    return rows


rows = parse_rows(input_text())
by_product = defaultdict(lambda: {"units": 0, "revenue": 0.0, "profit": 0.0, "orders": 0, "category": ""})
by_category = defaultdict(lambda: {"units": 0, "revenue": 0.0, "profit": 0.0})
by_customer = defaultdict(lambda: {"revenue": 0.0, "profit": 0.0, "orders": 0})

for row in rows:
    product = row["product"]
    category = row.get("category", "")
    by_product[product]["units"] += row["units"]
    by_product[product]["revenue"] += row["revenue"]
    by_product[product]["profit"] += row["profit"]
    by_product[product]["orders"] += 1
    by_product[product]["category"] = category
    by_category[category]["units"] += row["units"]
    by_category[category]["revenue"] += row["revenue"]
    by_category[category]["profit"] += row["profit"]
    by_customer[row.get("customer", "Unknown")]["revenue"] += row["revenue"]
    by_customer[row.get("customer", "Unknown")]["profit"] += row["profit"]
    by_customer[row.get("customer", "Unknown")]["orders"] += 1

total_revenue = sum(row["revenue"] for row in rows)
total_profit = sum(row["profit"] for row in rows)
total_units = sum(row["units"] for row in rows)
margin = total_profit / total_revenue if total_revenue else 0

products = [
    {
        "product": product,
        "category": data["category"],
        "units": data["units"],
        "revenue": round(data["revenue"], 2),
        "profit": round(data["profit"], 2),
        "margin": round(data["profit"] / data["revenue"], 3) if data["revenue"] else 0,
        "orders": data["orders"],
    }
    for product, data in by_product.items()
]
top_products = sorted(products, key=lambda item: (item["profit"], item["revenue"]), reverse=True)[:5]
slow_movers = sorted(products, key=lambda item: (item["units"], item["profit"]))[:3]
top_customers = sorted(
    [
        {"customer": customer, "revenue": round(data["revenue"], 2), "profit": round(data["profit"], 2), "orders": data["orders"]}
        for customer, data in by_customer.items()
    ],
    key=lambda item: item["revenue"],
    reverse=True,
)[:5]

recommendations = []
if top_products:
    recommendations.append(f"Feature {top_products[0]['product']} in next week's offer; it is the strongest profit driver.")
if slow_movers:
    recommendations.append(f"Review {slow_movers[0]['product']} before reordering; it has the weakest movement in this sample.")
if top_customers:
    recommendations.append(f"Follow up with {top_customers[0]['customer']} first; they are the highest-value buyer in this batch.")

report_lines = [
    "Sales Analysis Demo",
    "",
    f"Rows analyzed: {len(rows)}",
    f"Total units sold: {total_units}",
    f"Total revenue: {money(total_revenue)}",
    f"Estimated gross profit: {money(total_profit)}",
    f"Estimated gross margin: {margin:.1%}",
    "",
    "Top products:",
]
for item in top_products:
    report_lines.append(f"- {item['product']}: {item['units']} units, {money(item['revenue'])} revenue, {money(item['profit'])} profit")
report_lines.append("")
report_lines.append("Products to review before restocking:")
for item in slow_movers:
    report_lines.append(f"- {item['product']}: {item['units']} units, {money(item['profit'])} profit")
report_lines.append("")
report_lines.append("Recommended next actions:")
for item in recommendations:
    report_lines.append(f"- {item}")

result = {
    "report": "\n".join(report_lines),
    "kpis": {
        "rows": len(rows),
        "total_units": total_units,
        "total_revenue": round(total_revenue, 2),
        "gross_profit": round(total_profit, 2),
        "gross_margin": round(margin, 3),
    },
    "top_products": top_products,
    "slow_movers": slow_movers,
    "top_customers": top_customers,
    "recommendations": recommendations,
}
