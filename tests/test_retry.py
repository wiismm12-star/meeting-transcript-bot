from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from transcript_bot.retry import request_with_retry


class RequestRetryTests(unittest.TestCase):
    def test_retries_a_transient_status_then_returns_the_successful_response(self) -> None:
        unavailable = MagicMock(status_code=503)
        success = MagicMock(status_code=200)
        request = MagicMock(side_effect=[unavailable, success])

        with patch("transcript_bot.retry.time.sleep") as sleep:
            result = request_with_retry(request, action="測試請求")

        self.assertIs(result, success)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_retries_a_connection_error_then_returns_success(self) -> None:
        request = MagicMock(side_effect=[httpx.ConnectError("offline"), MagicMock(status_code=200)])

        with patch("transcript_bot.retry.time.sleep") as sleep:
            result = request_with_retry(request, action="測試請求")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_does_not_retry_a_non_transient_client_error(self) -> None:
        bad_request = MagicMock(status_code=400)
        request = MagicMock(return_value=bad_request)

        with patch("transcript_bot.retry.time.sleep") as sleep:
            result = request_with_retry(request, action="測試請求")

        self.assertIs(result, bad_request)
        request.assert_called_once_with()
        sleep.assert_not_called()

    def test_does_not_retry_an_exhausted_quota_response(self) -> None:
        quota_exhausted = MagicMock(status_code=429, text="insufficient_quota")
        request = MagicMock(return_value=quota_exhausted)

        with patch("transcript_bot.retry.time.sleep") as sleep:
            result = request_with_retry(request, action="測試請求")

        self.assertIs(result, quota_exhausted)
        request.assert_called_once_with()
        sleep.assert_not_called()

    def test_does_not_repeat_non_idempotent_request_after_connection_error(self) -> None:
        request = MagicMock(side_effect=httpx.ReadTimeout("response lost"))

        with patch("transcript_bot.retry.time.sleep") as sleep:
            with self.assertRaises(httpx.ReadTimeout):
                request_with_retry(request, action="建立工作", retry_request_errors=False)

        request.assert_called_once_with()
        sleep.assert_not_called()
