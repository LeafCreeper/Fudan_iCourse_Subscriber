import contextlib
import io
import traceback
import unittest
from unittest.mock import Mock

import requests

from src.api import icourse


class PlaybackLookupTests(unittest.TestCase):
    def setUp(self):
        self.vpn = Mock()
        self.client = icourse.ICourseClient(self.vpn)
        self.client.sign_video_url = Mock(return_value="signed-video")
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    @staticmethod
    def response(payload=None, code=0):
        return Mock(json=Mock(return_value={"code": code, "data": payload or {}}))

    def assert_lookup_error(self):
        with self.assertRaises(RuntimeError) as caught:
            self.client.get_video_url("course", "lecture")
        self.assertEqual(type(caught.exception).__name__, "PlaybackLookupError")
        self.client.sign_video_url.assert_not_called()
        return caught.exception

    def test_both_requests_fail_is_not_reported_as_no_video(self):
        self.vpn.get.side_effect = [
            requests.HTTPError("403: https://private.invalid/?token=secret-token"),
            requests.Timeout("response contains secret-response"),
        ]
        error = self.assert_lookup_error()
        diagnostic = "".join(traceback.format_exception(error)) + self.output.getvalue()
        self.assertIn("HTTPError", diagnostic)
        self.assertIn("Timeout", diagnostic)
        self.assertNotIn("secret-token", diagnostic)
        self.assertNotIn("secret-response", diagnostic)
        self.assertNotIn("private.invalid", diagnostic)
        self.assertEqual(self.vpn.get.call_count, 2)

    def test_failed_info_and_empty_detail_is_not_reported_as_no_video(self):
        self.vpn.get.side_effect = [requests.Timeout("private details"), self.response()]
        self.assert_lookup_error()

    def test_empty_info_and_failed_detail_is_not_reported_as_no_video(self):
        self.vpn.get.side_effect = [self.response(), requests.HTTPError("private details")]
        self.assert_lookup_error()

    def test_http_response_failure_is_not_reported_as_no_video(self):
        failed_response = self.response()
        failed_response.raise_for_status.side_effect = requests.HTTPError("secret-response")
        self.vpn.get.side_effect = [failed_response, self.response()]
        self.assert_lookup_error()

    def test_json_response_failure_is_not_reported_as_no_video(self):
        failed_response = self.response()
        failed_response.json.side_effect = ValueError("secret-response")
        self.vpn.get.side_effect = [failed_response, self.response()]
        self.assert_lookup_error()

    def test_api_error_is_not_reported_as_no_video(self):
        self.vpn.get.side_effect = [self.response(code=500), self.response()]
        self.assert_lookup_error()

    def test_two_successful_empty_responses_return_none(self):
        self.vpn.get.side_effect = [self.response(), self.response()]
        self.assertIsNone(self.client.get_video_url("course", "lecture"))
        self.client.sign_video_url.assert_not_called()

    def test_detail_video_still_works_after_info_failure(self):
        video_url = "https://media.invalid/video.mp4?token=example"
        self.vpn.get.side_effect = [
            requests.Timeout("private details"),
            self.response({"content": {"playback": {"url": video_url}}}),
        ]
        self.assertEqual(self.client.get_video_url("course", "lecture"), "signed-video")
        self.client.sign_video_url.assert_called_once_with(video_url, now=None)

    def test_video_list_accepts_query_parameters_and_keeps_precedence(self):
        video_url = "https://media.invalid/video.mp4?token=example"
        self.vpn.get.return_value = self.response({
            "now": "123",
            "video_list": {"primary": {"preview_url": video_url}},
            "playurl": {"alternate": "https://media.invalid/alternate.mp4"},
        })
        self.assertEqual(self.client.get_video_url("course", "lecture"), "signed-video")
        self.client.sign_video_url.assert_called_once_with(video_url, now=123)
        self.vpn.get.assert_called_once()

    def test_playurl_accepts_query_parameters(self):
        video_url = "https://media.invalid/video.mp4?token=example"
        self.vpn.get.return_value = self.response({"playurl": {"main": video_url}})
        self.assertEqual(self.client.get_video_url("course", "lecture"), "signed-video")
        self.client.sign_video_url.assert_called_once_with(video_url, now=None)
        self.vpn.get.assert_called_once()

    def test_existing_nested_source_accepts_query_parameters(self):
        video_url = "https://media.invalid/video.mp4?token=example"
        self.vpn.get.return_value = self.response({
            "content": {"playback": {"url": video_url}, "now": "456"},
        })
        self.assertEqual(self.client.get_video_url("course", "lecture"), "signed-video")
        self.client.sign_video_url.assert_called_once_with(video_url, now=456)
        self.vpn.get.assert_called_once()

    def test_query_mp4_suffix_does_not_turn_nonvideo_path_into_video(self):
        self.vpn.get.side_effect = [
            self.response({"video_list": {"main": {"preview_url": "https://media.invalid/page?file=.mp4"}}}),
            self.response(),
        ]
        self.assertIsNone(self.client.get_video_url("course", "lecture"))
        self.client.sign_video_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
