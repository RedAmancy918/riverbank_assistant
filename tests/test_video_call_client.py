import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "expression-ui"))

from video_call_client import fetch_remote_frame


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


class VideoCallClientTests(unittest.TestCase):
    def test_remote_frame_is_center_cropped_to_target(self) -> None:
        image = Image.new("RGB", (160, 90), (12, 34, 56))
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG")
        with patch(
            "video_call_client.urllib.request.urlopen",
            return_value=FakeResponse(encoded.getvalue()),
        ):
            payload = fetch_remote_frame((80, 80))
        self.assertEqual(len(payload), 80 * 80 * 3)


if __name__ == "__main__":
    unittest.main()
