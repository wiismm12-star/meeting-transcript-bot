from __future__ import annotations

import unittest

from transcript_bot.formatting import clean_text


class FormattingTests(unittest.TestCase):
    def test_removes_artificial_spaces_between_chinese_tokens(self) -> None:
        self.assertEqual(clean_text("風雲 榜 今年 要 舉辦 第18 屆"), "風雲榜今年要舉辦第18屆")

    def test_corrects_confirmed_escalator_recognition_error(self) -> None:
        self.assertEqual(clean_text("請搭乘首扶梯上樓"), "請搭乘手扶梯上樓")


if __name__ == "__main__":
    unittest.main()
