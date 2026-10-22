import base64
import logging
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import requests
from werkzeug.security import generate_password_hash

import carrier_check
from carrier_check import CheckError, verdict

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
STUCK = {"carrier": "usps", "tracking_number": "9400100000000000000001", "tier": "critical", "days": 12,
         "last_movement_at": "2026-10-10T09:00:00+00:00", "label_created_at": "2026-10-08T09:00:00+00:00"}
ENV = {"USPS_CLIENT_ID": "usps-id-secret", "USPS_CLIENT_SECRET": "usps-pass-secret", "DHL_API_KEY": "dhl-key-secret"}


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def usps(category, *events, number=STUCK["tracking_number"]):
    return {"trackingNumber": number, "statusCategory": category, "status": category,
            "trackingEvents": [{"eventType": text, "GMTTimestamp": at, "eventCode": code} for text, at, code in events]}


def dhl(code, *events, number="420331019261290400221977085286"):
    return {"shipments": [{"id": number, "service": "ecommerce", "status": {"statusCode": code},
                           "events": [{"timestamp": at, "statusCode": c, "description": d} for d, at, c in events]}]}


class FakeCarriers:
    """Answers USPS token, USPS tracking and DHL tracking; records every request."""

    def __init__(self, usps_body=None, dhl_body=None, status=200, headers=None):
        self.usps_body, self.dhl_body, self.status, self.headers, self.calls = usps_body, dhl_body, status, headers, []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/oauth2/v3/token"):
            return FakeResponse(200, {"access_token": "token-secret", "expires_in": 3600})
        body = self.usps_body if "usps" in url else self.dhl_body
        return FakeResponse(self.status, body, self.headers)


class CarrierTestCase(unittest.TestCase):
    def setUp(self):
        carrier_check._token.update(value=None, expires=0.0)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def check(self, fake, row=STUCK):
        with patch("carrier_check.requests.request", fake):
            return verdict(row, now=NOW)


