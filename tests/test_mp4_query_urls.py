import unittest
from unittest.mock import Mock

from src.api.icourse import ICourseClient


class PlaybackURLTests(unittest.TestCase):
    def test_mp4_path_is_recognized_in_each_sub_info_source(self):
        for suffix in ("", "?token=example", "#playback"):
            url = "https://media.invalid/lecture.mp4" + suffix
            sources = (
                {"video_list": {"1": {"preview_url": url}}},
                {"playurl": {"1": url}},
                {"content": {"playback": {"url": url}}},
            )
            for source in sources:
                with self.subTest(source=source):
                    client = ICourseClient(Mock())
                    client.get_sub_info = Mock(return_value=source)
                    client.get_sub_detail = Mock(return_value={})
                    client.sign_video_url = Mock(return_value="signed")
                    self.assertEqual(client.get_video_url("course", "lecture"), "signed")
                    client.sign_video_url.assert_called_once_with(url, now=None)
                    client.get_sub_detail.assert_not_called()

    def test_non_mp4_path_still_uses_detail_fallback(self):
        client = ICourseClient(Mock())
        client.get_sub_info = Mock(return_value={
            "playurl": {"1": "https://media.invalid/lecture.txt?file=.mp4"},
        })
        url = "https://media.invalid/fallback.mp4"
        client.get_sub_detail = Mock(return_value={"content": {"playback": {"url": url}}})
        client.sign_video_url = Mock(return_value="signed")
        self.assertEqual(client.get_video_url("course", "lecture"), "signed")
        client.sign_video_url.assert_called_once_with(url, now=None)


if __name__ == "__main__":
    unittest.main()
