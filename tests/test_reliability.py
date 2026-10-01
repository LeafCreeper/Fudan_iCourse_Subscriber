import contextlib
import io
import os
from pathlib import Path
import smtplib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from src.ai.transcriber import IncompleteAudioError, NoAudioStreamError, Transcriber
from src.api.emailer import Emailer
from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime.reporter import Reporter


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / 'test.db'))
        self.addCleanup(self.db.conn.close)
        self.db.upsert_course('course', 'Test course', 'Teacher')
        self.db.insert_lecture('1', 'course', 'Lecture 1', '2026-09-30')
        self.reporter = Reporter()
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)


class AudioRetryTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.downloader = Mock()
        self.downloader.get.return_value = SimpleNamespace(path='audio.raw', process=Mock(), stderr_chunks=[])
        self.transcriber = Mock()
        self.runner = LectureRunner(self.client, self.db,
                                    SimpleNamespace(audio_downloader=self.downloader),
                                    self.transcriber, Mock(), self.reporter)
        self.addCleanup(patch.stopall)
        patch('src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT', False).start()
        patch('src.pipeline.lecture_runner.time.sleep').start()

    def incomplete(self):
        return IncompleteAudioError('Only received 4068s of 10031s', 4068, 10031, 'partial')

    def test_truncated_audio_restarts_download_and_saves_only_complete_transcript(self):
        self.transcriber.transcribe_tail.side_effect = [self.incomplete(), ('complete', [])]
        result = self.runner._get_transcript(None, 'course', '1')
        self.assertEqual(result, ('complete', []))
        self.assertEqual(self.db.get_lecture('1')['transcript'], 'complete')
        self.assertEqual(self.downloader.schedule.call_count, 2)
        self.downloader.release.assert_called_once_with('1')
        self.assertFalse(self.db.get_lecture('1')['error_stage'])

    def test_exhaustion_is_bounded_and_partial_transcript_is_never_saved(self):
        self.transcriber.transcribe_tail.side_effect = self.incomplete()
        self.assertEqual(self.runner._get_transcript(None, 'course', '1'), (None, None))
        self.assertEqual(self.transcriber.transcribe_tail.call_count, 3)
        self.assertEqual(self.downloader.release.call_count, 3)
        row = self.db.get_lecture('1')
        self.assertFalse(row['transcript'])
        self.assertEqual(row['error_stage'], 'transcribe')
        self.assertEqual(row['error_count'], 1)

    def test_no_audio_stream_is_not_retried(self):
        self.transcriber.transcribe_tail.side_effect = NoAudioStreamError('video only')
        self.assertEqual(self.runner._get_transcript(None, 'course', '1'), (None, None))
        self.assertEqual(self.transcriber.transcribe_tail.call_count, 1)

    def test_spawn_timeout_releases_pending_download(self):
        self.downloader.get.side_effect = TimeoutError('spawn timed out')
        self.runner._get_transcript(None, 'course', '1')
        self.assertEqual(self.downloader.release.call_count, 3)

    def test_cached_transcript_never_downloads(self):
        self.assertEqual(self.runner._get_transcript({'transcript': 'cached'}, 'course', '1'), ('cached', None))
        self.downloader.schedule.assert_not_called()


class AudioDiagnosticsTests(unittest.TestCase):
    def test_truncation_reports_exit_code_and_redacted_ffmpeg_error(self):
        transcriber = Transcriber()
        transcriber._last_duration = 4068
        transcriber._media_duration = 10031
        transcriber._consume_pcm_stream = Mock(return_value=('partial', []))
        process = Mock(returncode=0)
        stderr = [b'HTTP error 403 Forbidden\nhttps://example.test/video?t=secret-token\nCookie: session=secret-cookie\n']
        with tempfile.NamedTemporaryFile() as pcm:
            with self.assertRaises(IncompleteAudioError) as caught:
                transcriber.transcribe_tail(pcm.name, process, stderr)
        message = str(caught.exception)
        self.assertIn('403 Forbidden', message)
        self.assertIn('rc=0', message)
        self.assertNotIn('secret-token', message)
        self.assertNotIn('secret-cookie', message)


class EmailTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.items = []
        for sub_id in ['1', '2', '3']:
            self.db.insert_lecture(sub_id, 'course', 'Lecture ' + sub_id, '2026-09-30')
            self.db.update_summary(sub_id, 'summary ' + sub_id, 'model')
            self.db.mark_processed(sub_id)
            self.items.append(dict(sub_id=sub_id, course_title='Test course',
                                   sub_title='Lecture ' + sub_id, date='2026-09-30', summary='summary ' + sub_id))

    def test_rejected_digest_isolated_and_only_delivered_lectures_marked(self):
        emailer = Mock(last_error_code=550)
        emailer.send.side_effect = [False, True, False, True]
        main._send_email(emailer, self.db, self.reporter, [])
        self.assertIsNotNone(self.db.get_lecture('1')['emailed_at'])
        self.assertIsNone(self.db.get_lecture('2')['emailed_at'])
        self.assertIsNotNone(self.db.get_lecture('3')['emailed_at'])
        with self.assertRaises(RuntimeError):
            self.reporter.raise_if_failed()
        self.assertEqual([len(call.args[0]) for call in emailer.send.call_args_list], [3, 1, 1, 1])

    def test_successful_digest_is_sent_once(self):
        emailer = Mock()
        emailer.send.return_value = True
        main._send_email(emailer, self.db, self.reporter, self.items)
        self.assertEqual(emailer.send.call_count, 1)
        self.assertEqual(self.db.get_unsent_lectures(), [])

    def test_authentication_failure_does_not_fan_out(self):
        emailer = Mock(last_error_code=535)
        emailer.send.return_value = False
        main._send_email(emailer, self.db, self.reporter, [])
        self.assertEqual(emailer.send.call_count, 1)
        self.assertEqual(len(self.db.get_unsent_lectures()), 3)

    def test_permanent_smtp_rejection_is_not_sent_three_times(self):
        with patch('src.api.emailer.smtplib.SMTP_SSL') as smtp, patch('src.api.emailer.time.sleep'):
            smtp.return_value.__enter__.return_value.sendmail.side_effect = smtplib.SMTPDataError(550, b'content rejected')
            emailer = Emailer()
            self.assertFalse(emailer.send(self.items))
            self.assertEqual(smtp.call_count, 1)
            self.assertEqual(emailer.last_error_code, 550)

    def test_temporary_smtp_rejection_still_retries(self):
        with patch('src.api.emailer.smtplib.SMTP_SSL') as smtp, patch('src.api.emailer.time.sleep'):
            smtp.return_value.__enter__.return_value.sendmail.side_effect = [smtplib.SMTPDataError(451, b'try later'), {}]
            self.assertTrue(Emailer().send(self.items))
            self.assertEqual(smtp.call_count, 2)


class RunStatusTests(DatabaseCase):
    def test_recorded_transcription_failure_affects_run_status(self):
        self.db.update_error('1', 'transcribe', 'incomplete audio')
        with patch('main.LectureRunner') as factory, patch('main._check_session'):
            factory.return_value.run.return_value = None
            main._drive_lectures(Mock(), self.db, Mock(), Mock(), Mock(), self.reporter,
                                 [('course', 'Test course', {'sub_id': '1'})], [])
        with self.assertRaises(RuntimeError):
            self.reporter.raise_if_failed()

    def test_failure_summary_is_written_before_nonzero_exit(self):
        path = Path(self.temp.name) / 'summary.md'
        self.reporter.course_enumeration_error('course')
        with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(path)}):
            self.reporter.run_footer()
        self.assertTrue(path.exists())
        self.assertIn('course', path.read_text())
        with self.assertRaises(RuntimeError):
            self.reporter.raise_if_failed()

