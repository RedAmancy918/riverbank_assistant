from datetime import date
import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "expression-ui"
sys.path.insert(0, str(APP_DIR))

from flight_search import (  # noqa: E402
    FlightQuote,
    FlightSearchResult,
    _FlightLabelParser,
    extract_flight_route,
    format_spoken_result,
    is_flight_price_request,
)


class FlightSearchTests(unittest.TestCase):
    def test_flight_intent_and_route_with_normal_transcript(self) -> None:
        text = "帮我查深圳飞厦门的机票近一周最便宜的价格"
        self.assertTrue(is_flight_price_request(text))
        self.assertEqual(extract_flight_route(text), ("深圳", "厦门"))

    def test_route_repairs_common_whisper_city_error(self) -> None:
        text = "把我查下从深圳到下门的机票接下来一周最低的价格"
        self.assertEqual(extract_flight_route(text), ("深圳", "厦门"))

    def test_real_whisper_traditional_transcript_is_routed(self) -> None:
        text = "把我踩下從深圳到下門的機票進一周最低的價格"
        self.assertTrue(is_flight_price_request(text))
        self.assertEqual(extract_flight_route(text), ("深圳", "厦门"))

    def test_accessible_label_parser_and_spoken_result(self) -> None:
        parser = _FlightLabelParser()
        parser.feed(
            '<div aria-label="起价：1,830 人民币。 深航航班，直飞。 选择航班"></div>'
        )
        self.assertEqual(
            parser.labels,
            ["起价：1,830 人民币。 深航航班，直飞。 选择航班"],
        )
        result = FlightSearchResult(
            origin="深圳",
            destination="厦门",
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 31),
            quotes=(
                FlightQuote(
                    date(2026, 8, 31),
                    1830,
                    "深航航班，直飞。",
                    "https://example.test",
                ),
            ),
            failed_dates=(),
            queried_at=__import__("datetime").datetime(2026, 8, 24, 20, 58),
        )
        spoken = format_spoken_result(result)
        self.assertIn("8月31日的1830元", spoken)
        self.assertIn("Google Flights", spoken)


if __name__ == "__main__":
    unittest.main()
