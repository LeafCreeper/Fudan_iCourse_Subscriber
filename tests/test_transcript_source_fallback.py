"""Source selection shared by scheduled and manual lecture runs."""

import unittest
from unittest.mock import Mock, patch

from src.pipeline.lecture_runner import LectureRunner
from src.runtime import config


SEGMENTS = [
    {"start_ms": 0, "end_ms": 1000, "text": "第一句"},
    {"start_ms": 1000, "end_ms": 2000, "text": "第二句"},
]


class TranscriptSourceFallbackTest(unittest.TestCase):
    def setUp(self):
        self.runner = LectureRunner.__new__(LectureRunner)
        self.runner._client = Mock()
        self.runner._db = Mock()
        self.runner._scheduler = Mock()
        self.runner._transcriber = Mock()
        self.runner._reporter = Mock()
        self.runner._official_cache = {}
        self.runner._db.get_max_ppt_created_sec.return_value = 0

    def test_video_first_uses_subtitles_when_video_url_is_missing(self):
        self.runner._scheduler.audio_downloader.get.return_value = None
        self.runner._client.get_transcript_segments.return_value = SEGMENTS

        with patch.object(config, "USE_OFFICIAL_TRANSCRIPT", False):
            transcript, segments = self.runner._get_transcript(None, "c", "s")

        self.assertEqual((transcript, segments), ("第一句 第二句", SEGMENTS))
        self.runner._db.update_transcript.assert_called_once_with(
            "s", "第一句 第二句"
        )
        self.runner._db.update_error.assert_not_called()
        self.runner._transcriber.transcribe_tail.assert_not_called()

    def test_video_first_keeps_asr_when_video_is_available(self):
        handle = self.runner._scheduler.audio_downloader.get.return_value
        self.runner._transcriber.transcribe_tail.return_value = (
            "ASR text", SEGMENTS
        )

        with patch.object(config, "USE_OFFICIAL_TRANSCRIPT", False):
            result = self.runner._get_transcript(None, "c", "s")

        self.assertEqual(result, ("ASR text", SEGMENTS))
        self.runner._transcriber.transcribe_tail.assert_called_once_with(
            handle.path, handle.process, handle.stderr_chunks
        )
        self.runner._client.get_transcript_segments.assert_not_called()

    def test_manual_preference_still_uses_subtitles_before_video(self):
        self.runner._client.get_transcript_segments.return_value = SEGMENTS

        with patch.object(config, "USE_OFFICIAL_TRANSCRIPT", True):
            transcript, segments = self.runner._get_transcript(None, "c", "s")

        self.assertEqual((transcript, segments), ("第一句 第二句", SEGMENTS))
        self.runner._scheduler.audio_downloader.schedule.assert_not_called()
        self.runner._transcriber.transcribe_tail.assert_not_called()

    def test_manual_preference_uses_asr_when_subtitles_are_missing(self):
        self.runner._client.get_transcript_segments.return_value = None
        self.runner._transcriber.transcribe_tail.return_value = (
            "ASR text", SEGMENTS
        )

        with patch.object(config, "USE_OFFICIAL_TRANSCRIPT", True):
            result = self.runner._get_transcript(None, "c", "s")

        self.assertEqual(result, ("ASR text", SEGMENTS))
        self.runner._scheduler.audio_downloader.schedule.assert_called_once()
        self.runner._transcriber.transcribe_tail.assert_called_once()

    def test_missing_video_and_incomplete_subtitles_remain_retryable(self):
        self.runner._scheduler.audio_downloader.get.return_value = None
        self.runner._client.get_transcript_segments.return_value = [
            {"start_ms": 21 * 60_000, "end_ms": 22 * 60_000, "text": "late"}
        ]

        with patch.object(config, "USE_OFFICIAL_TRANSCRIPT", False):
            result = self.runner._get_transcript(None, "c", "s")

        self.assertEqual(result, (None, None))
        self.runner._db.update_transcript.assert_not_called()
        self.runner._db.update_error.assert_called_once_with(
            "s", "no_video", "no playable video URL"
        )


if __name__ == "__main__":
    unittest.main()
