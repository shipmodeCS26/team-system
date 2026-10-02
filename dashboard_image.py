"""Render a client's Dashboard as a PNG for the EOD report.

Drawn from the same parsed Dashboard read as the report text, so the picture and the
words always show the same values and as-of date. Values are drawn exactly as the Sheet
displays them; nothing is recalculated. No note markers or filter buttons are drawn.
"""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "static", "fonts")
LOGO = os.path.join(HERE, "static", "shipmode-logo.png")

BLACK, WHITE, NAVY = "#07080A", "#FFFFFF", "#1B263C"
SILVER, SURFACE, INK = "#D4D7D9", "#F5F6F7", "#1B263C"
STATUS = {"VERIFIED": "#1E8E4E", "REVIEW": "#C27A00", "INCOMPLETE": "#6B7280"}
ROW_STATUS = {"OUT OF STOCK": "#C62828", "REORDER NOW": "#C62828", "STOCK SUFFICIENT": "#1E8E4E"}
COLUMNS = (("product", "Product"), ("starting", "Starting"), ("shipped", "Shipped"),
           ("remaining", "Remaining"), ("demand", "Daily demand"), ("cover", "Days of cover"),
           ("status", "Status"), ("run_out", "Runs out"), ("order_by", "Order by"),
           ("suggested", "Suggested order"), ("incoming", "Incoming"))
OPTIONAL = {"run_out", "incoming"}
SCALE = 2


def _font(name: str, size: int):
    path = os.path.join(FONTS, name)
    try:
        return ImageFont.truetype(path, size * SCALE)
    except OSError:
        return ImageFont.load_default(size * SCALE)


def _width(draw, text, font) -> int:
    return int(draw.textlength(text, font=font))


def render_png(client_name: str, source: dict, status: str) -> bytes:
    body = _font("barlow-latin-400-normal.woff2", 15)
    bold = _font("barlow-latin-600-normal.woff2", 15)
    small = _font("barlow-latin-400-normal.woff2", 12)
    title = _font("barlow-condensed-latin-800-italic.woff2", 30)
    big = _font("barlow-condensed-latin-800-italic.woff2", 34)
    s = SCALE
    rows = source.get("rows") or []
    columns = [(key, label) for key, label in COLUMNS
               if key not in OPTIONAL or any(row.get(key) for row in rows)]

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pad = 12 * s
    widths = [max([_width(probe, label, bold)] + [_width(probe, str(row.get(key, "")), body) for row in rows])
              + 2 * pad for key, label in columns]
    margin = 24 * s
    width = max(sum(widths) + 2 * margin, 980 * s)
    row_h, head_h, band_h, kpi_h = 34 * s, 38 * s, 78 * s, 92 * s
    height = band_h + 20 * s + kpi_h + 20 * s + head_h + row_h * max(len(rows), 1) + 56 * s
    image = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(image)

    # Header band: logo, client, as-of date, data status (always a word, never color alone).
    draw.rectangle([0, 0, width, band_h], fill=BLACK)
    x = margin
    if os.path.exists(LOGO):
        logo = Image.open(LOGO).convert("RGBA")
        logo.thumbnail((56 * s, 56 * s))
        image.paste(logo, (x, (band_h - logo.height) // 2), logo)
        x += logo.width + 16 * s
    draw.text((x, band_h // 2), f"{client_name}  ·  Inventory Dashboard", font=title, fill=WHITE, anchor="lm")
    chip = f"  {status}  "
    chip_w = _width(draw, chip, bold)
    chip_x = width - margin - chip_w
    draw.rounded_rectangle([chip_x, band_h // 2 - 15 * s, chip_x + chip_w, band_h // 2 + 15 * s],
                           radius=6 * s, fill=STATUS.get(status, STATUS["INCOMPLETE"]))
    draw.text((chip_x + chip_w // 2, band_h // 2), chip.strip(), font=bold, fill=WHITE, anchor="mm")
    as_of = f"As of {source.get('as_of') or 'unknown date'}"
    draw.text((chip_x - 18 * s, band_h // 2), as_of, font=bold, fill=SILVER, anchor="rm")

    # Summary figures exactly as the Dashboard shows them.
    labels = source.get("summary_labels") or {}
    summary = source.get("summary") or {}
    keys = [("products", "Products tracked"), ("reorder", "Need ordering now"),
            ("out", "Out of stock"), ("on_hand", "Units on hand")]
    top = band_h + 20 * s
    tile_w = (width - 2 * margin - 3 * 12 * s) // 4
    for i, (key, fallback) in enumerate(keys):
        left = margin + i * (tile_w + 12 * s)
        draw.rectangle([left, top, left + tile_w, top + kpi_h], fill=SURFACE, outline=SILVER, width=s)
        draw.text((left + 16 * s, top + 14 * s), (labels.get(key) or fallback).upper(), font=small, fill=INK)
        draw.text((left + 16 * s, top + kpi_h - 14 * s), summary.get(key) or "—", font=big, fill=NAVY, anchor="ls")

    # Product table.
    top += kpi_h + 20 * s
    draw.rectangle([margin, top, width - margin, top + head_h], fill=NAVY)
    x = margin
    for (key, label), w in zip(columns, widths):
        anchor_x, anchor = (x + pad, "lm") if key in ("product", "status") else (x + w - pad, "rm")
        draw.text((anchor_x, top + head_h // 2), label, font=bold, fill=WHITE, anchor=anchor)
        x += w
    top += head_h
    for index, row in enumerate(rows):
        if index % 2:
            draw.rectangle([margin, top, width - margin, top + row_h], fill=SURFACE)
        x = margin
        for (key, _label), w in zip(columns, widths):
            text = str(row.get(key, ""))
            color = ROW_STATUS.get(text.upper(), "#C27A00") if key == "status" and text else INK
            if key in row.get("flags", []):
                color = "#C27A00"
            font = bold if key in ("product", "status", "remaining") else body
            anchor_x, anchor = (x + pad, "lm") if key in ("product", "status") else (x + w - pad, "rm")
            draw.text((anchor_x, top + row_h // 2), text, font=font, fill=color, anchor=anchor)
            x += w
        draw.line([margin, top + row_h, width - margin, top + row_h], fill=SILVER, width=s)
        top += row_h
    if not rows:
        draw.text((margin + pad, top + row_h // 2), "No products are listed on the Dashboard.",
                  font=body, fill=INK, anchor="lm")
        top += row_h

    footer = (f"Values exactly as shown on the {client_name} Sheet Dashboard"
              + (f", read {source['fetched_at'][:16].replace('T', ' ')} UTC" if source.get("fetched_at") else "")
              + ". Nothing recalculated.")
    draw.text((margin, top + 24 * s), footer, font=small, fill="#6B7280", anchor="lm")

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