class UspsVerdictTests(CarrierTestCase):
    def test_delivered_needs_a_delivery_scan_with_a_time(self):
        result = self.check(FakeCarriers(usps("Delivered", ("Delivered, In/At Mailbox", "2026-10-15T18:02:00Z", "01"),
                                               ("Arrived at USPS Facility", "2026-10-11T08:00:00Z", "10"))))
        self.assertEqual(result["verdict"], "delivered")
        self.assertEqual(result["carrier_delivered_at"], "2026-10-15T18:02:00+00:00")
        self.assertEqual(result["carrier_event"], "Delivered, In/At Mailbox")

    def test_delivered_status_without_a_dated_delivery_event_is_not_trusted(self):
        result = self.check(FakeCarriers(usps("Delivered", ("Arrived at USPS Facility", "2026-10-11T08:00:00Z", "10"),
                                               ("Delivered, In/At Mailbox", None, "01"))))
        self.assertEqual(result["verdict"], "unverified")

    def test_delivery_back_to_the_sender_is_not_delivered(self):
        result = self.check(FakeCarriers(usps("Delivered", ("Delivered, To Original Sender", "2026-10-18T15:00:00Z", "01"))))
        self.assertNotEqual(result["verdict"], "delivered")

    def test_newer_physical_scan_is_moved(self):
        result = self.check(FakeCarriers(usps("In Transit", ("Departed USPS Regional Facility", "2026-10-20T03:00:00Z", "L1"),
                                               ("Arrived at USPS Facility", "2026-10-10T09:00:00Z", "10"))))
        self.assertEqual(result["verdict"], "moved")
        self.assertEqual(result["carrier_last_scan_at"], "2026-10-20T03:00:00+00:00")

    def test_events_usps_writes_without_a_scan_never_count_as_movement(self):
        for text in ("In Transit to Next Facility", "Arriving Late", "Reminder to Schedule Redelivery of your item",
                     "Shipping Label Created, USPS Awaiting Item", "Pre-Shipment Info Sent to USPS"):
            with self.subTest(text=text):
                result = self.check(FakeCarriers(usps("In Transit", (text, "2026-10-21T03:00:00Z", "NT"),
                                                       ("Arrived at USPS Facility", "2026-10-10T09:00:00Z", "10"))))
                self.assertEqual(result["verdict"], "confirmed")

    def test_nothing_newer_is_confirmed(self):
        result = self.check(FakeCarriers(usps("In Transit", ("Arrived at USPS Facility", "2026-10-10T09:00:00Z", "10"))))
        self.assertEqual(result["verdict"], "confirmed")
        self.assertEqual(result["carrier_event_at"], "2026-10-10T09:00:00+00:00")

    def test_label_only_shipment_is_confirmed_never_scanned(self):
        row = dict(STUCK, last_movement_at=None)
        result = self.check(FakeCarriers(usps("Pre-Shipment", ("Shipping Label Created", "2026-10-08T09:00:00Z", "GX"))), row)
        self.assertEqual(result["verdict"], "confirmed")
        self.assertIsNone(result["carrier_last_scan_at"])

    def test_reply_for_another_tracking_number_is_unverified(self):
        result = self.check(FakeCarriers(usps("Delivered", ("Delivered", "2026-10-15T18:02:00Z", "01"), number="9400999")))
        self.assertEqual(result["verdict"], "unverified")
        self.assertIn("different tracking number", result["reason"])

    def test_unknown_status_is_unverified(self):
        result = self.check(FakeCarriers(usps("", ("Arrived at USPS Facility", "2026-10-10T09:00:00Z", "10"))))
        self.assertEqual(result["verdict"], "unverified")

    def test_failures_are_unverified(self):
        cases = {404: "not found", 401: "API key", 403: "API key", 429: "rate limit", 500: "error (500)"}
        for status, reason in cases.items():
            with self.subTest(status=status):
                carrier_check._token.update(value=None, expires=0.0)
                result = self.check(FakeCarriers(usps("Delivered"), status=status))
                self.assertEqual(result["verdict"], "unverified")
                self.assertIn(reason, result["reason"])

    def test_timeout_and_unreadable_answers_are_unverified(self):
        def timeout(method, url, **kwargs):
            raise requests.Timeout()
        self.assertEqual(self.check(timeout)["verdict"], "unverified")
        for body in (ValueError("not json"), ["list"], {"trackingNumber": STUCK["tracking_number"],
                                                        "statusCategory": "In Transit",
                                                        "trackingEvents": [{"eventType": "x", "GMTTimestamp": "garbage"}]}):
            with self.subTest(body=body):
                carrier_check._token.update(value=None, expires=0.0)
                self.assertEqual(self.check(FakeCarriers(body))["verdict"], "unverified")

    def test_rate_limit_keeps_retry_after(self):
        result = self.check(FakeCarriers(usps("Delivered"), status=429, headers={"Retry-After": "120"}))
        self.assertEqual(result["retry_after"], 120)

    def test_missing_keys_are_unverified_and_send_nothing(self):
        fake = FakeCarriers(usps("Delivered"))
        with patch.dict(os.environ, {"USPS_CLIENT_ID": ""}):
            result = self.check(fake)
        self.assertEqual(result["verdict"], "unverified")
        self.assertEqual(fake.calls, [])


class DhlVerdictTests(CarrierTestCase):
    ROW = dict(STUCK, carrier="dhl-ecommerce", tracking_number="420331019261290400221977085286")

    def test_delivered(self):
        result = self.check(FakeCarriers(dhl_body=dhl("delivered", ("DELIVERED", "2026-10-16T14:00:00Z", "delivered"))), self.ROW)
        self.assertEqual(result["verdict"], "delivered")

    def test_newer_transit_scan_is_moved_and_pre_transit_is_not(self):
        moved = self.check(FakeCarriers(dhl_body=dhl("transit", ("Arrived at DHL eCommerce facility", "2026-10-19T14:00:00Z", "transit"))), self.ROW)
        self.assertEqual(moved["verdict"], "moved")
        label = self.check(FakeCarriers(dhl_body=dhl("pre-transit", ("DHL eCommerce has received electronic notification", "2026-10-19T14:00:00Z", "pre-transit"))), self.ROW)
        self.assertEqual(label["verdict"], "confirmed")

    def test_wrong_or_ambiguous_shipment_is_unverified(self):
        other = self.check(FakeCarriers(dhl_body=dhl("delivered", ("DELIVERED", "2026-10-16T14:00:00Z", "delivered"), number="999")), self.ROW)
        self.assertEqual(other["verdict"], "unverified")
        body = dhl("delivered", ("DELIVERED", "2026-10-16T14:00:00Z", "delivered"))
        body["shipments"] *= 2
        self.assertEqual(self.check(FakeCarriers(dhl_body=body), self.ROW)["verdict"], "unverified")

    def test_unknown_status_code_is_unverified(self):
        result = self.check(FakeCarriers(dhl_body=dhl("lost-in-space")), self.ROW)
        self.assertEqual(result["verdict"], "unverified")


