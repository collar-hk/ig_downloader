"""Caption and filename helpers for Telegram uploads."""

from __future__ import annotations

import unittest
from pathlib import Path

from bot import _instagram_caption, _is_reel_link


class CaptionTests(unittest.TestCase):
    def test_reel_caption_keeps_underscored_username(self) -> None:
        path = Path("2024-05-01_my_user_GraphVideo_998877.mp4")
        self.assertEqual(
            _instagram_caption(path, story=False, reel=True),
            "#20240501 #my_userIGReels",
        )

    def test_story_and_post_captions(self) -> None:
        path = Path("2024-05-01_ada_GraphStoryImage_12.jpg")
        self.assertEqual(
            _instagram_caption(path, story=True, reel=False),
            "#20240501 #adaIGS",
        )
        path = Path("2024-05-01_ada_GraphImage_12.jpg")
        self.assertEqual(
            _instagram_caption(path, story=False, reel=False),
            "#20240501 #adaIG",
        )

    def test_reel_link_detection(self) -> None:
        self.assertTrue(_is_reel_link("https://www.instagram.com/reel/AbC/"))
        self.assertTrue(_is_reel_link("https://www.instagram.com/reels/AbC/"))
        self.assertFalse(_is_reel_link("https://www.instagram.com/p/AbC/"))


if __name__ == "__main__":
    unittest.main()
