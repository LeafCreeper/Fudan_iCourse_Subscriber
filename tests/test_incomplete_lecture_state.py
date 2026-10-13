import unittest
from unittest.mock import Mock, patch

import main
from src.ai.transcriber import NoAudioStreamError
from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner


class IncompleteLectureTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.conn.close)
        self.db.upsert_course("course", "Course", "Teacher")
        self.lecture = {"sub_id": "1", "sub_title": "Lecture", "has_playback": True}
        self.db.insert_lecture("1", "course", "Lecture", "")
        self.client = Mock()
        self.client.get_course_detail.return_value = {
            "title": "Course", "teacher": "Teacher", "lectures": [self.lecture],
        }
        self.scheduler = Mock()
        self.transcriber = Mock()
        self.summarizer = Mock()
        self.runner = LectureRunner(self.client, self.db, self.scheduler,
                                    self.transcriber, self.summarizer, Mock())
        self.runner._ppt = Mock()
        self.enterContext(patch("src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT", False))
        self.enterContext(patch("main.config.COURSE_IDS", ["course"]))

    def test_empty_asr_is_pending_then_can_complete_on_a_later_run(self):
        self.transcriber.transcribe_tail.return_value = ("", [])
        self.assertIsNone(self.runner.run("course", "Course", self.lecture))
        row = self.db.get_lecture("1")
        self.assertIsNone(row["processed_at"])
        self.assertEqual(row["error_count"], 1)
        self.summarizer.summarize.assert_not_called()
        self.assertEqual(len(main._enumerate_lectures(self.client, self.db, Mock())), 1)

        self.transcriber.transcribe_tail.return_value = ("useful transcript", [])
        self.summarizer.summarize.return_value = ("summary", "model")
        self.assertEqual(self.runner.run("course", "Course", self.lecture), "summary")
        row = self.db.get_lecture("1")
        self.assertIsNotNone(row["processed_at"])
        self.assertEqual(row["error_count"], 0)
        self.assertEqual(main._enumerate_lectures(self.client, self.db, Mock()), [])

    def test_no_audio_stays_pending_but_stops_after_three_failed_runs(self):
        self.transcriber.transcribe_tail.side_effect = NoAudioStreamError("video only")
        for attempt in range(3):
            self.assertEqual(len(main._enumerate_lectures(self.client, self.db, Mock())), 1)
            self.assertIsNone(self.runner.run("course", "Course", self.lecture))
            row = self.db.get_lecture("1")
            self.assertIsNone(row["processed_at"])
            self.assertEqual(row["error_count"], attempt + 1)
        self.assertEqual(main._enumerate_lectures(self.client, self.db, Mock()), [])
        self.assertEqual(self.scheduler.audio_downloader.release.call_count, 3)

    def test_whitespace_asr_is_not_cached_for_the_next_run(self):
        self.transcriber.transcribe_tail.return_value = (" \n ", [])
        self.runner.run("course", "Course", self.lecture)
        self.assertFalse(self.db.get_lecture("1")["transcript"])
        self.transcriber.transcribe_tail.return_value = ("useful transcript", [])
        self.summarizer.summarize.return_value = ("summary", "model")
        self.assertEqual(self.runner.run("course", "Course", self.lecture), "summary")
        self.assertEqual(self.transcriber.transcribe_tail.call_count, 2)

    def test_fresh_and_db_only_lectures_are_still_enumerated(self):
        self.db.insert_lecture("retry", "course", "Older lecture", "")
        self.db.update_error("retry", "transcribe", "interrupted")
        self.client.get_course_detail.return_value["lectures"].append(
            {"sub_id": "new", "sub_title": "New lecture", "has_playback": True}
        )
        result = main._enumerate_lectures(self.client, self.db, Mock())
        self.assertEqual({row[2]["sub_id"] for row in result}, {"1", "retry", "new"})

    def test_no_video_error_count_is_not_reset(self):
        for stage in ("transcribe", "no_video", "transcribe"):
            self.db.update_error("1", stage, stage)
        self.assertEqual(self.db.get_lecture("1")["error_count"], 3)
        self.assertEqual(main._enumerate_lectures(self.client, self.db, Mock()), [])


if __name__ == "__main__":
    unittest.main()