class GuardTests(CarrierTestCase):
    def test_only_tracking_reads_and_the_token_request_are_allowed(self):
        for method, url in (("POST", "https://apis.usps.com/tracking/v3/tracking/1/notifications"),
                            ("DELETE", "https://apis.usps.com/labels/v3/label/1"),
                            ("GET", "https://apis.usps.com/labels/v3/label/1"),
                            ("POST", "https://api-eu.dhl.com/track/shipments"),
                            ("GET", "https://evil.example/track/shipments")):
            with self.subTest(method=method, url=url), patch("carrier_check.requests.request") as sent:
                with self.assertRaises(carrier_check.ReadOnlyViolation):
                    carrier_check._send(method, url)
                sent.assert_not_called()

    def test_requests_sent_for_a_check(self):
        fake = FakeCarriers(usps("In Transit", ("Arrived at USPS Facility", "2026-10-10T09:00:00Z", "10")))
        self.check(fake)
        self.assertEqual([(m, u.split("?")[0]) for m, u, _ in fake.calls],
                         [("POST", "https://apis.usps.com/oauth2/v3/token"),
                          ("GET", "https://apis.usps.com/tracking/v3/tracking/" + STUCK["tracking_number"])])

    def test_odd_tracking_numbers_are_never_sent(self):
        fake = FakeCarriers(usps("Delivered"))
        for number in ("", "94001/../labels", "9400?x=1", "9400 0000"):
            self.assertEqual(self.check(fake, dict(STUCK, tracking_number=number))["verdict"], "unverified")
        self.assertEqual(fake.calls, [])

    def test_cirro_and_unknown_carriers_send_nothing(self):
        fake = FakeCarriers()
        for carrier in ("cirro-ecommerce", "ups"):
            result = self.check(fake, dict(STUCK, carrier=carrier))
            self.assertEqual(result["verdict"], "unverified")
        self.assertEqual(fake.calls, [])

    def test_no_keys_or_tracking_numbers_in_logs(self):
        def boom(number):
            raise RuntimeError(f"failure for {number} with {ENV['USPS_CLIENT_SECRET']}")
        with self.assertLogs("carrier_check", logging.WARNING) as logs, patch.dict(carrier_check.LOOKUPS, {"usps": boom}):
            result = verdict(STUCK, now=NOW)
        self.assertEqual(result["verdict"], "unverified")
        text = "\n".join(logs.output)
        for secret in (*ENV.values(), STUCK["tracking_number"], "token-secret"):
            self.assertNotIn(secret, text)


class ApplyTests(unittest.TestCase):
    def test_delivered_leaves_the_stuck_queue(self):
        row = carrier_check.apply(dict(STUCK), {"verdict": "delivered", "carrier_last_scan_at": None})
        self.assertEqual(row["tier"], "delivered")
        self.assertTrue(row["ssk_out_of_date"])

    def test_moved_recomputes_the_age_from_the_carrier_scan(self):
        recent = datetime.now(timezone.utc).isoformat()
        row = carrier_check.apply(dict(STUCK, carrier_status="in_transit"), {"verdict": "moved", "carrier_last_scan_at": recent})
        self.assertEqual(row["tier"], "monitoring")
        self.assertEqual(row["days"], 0)

    def test_confirmed_and_unverified_keep_the_shipment_flagged(self):
        for name in ("confirmed", "unverified"):
            row = carrier_check.apply(dict(STUCK), {"verdict": name})
            self.assertEqual(row["tier"], "critical")
            self.assertEqual(row["carrier_check"]["verdict"], name)


