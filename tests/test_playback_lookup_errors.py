import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.api.icourse import ICourseClient
from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime.scheduler import AudioDownloader, _PendingSpawn


class PlaybackLookupTests(unittest.TestCase):
    def test_failed_queries_are_not_reported_as_missing_video(self):
        for first_fails, second_fails in ((True, False), (False, True), (True, True)):
            with self.subTest(first=first_fails, second=second_fails):
                client = ICourseClient(Mock())
                client.get_sub_info = Mock(return_value={})
                client.get_sub_detail = Mock(return_value={})
                if first_fails:
                    client.get_sub_info.side_effect = OSError("https://private.invalid/?token=secret")
                if second_fails:
                    client.get_sub_detail.side_effect = OSError("https://private.invalid/?token=secret")
                with self.assertRaises(RuntimeError) as caught:
                    client.get_video_url("course", "lecture")
                self.assertNotIn("secret", str(caught.exception))

    def test_successful_empty_queries_still_return_none(self):
        client = ICourseClient(Mock())
        client.get_sub_info = Mock(return_value={})
        client.get_sub_detail = Mock(return_value={})
        self.assertIsNone(client.get_video_url("course", "lecture"))

    def test_detail_fallback_can_recover_a_failed_primary_query(self):
        client = ICourseClient(Mock())
        client.get_sub_info = Mock(side_effect=OSError("unavailable"))
        client.get_sub_detail = Mock(return_value={"content": {"playback": {"url": "video.mp4"}}})
        client.sign_video_url = Mock(return_value="signed")
        self.assertEqual(client.get_video_url("course", "lecture"), "signed")


class AudioPrefetchErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.downloader = AudioDownloader(self.temp, max_concurrent=1)
        self.addCleanup(self.downloader.shutdown)
        self.client = Mock()
        self.pending = _PendingSpawn()
        self.downloader._active["lecture"] = self.pending

    def test_prefetch_exception_reaches_consumer_without_private_details(self):
        self.client.get_video_url.side_effect = OSError("https://private.invalid/?token=secret")
        self.downloader._spawn_when_ready(self.client, "course", "lecture", self.pending)
        with self.assertRaises(RuntimeError) as caught:
            self.downloader.get("lecture", timeout=0)
        self.assertNotIn("secret", str(caught.exception))
        self.downloader.release("lecture")
        self.assertIsNone(self.downloader.get("lecture", timeout=0))

    def test_no_url_still_returns_none(self):
        self.client.get_video_url.return_value = None
        self.downloader._spawn_when_ready(self.client, "course", "lecture", self.pending)
        self.assertIsNone(self.downloader.get("lecture", timeout=0))

    @patch("src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT", False)
    @patch("src.runtime.scheduler.subprocess.Popen", side_effect=OSError("ffmpeg missing"))
    def test_spawn_failure_records_transcribe_error_instead_of_no_video(self, popen):
        self.client.get_stream_params.return_value = ("url", "headers")
        self.downloader._spawn_when_ready(self.client, "course", "lecture", self.pending)
        db = Database(":memory:")
        self.addCleanup(db.conn.close)
        db.upsert_course("course", "Course", "Teacher")
        db.insert_lecture("lecture", "course", "Lecture", "")
        transcriber = Mock()
        runner = LectureRunner(self.client, db, SimpleNamespace(audio_downloader=self.downloader),
                               transcriber, Mock(), Mock())
        self.assertEqual(runner._get_transcript(None, "course", "lecture"), (None, None))
        self.assertEqual(db.get_lecture("lecture")["error_stage"], "transcribe")
        self.assertEqual(db.get_lecture("lecture")["error_count"], 1)
        transcriber.transcribe_tail.assert_not_called()
        self.assertIsNone(self.downloader.get("lecture", timeout=0))


if __name__ == "__main__":
    unittest.main()
