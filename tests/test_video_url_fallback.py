"""Direct video URLs precede the video-cover fallback."""

import unittest
from unittest.mock import Mock

from src.api.icourse import ICourseClient


POSTER = "https://media.example.edu/Pic/2026/11/13/file_500k.webp"
VIDEO = "https://media.example.edu/2026/11/13/file_1920_1080.mp4"


class VideoUrlFallbackTest(unittest.TestCase):
    def setUp(self):
        self.client = ICourseClient.__new__(ICourseClient)
        self.client.base_url = "https://icourse.fudan.edu.cn"
        self.client.vpn = Mock()
        self.client.get_sub_info = Mock(return_value={"now": 123})
        self.client.get_sub_detail = Mock(return_value={})
        self.client.sign_video_url = Mock(
            side_effect=lambda url, now=None: url + "?signed=1"
        )

    def test_direct_info_url_precedes_detail_and_poster(self):
        self.client.get_sub_info.return_value = {
            "now": 123,
            "content": {
                "playback": {"url": "https://media.example.edu/direct.mp4"},
                "webp_path": POSTER,
            },
        }

        result = self.client.get_video_url("course", "lecture")

        self.assertEqual(result, "https://media.example.edu/direct.mp4?signed=1")
        self.client.get_sub_detail.assert_not_called()
        self.client.vpn.get.assert_not_called()

    def test_direct_detail_url_precedes_poster(self):
        self.client.get_sub_info.return_value = {
            "now": 123, "content": {"webp_path": POSTER}
        }
        self.client.get_sub_detail.return_value = {
            "content": {"playback": {"url": "https://media.example.edu/detail.mp4"}}
        }

        result = self.client.get_video_url("course", "lecture")

        self.assertEqual(result, "https://media.example.edu/detail.mp4?signed=1")
        self.client.vpn.get.assert_not_called()

    def test_poster_url_is_derived_without_a_probe(self):
        self.client.get_sub_info.return_value = {
            "now": 123, "content": {"webp_path": POSTER}
        }

        result = self.client.get_video_url("course", "lecture")

        self.assertEqual(result, VIDEO + "?signed=1")
        self.client.sign_video_url.assert_called_once_with(VIDEO, now=123)
        self.client.vpn.get.assert_not_called()

    def test_detail_poster_is_used_when_info_is_unavailable(self):
        self.client.get_sub_info.side_effect = RuntimeError("unavailable")
        self.client.get_sub_detail.return_value = {
            "content": {"webp_path": POSTER}
        }
        self.assertEqual(
            self.client.get_video_url("course", "lecture"), VIDEO + "?signed=1"
        )
        self.client.vpn.get.assert_not_called()

    def test_unrecognized_poster_returns_none_for_subtitle_fallback(self):
        self.client.get_sub_info.return_value = {
            "now": 123,
            "content": {"webp_path": "https://media.example.edu/Pic/file.webp"},
        }

        self.assertIsNone(self.client.get_video_url("course", "lecture"))
        self.client.vpn.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