class RecoveryIntegrationTests(DatabaseCase):
    def test_real_downloader_requests_new_signed_url_after_truncation(self):
        from src.runtime.scheduler import AudioDownloader
        downloader = AudioDownloader(self.temp.name, max_concurrent=2, reporter=self.reporter)
        self.addCleanup(downloader.shutdown)
        client = Mock()
        client.get_video_url.side_effect = ['https://test.invalid/first', 'https://test.invalid/second']
        client.get_stream_params.side_effect = lambda url: (url, 'Cookie: fake')
        transcriber = Mock()
        transcriber.transcribe_tail.side_effect = [IncompleteAudioError('truncated', 4, 10), ('complete', [])]
        refresh = Mock()
        runner = LectureRunner(client, self.db, SimpleNamespace(audio_downloader=downloader), transcriber, Mock(), self.reporter, ensure_session=refresh)
        processes = []
        def spawn(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b'pcm')
            process = Mock(returncode=0, stderr=io.BytesIO(b'ffmpeg diagnostic'))
            process.poll.return_value = 0
            process.wait.return_value = 0
            processes.append((cmd, process))
            return process
        with patch('src.runtime.scheduler.subprocess.Popen', side_effect=spawn), patch('src.pipeline.lecture_runner.time.sleep'), patch('src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT', False):
            self.assertEqual(runner._get_transcript(None, 'course', '1'), ('complete', []))
        self.assertEqual(len(processes), 2)
        self.assertIn('https://test.invalid/first', processes[0][0])
        self.assertIn('https://test.invalid/second', processes[1][0])
        refresh.assert_called_once()
        self.assertEqual(self.db.get_lecture('1')['transcript'], 'complete')

    def test_run_saves_successful_email_then_exits_failed_for_another_lecture(self):
        self.db.insert_lecture('2', 'course', 'Lecture 2', '2026-09-30')
        def lecture_result(*args, **kwargs):
            sub_id = args[2]['sub_id']
            if sub_id == '1':
                self.db.update_error('1', 'transcribe', 'incomplete')
                return None
            self.db.update_summary('2', 'summary', 'model')
            self.db.mark_processed('2')
            return 'summary'
        summary_path = Path(self.temp.name) / 'actions.md'
        with contextlib.ExitStack() as stack:
            for name in ['Transcriber', 'Summarizer', 'Scheduler', 'ICourseClient', 'login_with_retry', '_crawl_semester_catalog', '_check_session']:
                stack.enter_context(patch('main.' + name))
            stack.enter_context(patch('main.Database', return_value=self.db))
            stack.enter_context(patch('main.config.COURSE_IDS', ['course']))
            stack.enter_context(patch('main.config.SMTP_EMAIL', 'test@example.invalid'))
            stack.enter_context(patch('main.config.SMTP_PASSWORD', 'test'))
            stack.enter_context(patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary_path)}))
            stack.enter_context(patch('main._enumerate_lectures', return_value=[('course', 'Test course', {'sub_id': sub_id}) for sub_id in ['1', '2']]))
            factory = stack.enter_context(patch('main.LectureRunner'))
            factory.return_value.run.side_effect = lecture_result
            emailer = stack.enter_context(patch('main.Emailer'))
            emailer.return_value.send.return_value = True
            with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                main.run()
        self.assertIsNotNone(self.db.get_lecture('2')['emailed_at'])
        self.assertEqual(self.db.get_lecture('1')['error_stage'], 'transcribe')
        self.assertIn('lecture', summary_path.read_text())

    def test_all_split_messages_delivered_leaves_run_successful(self):
        for sub_id in ['1', '2']:
            self.db.insert_lecture(sub_id, 'course', 'Lecture', '2026-09-30')
            self.db.update_summary(sub_id, 'summary', 'model')
            self.db.mark_processed(sub_id)
        emailer = Mock(last_error_code=550)
        emailer.send.side_effect = [False, True, True]
        main._send_email(emailer, self.db, self.reporter, [])
        self.reporter.raise_if_failed()
        self.assertEqual(self.db.get_unsent_lectures(), [])

    def test_successful_split_messages_are_not_retried_next_run(self):
        for sub_id in ['1', '2']:
            self.db.insert_lecture(sub_id, 'course', 'Lecture', '2026-09-30')
            self.db.update_summary(sub_id, 'summary', 'model')
            self.db.mark_processed(sub_id)
        emailer = Mock(last_error_code=550)
        emailer.send.side_effect = [False, True, False]
        main._send_email(emailer, self.db, self.reporter, [])
        next_emailer = Mock(last_error_code=None)
        next_emailer.send.return_value = True
        main._send_email(next_emailer, self.db, Reporter(), [])
        self.assertEqual([item['sub_id'] for item in next_emailer.send.call_args.args[0]], ['2'])

    def test_session_check_failure_is_reported_without_skipping_other_lectures(self):
        self.db.insert_lecture('2', 'course', 'Lecture 2', '2026-09-30')
        with patch('main.LectureRunner') as runner, patch('main._check_session', side_effect=[RuntimeError('session expired'), None]), contextlib.redirect_stderr(io.StringIO()):
            runner.return_value.run.return_value = None
            main._drive_lectures(Mock(), self.db, Mock(), Mock(), Mock(), self.reporter,
                                 [('course', 'Test course', {'sub_id': sub_id}) for sub_id in ['1', '2']], [])
        self.assertEqual(runner.return_value.run.call_count, 1)
        with self.assertRaises(RuntimeError):
            self.reporter.raise_if_failed()

