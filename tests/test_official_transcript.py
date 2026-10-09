"""Official captions should recover lectures without usable audio."""

import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime import config


class OfficialTranscriptTests(unittest.TestCase):
    def test_enabled_by_default_and_can_be_explicitly_disabled(self):
        try:
            with patch.dict(os.environ):
                os.environ.pop('USE_OFFICIAL_TRANSCRIPT', None)
                importlib.reload(config)
                self.assertTrue(config.USE_OFFICIAL_TRANSCRIPT)
                os.environ['USE_OFFICIAL_TRANSCRIPT'] = '0'
                importlib.reload(config)
                self.assertFalse(config.USE_OFFICIAL_TRANSCRIPT)
        finally:
            importlib.reload(config)

    def test_complete_captions_skip_audio_and_persist_text(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / 'test.db'))
            try:
                db.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
                client, scheduler, transcriber = Mock(), Mock(), Mock()
                segments = [{'start_ms': 0, 'end_ms': 60000, 'text': 'Official lecture text'}]
                client.get_transcript_segments.return_value = segments
                runner = LectureRunner(client, db, scheduler, transcriber, Mock(), Mock())
                with patch.object(config, 'USE_OFFICIAL_TRANSCRIPT', True):
                    self.assertEqual(runner._get_transcript(None, 'course', '1'),
                                     ('Official lecture text', segments))
                scheduler.audio_downloader.schedule.assert_not_called()
                transcriber.transcribe_tail.assert_not_called()
                self.assertEqual(db.get_lecture('1')['transcript'], 'Official lecture text')
            finally:
                db.conn.close()

    def test_incomplete_captions_fall_back_to_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / 'test.db'))
            try:
                db.insert_lecture('1', 'course', 'Lecture', '2026-10-08')
                client, scheduler, transcriber = Mock(), Mock(), Mock()
                client.get_transcript_segments.return_value = [
                    {'start_ms': 0, 'end_ms': 60000, 'text': 'First minute'},
                    {'start_ms': 1800000, 'end_ms': 1860000, 'text': 'Missing middle'},
                ]
                transcriber.transcribe_tail.return_value = ('Recovered audio text', [])
                runner = LectureRunner(client, db, scheduler, transcriber, Mock(), Mock())
                with patch.object(config, 'USE_OFFICIAL_TRANSCRIPT', True):
                    self.assertEqual(runner._get_transcript(None, 'course', '1'),
                                     ('Recovered audio text', []))
                transcriber.transcribe_tail.assert_called_once()
            finally:
                db.conn.close()


if __name__ == '__main__':
    unittest.main()
