"""Accuracy sign-off for the carrier check (Issue #31). Read-only.

  python carrier_check_cli.py sample --per-carrier 50 --out signoff.csv
      Reads the No Movement queue from ShipSidekick, checks a sample of flagged shipments per carrier
      with the carrier, and writes one row each with the app's verdict and the carrier's website link.
      A person opens each link and fills in website_verdict (delivered / moved / confirmed) and
      website_latest_event.

  python carrier_check_cli.py score signoff.csv
      Compares the filled-in file with the app. Release needs 100% agreement on every carrier;
      each disagreement is listed so it can be explained and fixed.

The output holds order and tracking numbers: keep it out of the repository.
"""
import argparse
import csv
import random
import sys
import time

import carrier_check
import ssk_source
from tracking import CLIENTS, classify

COLUMNS = ["client_id", "order_number", "carrier", "tracking_number", "tracking_url", "ssk_status",
           "ssk_last_scan", "app_verdict", "app_reason", "app_latest_event", "app_latest_event_at", "checked_at",
           "website_verdict", "website_latest_event"]
WEBSITE_VERDICTS = {"delivered", "moved", "confirmed"}


def sample(per_carrier, out, seed):
    stores = ssk_source.read_shipment_stores([c["id"] for c in CLIENTS], ssk_source.lookback_days())
    rows = [classify(r) for s in stores for r in s.get("rows", [])]
    flagged = [r for r in rows if r["tier"] in carrier_check.FLAGGED and r["carrier"] in carrier_check.LOOKUPS]
    rng = random.Random(seed)
    picked = []
    for carrier in sorted(carrier_check.LOOKUPS):
        pool = [r for r in flagged if r["carrier"] == carrier]
        picked += rng.sample(pool, min(per_carrier, len(pool)))
    with open(out, "w", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS)
        writer.writeheader()
        for number, row in enumerate(picked, 1):
            result = carrier_check.verdict(row)
            writer.writerow({"client_id": row["client_id"], "order_number": row["order_number"],
                             "carrier": row["carrier"], "tracking_number": row["tracking_number"],
                             "tracking_url": row.get("tracking_url"), "ssk_status": row["carrier_status"],
                             "ssk_last_scan": row.get("last_movement_at"), "app_verdict": result["verdict"],
                             "app_reason": result["reason"], "app_latest_event": result["carrier_event"],
                             "app_latest_event_at": result["carrier_event_at"], "checked_at": result["checked_at"],
                             "website_verdict": "", "website_latest_event": ""})
            print(f"{number}/{len(picked)} checked", file=sys.stderr)
            if number < len(picked):
                time.sleep(carrier_check.interval(row["carrier"]))  # stay inside the carrier's quota
    print(f"Wrote {len(picked)} shipments to {out}")


def score(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    blank = [r for r in rows if r["website_verdict"].strip().lower() not in WEBSITE_VERDICTS]
    if blank:
        print(f"{len(blank)} rows have no website_verdict (delivered / moved / confirmed); fill them in first.")
        return 1
    passed = True
    for carrier in sorted({r["carrier"] for r in rows}):
        mine = [r for r in rows if r["carrier"] == carrier]
        wrong = [r for r in mine if r["app_verdict"] != r["website_verdict"].strip().lower()]
        print(f"{carrier}: {len(mine) - len(wrong)}/{len(mine)} agree")
        for r in wrong:
            print(f"  {r['tracking_number']}: app {r['app_verdict']} ({r['app_reason']}) "
                  f"vs website {r['website_verdict']} ({r['website_latest_event']})")
        passed = passed and not wrong
    print("PASS: 100% agreement" if passed else "FAIL: disagreements must be explained and fixed before release")
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--per-carrier", type=int, default=50)
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=31)
    c = sub.add_parser("score")
    c.add_argument("path")
    args = parser.parse_args(argv)
    if args.command == "sample":
        sample(args.per_carrier, args.out, args.seed)
        return 0
    return score(args.path)


if __name__ == "__main__":
    sys.exit(main())
