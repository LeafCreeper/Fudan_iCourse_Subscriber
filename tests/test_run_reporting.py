import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.runtime.reporter import Reporter


class ReportingTests(unittest.TestCase):
    def test_failed_course_is_in_actions_summary(self):
        reporter = Reporter()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.md'
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(path)}), contextlib.redirect_stdout(io.StringIO()):
                reporter.course_enumeration_error('course-123')
                reporter.run_footer()
            self.assertTrue(path.exists(), 'Run must write a GitHub Actions summary')
            self.assertIn('course-123', path.read_text())
            self.assertIn('failed', path.read_text().lower())

    def test_failed_lecture_prevents_success_exit(self):
        reporter = Reporter()
        with contextlib.redirect_stdout(io.StringIO()):
            reporter.lecture_error('123')
        self.assertTrue(callable(getattr(reporter, 'raise_if_failed', None)), 'Run must reject success after lecture failure')
        with self.assertRaises(RuntimeError):
            reporter.raise_if_failed()

    def test_clean_run_is_successful(self):
        reporter = Reporter()
        self.assertTrue(callable(getattr(reporter, 'raise_if_failed', None)))
        reporter.raise_if_failed()

class EmailReportingTests(unittest.TestCase):
    def test_rejected_lecture_is_identified_in_summary(self):
        reporter = Reporter()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.md'
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(path)}), contextlib.redirect_stdout(io.StringIO()):
                reporter.email_failed('123')
                reporter.run_footer()
            self.assertIn('email', path.read_text())
            self.assertIn('123', path.read_text())
            with self.assertRaises(RuntimeError):
                reporter.raise_if_failed()


if __name__ == '__main__':
    unittest.main()
