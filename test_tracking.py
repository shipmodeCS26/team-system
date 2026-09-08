import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import app
from tracking import classify, parse_csv, sample_shipments, tracker_update


class AgingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

    def row(self, days, **kwargs):
        return dict(carrier_status="pre_transit", fulfillment_status="fulfilled",
                    shipped_at=(self.now-timedelta(days=days)).isoformat(), **kwargs)

    def test_exact_thresholds_and_no_ten_day_cutoff(self):
        for days, tier in [(4.999,"monitoring"),(5,"watch"),(6.999,"watch"),(7,"urgent"),(9.999,"urgent"),(10,"critical"),(100,"critical")]:
            with self.subTest(days=days):
                self.assertEqual(classify(self.row(days), self.now)["tier"], tier)

    def test_fulfilled_label_is_not_delivered(self):
        self.assertEqual(classify(self.row(12), self.now)["tier"], "critical")

    def test_clock_uses_physical_scan_not_ship_date(self):
        row=self.row(12,last_movement_at=(self.now-timedelta(days=2)).isoformat())
        row["carrier_status"]="in_transit"
        self.assertEqual(classify(row,self.now)["days"],2)
        self.assertEqual(classify(row,self.now)["tier"],"monitoring")

    def test_delivered_and_cancelled_excluded(self):
        for status in ["delivered","cancelled"]:
            row=self.row(30);row["carrier_status"]=status
            self.assertEqual(classify(row,self.now)["tier"],status)

    def test_missing_scan_timestamp_is_not_invented(self):
        row=self.row(30);row["carrier_status"]="in_transit"
        self.assertEqual(classify(row,self.now)["tier"],"data_gap")
        self.assertEqual(classify({"carrier_status":"pre_transit"},self.now)["tier"],"data_gap")

    def test_timezone_equivalence(self):
        a={"carrier_status":"pre_transit","shipped_at":"2026-09-03T08:00:00-04:00"}
        self.assertEqual(classify(a,self.now)["days"],5)

    def test_demo_is_five_clients_and_synthetic(self):
        rows=sample_shipments()
        self.assertEqual(len(set(r["client_id"] for r in rows)),5)
        self.assertTrue(all(r["tracking_number"].startswith("DEMO-") for r in rows))


class ImportTests(unittest.TestCase):
    def test_native_export_uses_label_date_not_scan(self):
        rows=parse_csv("Tracking Code,Created Date,Organization,Order Name,Carrier,Tracking Status,Voided\nDEMO-1,8/1/26,ClarityMD,#1,USPS,pre_transit,No\n","claritymd")
        self.assertIsNone(rows[0]["last_movement_at"])
        self.assertIsNone(rows[0]["shipped_at"])
        self.assertTrue(rows[0]["label_created_at"].startswith("2026-08-01"))

    def test_cross_client_rejected(self):
        with self.assertRaisesRegex(ValueError,"not the selected client"):
            parse_csv("Tracking Code,Carrier,Organization\nDEMO-1,USPS,Muravai\n","claritymd")

    def test_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError,"repeats"):
            parse_csv("tracking_number,carrier\nDEMO-1,USPS\nDEMO-1,USPS\n","claritymd")

    def test_multiple_packages_not_silently_lost(self):
        with self.assertRaisesRegex(ValueError,"one row per package"):
            parse_csv("Tracking Code,Carrier,Additional Tracking Codes\nDEMO-1,USPS,DEMO-2\n","claritymd")

    def test_invalid_and_future_dates_rejected(self):
        for value in ["yesterday","2999-01-01"]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_csv("tracking_number,carrier,shipped_at\nDEMO-1,USPS,"+value+"\n","claritymd")

    def test_voided_label_excluded(self):
        row=parse_csv("Tracking Code,Carrier,Tracking Status,Voided\nDEMO-1,USPS,pre_transit,Yes\n","claritymd")[0]
        self.assertEqual(classify(row)["tier"],"cancelled")


class WebhookTests(unittest.TestCase):
    def event(self):
        return {"id":"test-event","description":"tracker.updated","mode":"production","created_at":"2026-08-10T12:00:00Z","result":{"tracking_code":"DEMO-1","carrier":"USPS","status":"in_transit","tracking_details":[{"status":"in_transit","description":"Arrived at carrier facility","datetime":"2026-08-01T12:00:00Z"},{"status":"in_transit","description":"Shipping label created","datetime":"2026-08-10T12:00:00Z"}]}}

    def test_label_update_does_not_reset_movement(self):
        row=tracker_update(self.event())
        self.assertTrue(row["last_movement_at"].startswith("2026-08-01"))

    def test_informational_failure_not_movement(self):
        event=self.event();event["result"]["tracking_details"]=[{"status":"failure","description":"Delivery exception","datetime":"2026-08-09T12:00:00Z"}]
        self.assertIsNone(tracker_update(event)["last_movement_at"])

    def test_unsupported_shape_rejected(self):
        event=self.event();event["result"]={"trackingCode":"DEMO-1"}
        with self.assertRaises(ValueError):tracker_update(event)

    def test_invalid_signature_never_touches_database(self):
        with patch.dict(os.environ,{"APP_MODE":"live","DATABASE_URL":"unused","WORKSPACE_USER":"owner","WORKSPACE_PASSWORD_HASH":"unused","SECRET_KEY":"test-key","SSK_WEBHOOK_SECRET_CLARITYMD":"test-webhook"}):
            with patch("app.db",side_effect=AssertionError("database must not be accessed")):
                response=app.test_client().post("/api/shipsidekick/claritymd",json=self.event(),headers={"X-SSK-Signature":"bad"})
                self.assertEqual(response.status_code,401)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client=app.test_client()

    def test_demo_page_and_assets(self):
        for path in ["/","/static/workspace.js","/static/workspace.css","/api/health","/api/template.csv"]:
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.get("/api/workspace").json["mode"],"demo")

    def test_demo_cannot_accept_real_data(self):
        self.assertEqual(self.client.post("/api/import",json={"csv":"private"}).status_code,409)
        self.assertEqual(self.client.patch("/api/shipments/1",json={"notes":"private"}).status_code,409)
        self.assertEqual(self.client.post("/api/shipsidekick/claritymd",json={}).status_code,503)

    def test_live_fails_closed_without_configuration(self):
        with patch.dict(os.environ,{"APP_MODE":"live"},clear=True):
            self.assertEqual(self.client.get("/api/workspace").status_code,503)

    def test_live_requires_authentication(self):
        with patch.dict(os.environ,{"APP_MODE":"live","DATABASE_URL":"unused","WORKSPACE_USER":"owner","WORKSPACE_PASSWORD_HASH":"unused","SECRET_KEY":"test"}):
            self.assertEqual(self.client.get("/api/workspace").status_code,401)


if __name__=="__main__":
    unittest.main()