class DownloaderFailureTests(DatabaseCase):
    def test_network_failure_during_url_fetch_is_not_classified_as_missing_video(self):
        from src.runtime.scheduler import AudioDownloader
        downloader = AudioDownloader(self.temp.name, max_concurrent=1, reporter=self.reporter)
        self.addCleanup(downloader.shutdown)
        client = Mock()
        client.get_video_url.side_effect = [TimeoutError('server timed out'), 'https://test.invalid/new']
        client.get_stream_params.return_value = ('https://test.invalid/new', '')
        transcriber = Mock()
        transcriber.transcribe_tail.return_value = ('complete', [])
        runner = LectureRunner(client, self.db, SimpleNamespace(audio_downloader=downloader), transcriber, Mock(), self.reporter)
        def spawn(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b'pcm')
            process = Mock(returncode=0, stderr=io.BytesIO(b''))
            process.poll.return_value = 0
            process.wait.return_value = 0
            return process
        with patch('src.runtime.scheduler.subprocess.Popen', side_effect=spawn), patch('src.pipeline.lecture_runner.time.sleep'), patch('src.pipeline.lecture_runner.config.USE_OFFICIAL_TRANSCRIPT', False):
            result = runner._get_transcript(None, 'course', '1')
        self.assertEqual(result, ('complete', []))
        self.assertEqual(client.get_video_url.call_count, 2)

    def test_cancelled_spawn_cannot_delete_replacement_audio(self):
        import threading
        from src.runtime.scheduler import AudioDownloader
        downloader = AudioDownloader(self.temp.name, max_concurrent=2)
        self.addCleanup(downloader.shutdown)
        entered, resume, old_done = threading.Event(), threading.Event(), threading.Event()
        old_client = Mock()
        def old_params(url):
            entered.set()
            resume.wait(2)
            return url, ''
        old_client.get_video_url.return_value = 'https://test.invalid/old'
        old_client.get_stream_params.side_effect = old_params
        new_client = Mock()
        new_client.get_video_url.return_value = 'https://test.invalid/new'
        new_client.get_stream_params.side_effect = lambda url: (url, '')
        def spawn(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b'new pcm')
            process = Mock(returncode=0, stderr=io.BytesIO(b''))
            process.poll.return_value = 0
            process.wait.return_value = 0
            if 'https://test.invalid/old' in cmd:
                old_done.set()
            return process
        original_spawn = downloader._spawn_when_ready
        def wrapped(*args):
            try:
                return original_spawn(*args)
            finally:
                if args[0] is old_client:
                    old_done.set()
        with patch.object(downloader, '_spawn_when_ready', side_effect=wrapped), patch('src.runtime.scheduler.subprocess.Popen', side_effect=spawn):
            downloader.schedule(old_client, 'course', '1')
            self.assertTrue(entered.wait(2))
            downloader.release('1')
            downloader.schedule(new_client, 'course', '1')
            handle = downloader.get('1', timeout=2)
            resume.set()
            self.assertTrue(old_done.wait(2))
            # Wait until the old worker has completed its cancellation cleanup.
            for thread in threading.enumerate():
                if thread.name == 'audio-spawn-1':
                    thread.join(timeout=2)
            self.assertTrue(Path(handle.path).exists(), 'Cancelled request deleted current audio')
            self.assertEqual(Path(handle.path).read_bytes(), b'new pcm')


if __name__ == '__main__':
    unittest.main()
