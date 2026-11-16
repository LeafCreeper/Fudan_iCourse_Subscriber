from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.delete_course_records import delete_records
from src.data.schema import SCHEMA_SQL

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = self.root / "fixture.db"
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript(SCHEMA_SQL)
            connection.executemany("INSERT INTO courses(course_id) VALUES (?)", [("100",), ("200",)])
            connection.executemany("INSERT INTO lectures(sub_id,course_id,summary,processed_at) VALUES (?,?,?,?)", [
                ("one", "100", "First summary", "2026-01-01"),
                ("two", "100", "Second summary", "2026-01-01"),
                ("other", "200", "Other course", "2026-01-01"),
            ])
            connection.executemany("INSERT INTO ppt_pages(sub_id,page_num,created_sec) VALUES (?,1,0)", [("one",), ("two",), ("other",)])


    def tearDown(self):
        self.directory.cleanup()


    def rows(self, table):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(f"SELECT * FROM {table}").fetchall()


    def test_lecture_deletion_is_scoped_to_selected_course(self):
        before = self.db.read_bytes()
        self.assertEqual(delete_records(str(self.db), "100", "other"), 0)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(delete_records(str(self.db), "100", "one"), 1)
        self.assertEqual({row[0] for row in self.rows("lectures")}, {"two", "other"})
        self.assertEqual({row[0] for row in self.rows("ppt_pages")}, {"two", "other"})


    def test_course_deletion_preserves_other_course(self):
        self.assertEqual(delete_records(str(self.db), "100"), 2)
        self.assertEqual({row[0] for row in self.rows("courses")}, {"200"})
        self.assertEqual({row[0] for row in self.rows("ppt_pages")}, {"other"})


    def test_invalid_or_injected_ids_cannot_change_database(self):
        for course, lecture in [("$(touch marker)", ""), ("100' OR 1=1--", ""), ("100", "other'; DROP TABLE courses;--"), ("", ""), ("100", ", ,")]:
            with self.subTest(course=course, lecture=lecture):
                before = self.db.read_bytes()
                with self.assertRaises(ValueError):
                    delete_records(str(self.db), course, lecture)
                self.assertEqual(self.db.read_bytes(), before)


    def test_missing_database_is_not_created(self):
        missing = self.root / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            delete_records(str(missing), "100")
        self.assertFalse(missing.exists())


