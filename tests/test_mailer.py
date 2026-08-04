from __future__ import annotations

import unittest

from transcript_bot.mailer import is_valid_email


class MailerTests(unittest.TestCase):
    def test_accepts_a_plain_email_address(self) -> None:
        self.assertTrue(is_valid_email("recipient@example.com"))

    def test_rejects_display_names_and_malformed_addresses(self) -> None:
        self.assertFalse(is_valid_email("Recipient <recipient@example.com>"))
        self.assertFalse(is_valid_email("not-an-email"))


if __name__ == "__main__":
    unittest.main()
