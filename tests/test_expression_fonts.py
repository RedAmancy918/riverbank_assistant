import unittest

from expression_display_persistent import split_font_runs, uses_cjk_font


class ExpressionFontFallbackTests(unittest.TestCase):
    def test_latin_numbers_and_symbols_use_the_latin_font(self) -> None:
        self.assertFalse(uses_cjk_font("R"))
        self.assertFalse(uses_cjk_font("3"))
        self.assertFalse(uses_cjk_font("·"))
        self.assertEqual(
            split_font_runs("Wi-Fi · Edge OS 0.26.4"),
            [(False, "Wi-Fi · Edge OS 0.26.4")],
        )

    def test_chinese_and_full_width_punctuation_stay_on_the_cjk_font(self) -> None:
        self.assertTrue(uses_cjk_font("系"))
        self.assertTrue(uses_cjk_font("（"))
        self.assertEqual(split_font_runs("系统（正常）"), [(True, "系统（正常）")])

    def test_mixed_status_text_is_split_without_losing_characters(self) -> None:
        source = "已开启 · RiverBank Edge 0.26.4 · 自检 22/22"
        runs = split_font_runs(source)
        self.assertEqual("".join(value for _is_cjk, value in runs), source)
        self.assertEqual(
            runs,
            [
                (True, "已开启"),
                (False, " · RiverBank Edge 0.26.4 · "),
                (True, "自检"),
                (False, " 22/22"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
