"""Draw the receipt images and their labels: the eval set for the receipt reader.

Each receipt is made from numbers chosen here, so its label (order number, date, total) is right by
construction. The formats vary the way paper receipts do: four ways to print the order number and the date,
discounts, sales tax by store, and payment lines that look like totals (cash tendered, change). Each image is
rotated a little and softened, like a photo of a receipt.

Run once, from the repo root (Pillow is needed only here):  uv run --with pillow python examples/receipts/tools/make_receipts.py
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parents[1]
INBOX = HERE / "receipts" / "inbox"
DATASET = HERE / "evals" / "read.jsonl"
COUNT = 24
WIDTH = 420

STORES = [  # (location lines, tax label, rate)
    (("Portland Flagship", "1200 NW Glisan St, Portland OR"), "No sales tax (OR)", 0.0),
    (("Seattle", "410 Pine St, Seattle WA"), "Sales tax WA 10.35%", 0.1035),
    (("Boise", "88 W Main St, Boise ID"), "ID sales tax 6%", 0.06),
    (("Denver", "1550 Wynkoop St, Denver CO"), "Tax CO 8.81%", 0.0881),
    (("Bend", "640 NW Arizona Ave, Bend OR"), "No sales tax (OR)", 0.0),
]
ITEMS = [
    ("Trail Runner 2 Shoes", 129.00), ("Merino Hiking Socks", 18.00), ("Ridgeline 2P Tent", 289.00),
    ("Summit 30L Daypack", 94.50), ("Camp Stove Mini", 49.95), ("Fuel Canister 230g", 6.95),
    ("Headlamp 350 lm", 34.00), ("Squeeze Water Filter", 39.95), ("Down Jacket W M", 219.00),
    ("Rain Shell M L", 159.00), ("Trekking Poles (pair)", 79.00), ("Dry Bag 10L", 22.50),
    ("Insulated Mug", 24.00), ("First Aid Kit", 29.95), ("Sleeping Pad R4", 119.00),
]
CARDS = ["VISA ****4417", "Mastercard ****1111", "AMEX ****3005", "Debit ****8823"]


def money(value: float) -> str:
    return f"{value:,.2f}"


def receipt(rng: random.Random, number: int) -> tuple[list[tuple[str, str]], dict]:
    """The lines of one receipt, as (left, right) text pairs, and its label."""
    (place, address), tax_label, rate = rng.choice(STORES)
    order = f"FH-{rng.randint(100200, 109999)}"
    when = datetime(2026, 9, 1, 9, 0) + timedelta(days=rng.randint(0, 50), minutes=rng.randint(0, 600))
    while 1 <= when.day <= 12:  # keep month/day orders unambiguous: a day over 12 can't be a month
        when += timedelta(days=13)
    picked = rng.sample(ITEMS, rng.randint(1, 4))
    lines = [(item, qty, price) for (item, price) in picked for qty in [rng.choice([1, 1, 1, 2, 3])]]
    subtotal = round(sum(qty * price for _, qty, price in lines), 2)
    discount_kind = rng.choice([None, None, "member", "coupon"])
    discount = round(subtotal * 0.10, 2) if discount_kind == "member" else 15.0 if discount_kind == "coupon" and subtotal > 60 else 0.0
    tax = round((subtotal - discount) * rate, 2)
    total = round(subtotal - discount + tax, 2)

    order_line = rng.choice([f"Order: {order}", f"Order # {order.replace('-', '')}", f"ORDER NO. {order.lower()}",
                             f"Online order {order} (store pickup)"])
    date_line = rng.choice([when.strftime("%m/%d/%Y  %H:%M"), when.strftime("%d %b %Y  %I:%M %p"),
                            when.strftime("%Y-%m-%d %H:%M"), when.strftime("%b %d, %Y").replace(" 0", " ")])
    out = [("FERNHILL SUPPLY CO.", ""), (place, ""), (address, ""), ("", ""), (order_line, ""), (date_line, ""),
           (f"Cashier: {rng.choice(['Dana', 'Luis', 'Priya', 'Sam', 'Okafor'])}", ""), ("-" * 34, "")]
    for item, qty, price in lines:
        out.append((item if qty == 1 else f"{item} {qty} x {money(price)}", money(qty * price)))
    out += [("-" * 34, ""), ("Subtotal", money(subtotal))]
    if discount_kind == "member":
        out.append(("Member 10% off", f"-{money(discount)}"))
    elif discount:
        out.append(("Coupon FALL15", f"-{money(discount)}"))
    out.append((tax_label, money(tax)) if rate else (tax_label, "0.00"))
    out.append((rng.choice(["TOTAL", "Amount due", "Total paid"]), money(total)))
    if rng.random() < 0.3:  # cash: two more amounts that aren't the total
        tendered = float(int(total // 20 + 1) * 20)
        out += [("Cash tendered", money(tendered)), ("Change", money(tendered - total))]
    else:
        out.append((rng.choice(CARDS), money(total)))
    if discount:
        out.append((f"You saved {money(discount)}!", ""))
    out += [("", ""), (f"Rewards points earned: {int(total)}", ""), ("Thank you for shopping at Fernhill!", "")]
    return out, {"order_id": order, "date": when.strftime("%Y-%m-%d"), "total": total}


def draw(lines: list[tuple[str, str]], rng: random.Random) -> Image.Image:
    font, header = ImageFont.load_default(size=17), ImageFont.load_default(size=23)
    height = 40 + 26 * len(lines)
    page = Image.new("L", (WIDTH, height), 252)
    pen = ImageDraw.Draw(page)
    y = 20
    for i, (left, right) in enumerate(lines):
        face = header if i == 0 else font
        if i < 3:  # the store's name and address are centered
            pen.text(((WIDTH - pen.textlength(left, font=face)) / 2, y), left, fill=20, font=face)
        else:
            pen.text((22, y), left, fill=25, font=face)
            if right:
                pen.text((WIDTH - 22 - pen.textlength(right, font=face), y), right, fill=25, font=face)
        y += 26 if i else 32
    photo = page.rotate(rng.uniform(-2.5, 2.5), resample=Image.Resampling.BICUBIC, expand=True, fillcolor=205)
    return photo.filter(ImageFilter.GaussianBlur(0.5))


def main() -> None:
    rng = random.Random(20261025)
    INBOX.mkdir(parents=True, exist_ok=True)
    cases = []
    for number in range(1, COUNT + 1):
        lines, label = receipt(rng, number)
        name = f"r{number:02d}.png"
        draw(lines, rng).save(INBOX / name, optimize=True)
        cases.append({"id": f"read-{number:02d}", "input": {"receipt": name}, "expected": label})
    DATASET.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    print(f"{COUNT} receipts in {INBOX}, labels in {DATASET}")


if __name__ == "__main__":
    main()
