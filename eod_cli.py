"""Build one client's EOD report from files: `python eod_cli.py CLIENT --dashboard values.json [--csv export.csv]`.

`values.json` is the raw Dashboard!A1:S39 values (a list of rows, or {"values": [...]}) read from
that client's own Sheet. Writes <out>/<client>-eod.txt, -dashboard.png and -report.json. Never sends.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from dashboard_image import render_png
from eod_check import check
from eod_report import build_report
from inventory import parse_dashboard
from slack_draft import draft
from tracking import CLIENTS

NAMES = {client["id"]: client["name"] for client in CLIENTS}


def _json(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data.get("values", data) if isinstance(data, dict) else data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=sorted(NAMES))
    parser.add_argument("--dashboard", required=True, help="Dashboard!A1:S39 values JSON for this client only")
    parser.add_argument("--csv", help="ShipSidekick CSV export for this client")
    parser.add_argument("--no-shipments-confirmed", action="store_true",
                        help="The warehouse confirmed no shipments on the as-of date")
    parser.add_argument("--incoming", help="Parsed Incoming Stocks JSON; without it the report is held")
    parser.add_argument("--out", required=True, help="Output folder outside the repository (client data)")
    args = parser.parse_args(argv)

    source = parse_dashboard(_json(args.dashboard))
    rows = None
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    result = check(args.client, source, rows, csv_name=os.path.basename(args.csv or ""),
                   no_shipments_confirmed=args.no_shipments_confirmed)
    incoming = _json(args.incoming) if args.incoming else None
    # Without --incoming the Incoming section was never read: hold rather than say "None logged".
    report = build_report(args.client, NAMES[args.client], source, result, incoming,
                          None if args.incoming else "no Incoming Stocks file was given (--incoming).")
    slack = draft(args.client, report)
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, args.client)
    with open(f"{base}-eod.txt", "w", encoding="utf-8") as handle:
        handle.write(report["text"] + "\n")
    with open(f"{base}-dashboard.png", "wb") as handle:
        handle.write(render_png(NAMES[args.client], source, report["status"]))
    with open(f"{base}-report.json", "w", encoding="utf-8") as handle:
        json.dump({"report": report, "draft": slack}, handle, indent=2, default=str)
    print(report["text"])
    note = f"  ({slack['note']})" if report["ready_to_send"] and slack.get("note") else ""
    print(f"\nStatus: {report['status']}  Ready to send: {slack['ready_to_send']}{note}", file=sys.stderr)
    return 0 if slack["ready_to_send"] else 2


if __name__ == "__main__":
    sys.exit(main())
