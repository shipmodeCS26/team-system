"""Draft a client's daily Slack inventory update from displayed Sheet values.

Only restates what the Dashboard (and Incoming Stocks, when available) shows.
It never posts anywhere: the workspace shows the text for a person to review and copy.
"""
from __future__ import annotations

from incoming import FLAGS as INCOMING_FLAGS, whole_units


def _number(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _cover_text(cover: str) -> str:
    number = _number(cover)
    if number is None:
        return f"days of cover: {cover or 'not shown'}"
    if number < 1:
        return "less than 1 day of cover"
    return f"{cover} days of cover"


def _product_line(row: dict) -> str:
    remaining = row.get("remaining", "")
    stock = f"{remaining} units" if _number(remaining) is not None else f"remaining: {remaining or 'not shown'}"
    status = f" ({row['status']})" if row.get("status") else ""
    return f"• {row['product']}: {stock}, {_cover_text(row.get('cover', ''))}{status}"


def review_reasons(source: dict) -> list[str]:
    reasons = ["Report marked REVIEW"] if source.get("report_status", "").upper() == "REVIEW" else []
    return reasons + list(source.get("warnings") or [])


def incoming_lines(incoming: dict | None) -> tuple[list[str], list[str]]:
    """Client-facing lines for open incoming shipments, and the POs held back for internal review.
    Shared by the daily update and the EOD report so both word incoming shipments the same way."""
    listed, held_back = [], []
    if not (incoming and incoming.get("available") and incoming.get("shipments")):
        return listed, held_back
    for group in incoming["shipments"]:
        usable = [line for line in group["lines"] if "sku_unverified" not in line["flags"]
                  and whole_units(line.get("units")) is not None]
        if len(usable) < len(group["lines"]) and group["po"] not in held_back:
            held_back.append(group["po"])  # internal review item, not client-facing (also when only partly left out)
        if not usable:
            continue
        # A blank product name falls back to the verified SKU rather than hiding the line.
        items = "; ".join(f"{line.get('product') or line['sku']} {line['units']}" for line in usable)
        first = group["lines"][0]
        details = "; ".join(text for text in (first.get("where"), f"Expected in Miami: {first['expected_date']}"
                                              if first.get("expected_date") else "") if text)
        # Only flags of lines that are in the text; held-back lines stay internal.
        flags = [INCOMING_FLAGS[flag] for flag in sorted({f for line in usable for f in line["flags"]})
                 if flag in INCOMING_FLAGS]
        listed.append(f"• {group['po']}: {items}" + (f". {details}" if details else "")
                      + (f". Needs attention: {', '.join(flags)}" if flags else "") + ".")
    return listed, held_back


def build_update(client_name: str, source: dict, incoming: dict | None = None) -> dict:
    rows = source.get("rows") or []
    lines = []
    reasons = review_reasons(source)
    if reasons:
        lines += [f"DRAFT (source under review): {'; '.join(reasons)}. Remove this line only after checking the Sheet.", ""]
    lines.append(f"Hi @channel! Here is the {client_name} Inventory Update as of {source.get('as_of') or 'an unknown date'}.")
    lines.append("")
    lines += [_product_line(row) for row in rows] or ["• No products are listed on the Dashboard."]

    out_now = [row["product"] for row in rows
               if (_number(row.get("remaining", "")) is not None and _number(row["remaining"]) <= 0)]
    in_stock = [(cover, row) for row in rows
                if (cover := _number(row.get("cover", ""))) is not None
                and (_number(row.get("remaining", "")) or 0) > 0]
    lines.append("")
    if out_now:
        lines.append(f"Out of stock now: {', '.join(out_now)}.")
    if in_stock:
        cover, row = min(in_stock, key=lambda item: item[0])
        demand = f" at the Sheet's current daily demand of {row['demand']}/day" if row.get("demand") else ""
        lines.append(f"Earliest to run out: {row['product']} ({_cover_text(row['cover'])}{demand}). This is an estimate.")

    listed, held_back = incoming_lines(incoming)
    if listed:
        lines += ["", "Incoming, not yet in stock:", *listed]

    lines += ["", "Please let us know of any incoming inventory so we can reflect it in planning. Thank you!"]
    return {"text": "\n".join(lines), "draft": bool(reasons), "held_back": held_back,
            "incoming_truncated": bool(incoming and incoming.get("truncated"))}
