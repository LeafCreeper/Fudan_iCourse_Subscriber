"""Regression coverage for missing summaries and persisted retry state."""

import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
from scripts.merge_db import merge
from src.ai.transcriber import NoAudioStreamError
from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime.reporter import Reporter


class MissingSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'local.db')
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.conn.close())
        self.db.upsert_course('course', 'Course', 'Teacher')
        self.lecture = {'sub_id': '1', 'sub_title': 'Lecture',
                        'date': '2026-10-08', 'has_playback': True}
        self.db.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def runner(self):
        runner = LectureRunner(Mock(), self.db, Mock(), Mock(), Mock(), Reporter())
        runner._ppt = Mock()
        runner._schedule_next = Mock()
        runner._release_audio = Mock()
        return runner

    def test_empty_asr_result_is_not_completed(self):
        runner = self.runner()
        runner._get_transcript = Mock(return_value=('', []))
        self.assertIsNone(runner.run('course', 'Course', self.lecture))
        row = self.db.get_lecture('1')
        self.assertIsNone(row['processed_at'])
        self.assertEqual(row['error_stage'], 'empty_transcript')
        self.assertEqual(row['error_count'], 1)

    def test_video_without_audio_remains_retryable(self):
        runner = self.runner()
        runner._transcriber.transcribe_tail.side_effect = NoAudioStreamError('video only')
        with patch('src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT', False):
            runner._get_transcript(None, 'course', '1')
        self.assertIsNone(self.db.get_lecture('1')['processed_at'])
        self.assertEqual(self.db.get_lecture('1')['error_stage'], 'transcribe')

    def test_old_false_completion_is_repaired_without_resetting_retry_count(self):
        self.db.mark_processed('1')
        self.db.update_error('1', 'transcribe', 'no audio')
        self.db.conn.close()
        self.db = Database(self.path)
        self.assertIsNone(self.db.get_lecture('1')['processed_at'])
        self.assertEqual(self.db.get_lecture('1')['error_count'], 1)

    def test_valid_completed_summary_is_untouched(self):
        self.db.update_summary('1', 'Useful summary', 'test-model')
        self.db.mark_processed('1')
        self.db.mark_emailed_batch(['1'])
        original = self.db.get_lecture('1')
        self.db.conn.close()
        self.db = Database(self.path)
        self.assertEqual(self.db.get_lecture('1'), original)

    def test_retry_budget_cannot_be_bypassed_by_live_course_list(self):
        for _ in range(3):
            self.db.update_error('1', 'empty_transcript', 'no speech')
        client = Mock()
        client.get_course_detail.return_value = {
            'title': 'Course', 'teacher': 'Teacher', 'lectures': [self.lecture]}
        reporter = Reporter()
        with patch('main.config.COURSE_IDS', ['course']):
            self.assertEqual(main._enumerate_lectures(client, self.db, reporter), [])
        with self.assertRaises(RuntimeError):
            reporter.raise_if_failed()

    def test_waiting_for_playback_does_not_exhaust_retry_budget(self):
        for _ in range(4):
            self.db.update_error('1', 'no_video', 'not yet available')
        self.assertEqual([r['sub_id'] for r in self.db.get_unprocessed_lectures('course')], ['1'])

    def test_waiting_does_not_consume_later_transcription_retry_budget(self):
        for _ in range(4):
            self.db.update_error('1', 'no_video', 'not yet available')
        self.db.update_error('1', 'transcribe', 'incomplete audio')
        self.assertEqual(self.db.get_lecture('1')['error_count'], 1)
        self.assertNotIn('1', self.db.get_ineligible_sub_ids('course'))

    def test_blank_summary_is_not_completed(self):
        self.assertFalse(LectureRunner._has_summary({'summary': '   '}))

    def test_blank_local_summary_cannot_overwrite_remote_success(self):
        self.db.update_summary('1', '', 'old-model')
        remote_path = str(Path(self.temp.name) / 'remote.db')
        remote = Database(remote_path)
        remote.upsert_course('course', 'Course', 'Teacher')
        remote.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
        remote.update_summary('1', 'Concurrent success', 'model')
        remote.mark_processed('1')
        remote.mark_emailed_batch(['1'])
        original = remote.get_lecture('1')
        remote.conn.close()
        merge(self.path, remote_path)
        with sqlite3.connect(remote_path) as conn:
            summary, processed, emailed = conn.execute('SELECT summary, processed_at, emailed_at FROM lectures').fetchone()
        self.assertEqual((summary, processed, emailed),
                         (original['summary'], original['processed_at'], original['emailed_at']))

    def test_merge_does_not_restore_old_waiting_count_to_real_failure(self):
        remote_path = str(Path(self.temp.name) / 'remote.db')
        remote = Database(remote_path)
        remote.upsert_course('course', 'Course', 'Teacher')
        remote.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
        with remote.conn:
            remote.conn.execute("UPDATE lectures SET error_stage='no_video', error_count=8")
        remote.conn.close()
        self.db.update_error('1', 'transcribe', 'incomplete audio')
        merge(self.path, remote_path)
        with sqlite3.connect(remote_path) as conn:
            stage, count = conn.execute('SELECT error_stage, error_count FROM lectures').fetchone()
        self.assertEqual((stage, count), ('transcribe', 1))

    def test_merge_does_not_resurrect_false_completion_or_erase_retry_error(self):
        remote_path = str(Path(self.temp.name) / 'remote.db')
        remote = Database(remote_path)
        remote.upsert_course('course', 'Course', 'Teacher')
        remote.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
        remote.mark_processed('1')
        remote.conn.close()
        self.db.update_error('1', 'empty_transcript', 'no speech')
        merge(self.path, remote_path)
        with sqlite3.connect(remote_path) as conn:
            row = conn.execute('SELECT processed_at, error_stage, error_count FROM lectures').fetchone()
        self.assertEqual(row, (None, 'empty_transcript', 1))

    def test_merge_preserves_concurrent_success(self):
        remote_path = str(Path(self.temp.name) / 'remote.db')
        remote = Database(remote_path)
        remote.upsert_course('course', 'Course', 'Teacher')
        remote.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
        remote.update_summary('1', 'Concurrent success', 'model')
        remote.mark_processed('1')
        remote.conn.close()
        self.db.update_error('1', 'empty_transcript', 'no speech')
        merge(self.path, remote_path)
        with sqlite3.connect(remote_path) as conn:
            summary, processed, error = conn.execute('SELECT summary, processed_at, error_stage FROM lectures').fetchone()
        self.assertEqual(summary, 'Concurrent success')
        self.assertIsNotNone(processed)
        self.assertIsNone(error)


if __name__ == '__main__':
    unittest.main()