class BackgroundTests(CarrierTestCase):
    def setUp(self):
        super().setUp()
        carrier_check._results.clear()
        carrier_check._wanted.clear()
        carrier_check._next_ok.clear()

    def test_flagged_rows_are_queued_and_checked_oldest_first(self):
        rows = [dict(STUCK, tracking_number="A", days=6, tier="watch"), dict(STUCK, tracking_number="B", days=20),
                dict(STUCK, tracking_number="C", days=2, tier="monitoring")]
        with patch.object(carrier_check, "_start"):
            carrier_check.results_for(rows)
        self.assertEqual(set(carrier_check._wanted), {("usps", "A"), ("usps", "B")})
        job, _ = carrier_check._next_job()
        self.assertEqual(job[0], ("usps", "B"))
        job, wake = carrier_check._next_job()
        self.assertIsNone(job)  # the next USPS lookup waits for the quota interval
        self.assertIsNotNone(wake)

    def test_limits_are_split_across_workers(self):
        with patch.dict(os.environ, {"USPS_LOOKUPS_PER_HOUR": "120", "WEB_CONCURRENCY": "2"}):
            self.assertEqual(carrier_check.interval("usps"), 60)
        with patch.dict(os.environ, {"DHL_LOOKUPS_PER_DAY": "250", "WEB_CONCURRENCY": "1"}):
            self.assertAlmostEqual(carrier_check.interval("dhl-ecommerce"), 345.6)

    def test_unsupported_carrier_answers_at_once(self):
        with patch.object(carrier_check, "_start") as start:
            out = carrier_check.results_for([dict(STUCK, carrier="cirro-ecommerce", tracking_number="CR1")])
        self.assertEqual(out[("cirro-ecommerce", "CR1")]["verdict"], "unverified")
        start.assert_not_called()

    def test_carrier_without_a_key_answers_at_once(self):
        with patch.dict(os.environ, {"DHL_API_KEY": ""}), patch.object(carrier_check, "_start") as start:
            out = carrier_check.results_for([dict(STUCK, carrier="dhl-ecommerce", tracking_number="D1")])
        self.assertEqual(out[("dhl-ecommerce", "D1")]["reason"], "DHL API key not set")
        start.assert_not_called()


class ProgressTests(unittest.TestCase):
    def test_only_real_lookups_count_as_checked(self):
        rows = [dict(STUCK, carrier_check={"verdict": "confirmed", "looked_up": True}),
                dict(STUCK, carrier_check={"verdict": "unverified", "looked_up": True}),
                dict(STUCK, carrier_check={"verdict": "unverified", "looked_up": False}),
                dict(STUCK), dict(STUCK, tier="monitoring")]
        self.assertEqual(carrier_check.progress(rows), {"checked": 2, "flagged": 4, "cannot_check": 1, "delivered": 0,
                                                        "moved": 0, "confirmed": 1, "unverified": 1})


class WorkspaceTests(unittest.TestCase):
    def test_workspace_applies_verdicts(self):
        from app import app
        rows = [{"client_id": "muravai", "carrier": "usps", "tracking_number": "T1", "carrier_status": "in_transit",
                 "last_movement_at": "2026-09-01T00:00:00+00:00", "label_created_at": "2026-08-30T00:00:00+00:00",
                 "fulfillment_status": "In carrier network", "events": []}]
        env = {"SSK_API_ENABLED": "true", "CARRIER_CHECK_ENABLED": "true", "WORKSPACE_USER": "ops",
               "WORKSPACE_PASSWORD_HASH": generate_password_hash("pw"), "SECRET_KEY": "x", "APP_MODE": "demo"}
        store = {"id": "muravai", "rows": rows, "truncated": False, "skipped_statuses": [],
                 "environment": "production", "fetched_at": NOW.isoformat()}
        delivered = {"verdict": "delivered", "reason": "Carrier shows it delivered", "checked_at": NOW.isoformat(),
                     "looked_up": True}
        auth = {"Authorization": "Basic " + base64.b64encode(b"ops:pw").decode()}
        with patch.dict(os.environ, env), patch("ssk_source.queue_snapshot", return_value=[store]), \
                patch("carrier_check.results_for", return_value={("usps", "T1"): delivered}):
            body = app.test_client().get("/api/workspace", headers=auth).get_json()
        self.assertEqual(body["shipments"][0]["tier"], "delivered")
        self.assertEqual(body["carrier_checks"]["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
