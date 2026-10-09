"""SMTP rate limits must defer delivery without losing unsent lectures."""

import contextlib
import io
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import main
from src.api.emailer import Emailer
from src.data.database import Database
from src.runtime.reporter import Reporter


RATE_LIMIT = b"Too many attempts. Unable to send. Try again later"
CONTENT_REJECTION = b"The mail may contain inappropriate words or content."


class EmailThrottlingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / "lectures.db"))
        self.addCleanup(self.db.conn.close)
        self.db.upsert_course("course", "Course", "Teacher")
        self.items = []
        for sub_id in ("1", "2", "3"):
            self.db.insert_lecture(sub_id, "course", "Lecture " + sub_id, "2026-10-09")
            self.db.update_summary(sub_id, "Summary " + sub_id, "model")
            self.db.mark_processed(sub_id)
            self.items.append({"sub_id": sub_id, "course_title": "Course",
                               "sub_title": "Lecture " + sub_id,
                               "date": "2026-10-09", "summary": "Summary " + sub_id})
        self.reporter = Reporter()
        self.reporter.email_failed = Mock(wraps=self.reporter.email_failed)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.sleep = self.stack.enter_context(patch("main.time.sleep"))

    def smtp(self):
        return self.stack.enter_context(patch("src.api.emailer.smtplib.SMTP_SSL"))

    def assert_unsent(self, expected):
        self.assertEqual({row["sub_id"] for row in self.db.get_unsent_lectures()}, set(expected))

    def test_explicit_550_rate_limit_is_identified_without_retry(self):
        smtp = self.smtp()
        smtp.return_value.__enter__.return_value.sendmail.side_effect = smtplib.SMTPDataError(550, RATE_LIMIT)
        emailer = Emailer()
        self.assertFalse(emailer.send(self.items))
        self.assertIs(getattr(emailer, "last_rate_limited", False), True)
        self.assertEqual(emailer.last_error_code, 550)
        self.assertEqual(smtp.call_count, 1)
        self.sleep.assert_not_called()

    def test_explicit_temporary_rate_limit_is_not_retried_immediately(self):
        smtp = self.smtp()
        smtp.return_value.__enter__.return_value.sendmail.side_effect = smtplib.SMTPDataError(451, RATE_LIMIT)
        emailer = Emailer()
        self.assertFalse(emailer.send(self.items))
        self.assertIs(getattr(emailer, "last_rate_limited", False), True)
        self.assertEqual(smtp.call_count, 1)
        self.sleep.assert_not_called()

    def test_content_550_is_distinct_from_rate_limit(self):
        smtp = self.smtp()
        smtp.return_value.__enter__.return_value.sendmail.side_effect = smtplib.SMTPDataError(550, CONTENT_REJECTION)
        emailer = Emailer()
        self.assertFalse(emailer.send(self.items))
        self.assertIs(getattr(emailer, "last_rate_limited", None), False)
        self.assertEqual(smtp.call_count, 1)

    def test_rate_limit_marker_is_reset_on_later_send(self):
        smtp = self.smtp()
        smtp.return_value.__enter__.return_value.sendmail.side_effect = [
            smtplib.SMTPDataError(550, RATE_LIMIT), {},
        ]
        emailer = Emailer()
        self.assertFalse(emailer.send(self.items))
        self.assertIs(getattr(emailer, "last_rate_limited", False), True)
        self.assertTrue(emailer.send(self.items))
        self.assertIs(emailer.last_rate_limited, False)
        self.assertIsNone(emailer.last_error_code)

    def test_rate_limited_digest_does_not_fan_out(self):
        emailer = Mock(last_error_code=550, last_rate_limited=True)
        emailer.send.return_value = False
        main._send_email(emailer, self.db, self.reporter, [])
        emailer.send.assert_called_once()
        self.sleep.assert_not_called()
        self.assert_unsent(("1", "2", "3"))
        self.assertEqual(self.reporter.email_failed.call_args_list,
                         [call("1"), call("2"), call("3")])
        with self.assertRaises(RuntimeError):
            self.reporter.raise_if_failed()

    def test_rate_limit_stops_split_batch_and_preserves_remaining_items(self):
        emailer = Mock(last_error_code=550, last_rate_limited=False)
        outcomes = iter(((False, False), (True, False), (False, True), (True, False)))

        def send(items):
            delivered, emailer.last_rate_limited = next(outcomes)
            return delivered

        emailer.send.side_effect = send
        main._send_email(emailer, self.db, self.reporter, [])
        self.assertEqual([len(entry.args[0]) for entry in emailer.send.call_args_list], [3, 1, 1])
        self.assertIsNotNone(self.db.get_lecture("1")["emailed_at"])
        self.assert_unsent(("2", "3"))
        self.assertEqual(self.reporter.email_failed.call_args_list, [call("2"), call("3")])
        self.sleep.assert_called_once_with(30)

    def test_split_content_rejection_keeps_other_deliveries_and_paces_requests(self):
        emailer = Mock(last_error_code=550, last_rate_limited=False)
        events = []
        outcomes = iter((False, False, True, True))

        def send(items):
            events.append("digest" if len(items) > 1 else items[0]["sub_id"])
            return next(outcomes)

        emailer.send.side_effect = send
        self.sleep.side_effect = lambda seconds: events.append(("wait", seconds))
        main._send_email(emailer, self.db, self.reporter, [])
        self.assertEqual(events, ["digest", "1", ("wait", 30), "2", ("wait", 30), "3"])
        self.assert_unsent(("1",))
        self.reporter.email_failed.assert_called_once_with("1")

    def test_successful_digest_has_no_split_delay(self):
        emailer = Mock()
        emailer.send.return_value = True
        main._send_email(emailer, self.db, self.reporter, [])
        emailer.send.assert_called_once()
        self.sleep.assert_not_called()
        self.reporter.email_failed.assert_not_called()
        self.assert_unsent(())

    def test_legacy_emailer_mock_does_not_look_rate_limited(self):
        emailer = Mock(last_error_code=550)
        emailer.send.side_effect = [False, True, True, True]
        main._send_email(emailer, self.db, self.reporter, [])
        self.assertEqual(emailer.send.call_count, 4)
        self.assert_unsent(())


if __name__ == "__main__":
    unittest.main()
