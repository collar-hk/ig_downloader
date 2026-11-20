"""Extraction tests. These do not call Instagram."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ig_media import (
    MediaAsset,
    dedupe_media_files,
    parse_html_payloads,
    post_assets_from_payloads,
    save_assets,
    story_assets_from_payloads,
)
from story_cache import StoryUnavailable, parse_story_target


def _photo(media_id: int, url: str = "https://cdn.example/photo.jpg") -> dict:
    return {
        "pk": media_id,
        "taken_at": 1_763_600_000 + media_id,
        "media_type": 1,
        "image_versions2": {
            "candidates": [
                {"url": url + "?w=150", "width": 150, "height": 150},
                {"url": url, "width": 1080, "height": 1920},
            ]
        },
    }


def _video(media_id: int, url: str = "https://cdn.example/video.mp4") -> dict:
    return {
        "pk": media_id,
        "taken_at": 1_763_600_000 + media_id,
        "media_type": 2,
        "video_versions": [
            {"url": url + "?low", "width": 480, "height": 854},
            {"url": url, "width": 1080, "height": 1920},
        ],
        "image_versions2": {"candidates": [{"url": "https://cdn.example/cover.jpg", "width": 320, "height": 568}]},
    }


STORY_PAGE = {
    "data": {
        "xdt_api__v1__feed__reels_media": {
            "reels_media": [
                {
                    "id": "999",
                    "reel_type": "user_reel",
                    "user": {"username": "ada", "pk": 999},
                    "items": [_photo(11), _video(12), _photo(13)],
                },
                {
                    "id": "highlight:42",
                    "reel_type": "highlight_reel",
                    "title": "Trip",
                    "user": {"username": "ada", "pk": 999},
                    "items": [_photo(80, "https://cdn.example/highlight.jpg")],
                },
                {
                    "id": "1000",
                    "user": {"username": "other", "pk": 1000},
                    "items": [_photo(70, "https://cdn.example/other.jpg")],
                },
            ]
        }
    }
}

POST_PAGE = {
    "data": {
        "xig_polaris_media": {
            "code": "ABC123xyz",
            "user": {"username": "ada"},
            "carousel_media_count": 3,
            "carousel_media": [
                _photo(1, "https://cdn.example/slide1.jpg"),
                _video(2, "https://cdn.example/slide2.mp4"),
                _photo(3, "https://cdn.example/slide3.jpg"),
            ],
        }
    }
}


class ExtractTests(unittest.TestCase):
    def test_story_keeps_photos_and_videos_and_skips_other_reels(self) -> None:
        assets = story_assets_from_payloads([STORY_PAGE], "Ada", None)
        self.assertEqual([asset.media_id for asset in assets], ["11", "12", "13"])
        self.assertEqual([asset.suffix for asset in assets], [".jpg", ".mp4", ".jpg"])
        self.assertEqual(assets[0].url, "https://cdn.example/photo.jpg")
        self.assertEqual(assets[1].url, "https://cdn.example/video.mp4")
        self.assertEqual(assets[0].kind, "GraphStoryImage")
        self.assertEqual(assets[1].kind, "GraphStoryVideo")

    def test_story_media_id_filters_to_one_item(self) -> None:
        assets = story_assets_from_payloads([STORY_PAGE], "ada", "12")
        self.assertEqual([asset.media_id for asset in assets], ["12"])

    def test_post_carousel_includes_every_slide(self) -> None:
        assets, expected = post_assets_from_payloads([POST_PAGE], "ABC123xyz")
        self.assertEqual(expected, 3)
        self.assertEqual([asset.suffix for asset in assets], [".jpg", ".mp4", ".jpg"])
        self.assertEqual([asset.media_id for asset in assets], ["1", "2", "3"])

    def test_post_reports_declared_count_when_children_are_truncated(self) -> None:
        truncated = {
            "code": "SHORT1",
            "carousel_media_count": 4,
            "carousel_media": [_photo(1)],
        }
        assets, expected = post_assets_from_payloads([truncated], "SHORT1")
        self.assertEqual(len(assets), 1)
        self.assertEqual(expected, 4)

    def test_html_script_and_marker_are_both_read(self) -> None:
        html = (
            '<html><script type="application/json">'
            '{"code":"FROMJS","image_versions2":{"candidates":[{"url":"https://cdn.example/a.jpg","width":10,"height":10}]},'
            '"media_type":1,"pk":5,"taken_at":10}'
            "</script>"
            '<div>shortcode_media":{"code":"FROMMARKER","display_url":"https://cdn.example/b.jpg","pk":6}</div>'
            "</html>"
        )
        payloads = parse_html_payloads(html)
        self.assertGreaterEqual(len(payloads), 2)
        from_js, _expected = post_assets_from_payloads(payloads, "FROMJS")
        from_marker, _expected = post_assets_from_payloads(payloads, "FROMMARKER")
        self.assertEqual(from_js[0].url, "https://cdn.example/a.jpg")
        self.assertEqual(from_marker[0].url, "https://cdn.example/b.jpg")

    def test_parse_story_target(self) -> None:
        self.assertEqual(
            parse_story_target("see https://www.instagram.com/stories/Ada/12345/?img_index=1"),
            ("ada", "12345"),
        )
        self.assertEqual(
            parse_story_target("https://instagram.com/stories/ada/"),
            ("ada", None),
        )
        self.assertIsNone(parse_story_target("https://www.instagram.com/stories/highlights/99/"))
        self.assertIsNone(parse_story_target("https://www.instagram.com/p/ABC/"))
        with self.assertRaises(StoryUnavailable):
            parse_story_target("https://www.instagram.com/stories/not-valid!/")

    def test_save_keeps_successful_files_when_one_url_fails(self) -> None:
        class Response:
            def __init__(self, body: bytes, status: int, content_type: str) -> None:
                self._body = body
                self.status_code = status
                self.headers = {"Content-Type": content_type}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def iter_content(self, chunk_size: int = 0):
                yield self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Session:
            def get(self, url: str, **kwargs):
                if "bad" in url:
                    return Response(b"nope", 404, "text/plain")
                return Response(b"\xff\xd8" + b"jpeg-bytes" * 8, 200, "image/jpeg")

        assets = [
            MediaAsset("1", "https://cdn.example/ok.jpg", ".jpg", 1_763_600_000, "ada", "GraphStoryImage"),
            MediaAsset("2", "https://cdn.example/bad.jpg", ".jpg", 1_763_600_000, "ada", "GraphStoryImage"),
        ]
        with TemporaryDirectory() as tmp:
            saved, failures = save_assets(Session(), assets, tmp, referer="https://www.instagram.com/")
            names = sorted(path.name for path in Path(tmp).iterdir())
            self.assertEqual(saved, 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(names), 1)
            self.assertIn("GraphStoryImage_1.jpg", names[0])

    def test_dedupe_removes_identical_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.jpg"
            second = Path(tmp) / "b.jpg"
            payload = b"\xff\xd8same-bytes"
            first.write_bytes(payload)
            second.write_bytes(payload)
            kept = dedupe_media_files([first, second])
            self.assertEqual(kept, [first])
            self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
