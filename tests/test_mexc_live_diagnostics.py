import unittest
from unittest.mock import patch

from mexc_futures_live import MexcFuturesLive, MexcLiveError
from mexc_futures import MexcFuturesReadOnlyClient, MexcAPIError
import urllib.error
import socket


class _FakeHeaders(dict):
    pass


class _FakeResponse:
    def __init__(self, body, content_type="text/html; charset=utf-8", status=200):
        self.body = body
        self.headers = _FakeHeaders({"Content-Type": content_type})
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body

    def getcode(self):
        return self.status


class InvalidJsonDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.client = MexcFuturesLive.__new__(MexcFuturesLive)
        self.client.access_key = "test-access-secret"
        self.client.secret_key = "test-private-secret"

    def _call_with_body(self, body, content_type="text/html; charset=utf-8"):
        response = _FakeResponse(body, content_type=content_type)
        with patch("mexc_futures_live.urllib.request.urlopen", return_value=response):
            return self.client._request("GET", "/api/v1/private/account/assets")

    def test_html_response_includes_safe_http_diagnostics(self):
        with self.assertRaises(MexcLiveError) as caught:
            self._call_with_body(
                b"<html><body>Gateway unavailable</body></html>"
            )
        message = str(caught.exception)
        self.assertIn("/api/v1/private/account/assets", message)
        self.assertIn("http_status=200", message)
        self.assertIn("content_type=text/html", message)
        self.assertIn("Gateway unavailable", message)
        self.assertNotIn(self.client.access_key, message)
        self.assertNotIn(self.client.secret_key, message)

    def test_valid_json_larger_than_eight_kilobytes_is_fully_parsed(self):
        # Simulate the account/assets endpoint returning a large Futures asset list.
        assets = [{"currency": f"COIN{i}", "availableBalance": "1"} for i in range(500)]
        body = __import__("json").dumps({"success": True, "code": 0, "data": assets}).encode()
        self.assertGreater(len(body), 8192)
        result = self._call_with_body(body, content_type="application/json")
        self.assertEqual(len(result), 500)
        self.assertEqual(result[-1]["currency"], "COIN499")

    def test_response_over_one_megabyte_is_rejected_safely(self):
        body = b'{"success":true,"code":0,"data":"' + (b"x" * (1_048_576 + 10)) + b'"}'
        with self.assertRaises(MexcLiveError) as caught:
            self._call_with_body(body, content_type="application/json")
        self.assertIn("response exceeds safety limit", str(caught.exception))
        self.assertNotIn(self.client.secret_key, str(caught.exception))

    def test_live_transport_error_includes_safe_underlying_reason(self):
        reason = f"temporary network failure {self.client.secret_key}"
        with patch(
            "mexc_futures_live.urllib.request.urlopen",
            side_effect=urllib.error.URLError(reason),
        ):
            with self.assertRaises(MexcLiveError) as caught:
                self.client._request("GET", "/api/v1/private/position/position_mode")
        message = str(caught.exception)
        self.assertIn("/api/v1/private/position/position_mode", message)
        self.assertIn("reason_type=str", message)
        self.assertIn("temporary network failure", message)
        self.assertNotIn(self.client.secret_key, message)

    def test_read_only_connection_check_reports_safe_transport_reason(self):
        client = MexcFuturesReadOnlyClient(
            access_key="test-access-secret",
            secret_key="test-private-secret",
        )
        with patch(
            "mexc_futures.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            with self.assertRaises(MexcAPIError) as caught:
                client.get_private("/api/v1/private/account/tiered_fee_rate/v2", {"symbol": "BTC_USDT"})
        message = str(caught.exception)
        self.assertIn("/api/v1/private/account/tiered_fee_rate/v2", message)
        self.assertIn("reason_type=str", message)
        self.assertIn("timed out", message)
        self.assertNotIn("test-private-secret", message)

    def test_dns_failure_exposes_errno_without_secrets(self):
        with patch(
            "mexc_futures_live.urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
        ):
            with self.assertRaises(MexcLiveError) as caught:
                self.client._request("GET", "/api/v1/private/position/position_mode")
        message = str(caught.exception)
        self.assertIn("reason_type=gaierror", message)
        self.assertIn("errno=-2", message)
        self.assertNotIn(self.client.secret_key, message)

    def test_empty_response_is_reported_without_crashing(self):
        with self.assertRaises(MexcLiveError) as caught:
            self._call_with_body(b"", content_type="application/json")
        message = str(caught.exception)
        self.assertIn("/api/v1/private/account/assets", message)
        self.assertIn("http_status=200", message)
        self.assertIn("content_type=application/json", message)
        self.assertIn("<empty response>", message)


if __name__ == "__main__":
    unittest.main()
