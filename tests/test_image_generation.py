from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from image_generation import (  # noqa: E402
    DashScopeImageGenerator,
    ImageGenerationError,
    is_image_generation_request,
)


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int = -1) -> bytes:
        return self.payload if maximum < 0 else self.payload[:maximum]


class FakeURLopener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout: float):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


class ImageGenerationTests(unittest.TestCase):
    def test_detects_explicit_generation_without_hijacking_questions(self) -> None:
        positives = [
            "帮我生成一张雨夜里的城市插画",
            "请画一只趴在窗边的小猫",
            "设计一个极简的蓝色图标",
            "Create an image of a river at dawn",
        ]
        negatives = [
            "这张图片里面有什么？",
            "为什么没有生成图片？",
            "帮我总结这份 PDF",
        ]
        self.assertTrue(all(is_image_generation_request(item) for item in positives))
        self.assertFalse(any(is_image_generation_request(item) for item in negatives))

    def test_calls_china_native_endpoint_and_persists_returned_image(self) -> None:
        provider_payload = json.dumps(
            {
                "request_id": "request-1234",
                "output": {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {
                                        "image": (
                                            "https://dashscope-result-bj.oss-cn-beijing."
                                            "aliyuncs.com/result.png?Expires=1"
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
            }
        ).encode("utf-8")
        image_payload = b"\x89PNG\r\n\x1a\nmock-image"
        opener = FakeURLopener(
            [FakeResponse(provider_payload), FakeResponse(image_payload)]
        )
        generator = DashScopeImageGenerator(
            environment={
                "DASHSCOPE_API_KEY": "secret-test-key",
                "DASHSCOPE_BASE_URL": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
            },
            urlopen=opener,
        )
        generated = generator.generate("生成一张晨雾中的河流图片")
        self.assertEqual(generated.payload, image_payload)
        self.assertEqual(generated.model, "qwen-image-3.0-pro")
        self.assertTrue(generated.filename.endswith(".png"))
        api_request = opener.requests[0][0]
        image_request = opener.requests[1][0]
        self.assertEqual(
            api_request.full_url,
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
            "multimodal-generation/generation",
        )
        self.assertIn("secret-test-key", api_request.get_header("Authorization"))
        self.assertIsNone(image_request.get_header("Authorization"))

    def test_rejects_untrusted_provider_image_url(self) -> None:
        response = json.dumps(
            {
                "output": {
                    "choices": [
                        {"message": {"content": [{"image": "https://example.com/a.png"}]}}
                    ]
                }
            }
        ).encode("utf-8")
        generator = DashScopeImageGenerator(
            environment={"DASHSCOPE_API_KEY": "test-key"},
            urlopen=FakeURLopener([FakeResponse(response)]),
        )
        with self.assertRaises(ImageGenerationError):
            generator.generate("生成一张图片")


if __name__ == "__main__":
    unittest.main()
