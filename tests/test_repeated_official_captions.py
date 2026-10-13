import unittest
from unittest.mock import Mock, patch

from src.pipeline.lecture_runner import LectureRunner


def segments(texts):
    return [
        {"text": text, "start_ms": i * 30_000, "end_ms": (i + 1) * 30_000}
        for i, text in enumerate(texts)
    ]


class OfficialCaptionTests(unittest.TestCase):
    def test_repeated_single_character_noise_is_rejected(self):
        # Continuous timestamps alone do not establish usable lecture content.
        captions = segments(["的。"] * 287 + ["正常的课程句子"] * 10)
        self.assertFalse(LectureRunner._official_transcript_usable(captions))

    def test_punctuation_only_is_rejected(self):
        self.assertFalse(LectureRunner._official_transcript_usable(segments(["...", "！", " "])))

    def test_normal_captions_and_varied_short_answers_are_retained(self):
        for texts in (["课程内容", "下一句"], ["甲", "乙"] * 20, ["相同的课程句子"] * 30):
            with self.subTest(texts=texts[:2]):
                self.assertTrue(LectureRunner._official_transcript_usable(segments(texts)))

    def test_small_sample_is_not_rejected_as_repetitive(self):
        self.assertTrue(LectureRunner._official_transcript_usable(segments(["甲"] * 19)))

    def test_existing_gap_and_tail_checks_are_retained(self):
        captions = segments(["第一句", "第二句"])
        self.assertFalse(LectureRunner._official_transcript_usable(captions, duration_hint_s=3600))
        captions[1]["start_ms"] = 30 * 60_000
        captions[1]["end_ms"] = 31 * 60_000
        self.assertFalse(LectureRunner._official_transcript_usable(captions))

    @patch("src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT", True)
    def test_unusable_captions_fall_back_to_audio(self):
        client, db, scheduler, transcriber = Mock(), Mock(), Mock(), Mock()
        client.get_transcript_segments.return_value = segments(["的"] * 30)
        db.get_lecture.return_value = None
        db.get_max_ppt_created_sec.return_value = 0
        transcriber.transcribe_tail.return_value = ("ASR course content", [])
        runner = LectureRunner(client, db, scheduler, transcriber, Mock(), Mock())
        self.assertTrue(runner._needs_audio("lecture"))
        self.assertEqual(runner._get_transcript(None, "course", "lecture"), ("ASR course content", []))
        transcriber.transcribe_tail.assert_called_once()
        db.update_transcript.assert_called_once_with("lecture", "ASR course content")


if __name__ == "__main__":
    unittest.main()
