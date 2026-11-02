import unittest
from unittest.mock import patch

from mexc_futures_live import MexcFuturesLive, MexcLiveError


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
