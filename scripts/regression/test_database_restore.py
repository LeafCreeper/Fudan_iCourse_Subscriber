from contextlib import closing
import gzip
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import restore_database
from src.data import crypto_box
from src.data.schema import SCHEMA_SQL
from src.data.sharder import shard_database

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


    def restore(self, files=None, error=None, **options):
        files = files or {}
        def fake_git(*args):
            if args[0] == "ls-remote":
                if error:
                    raise subprocess.CalledProcessError(error, ["git", *args])
                return b"branch exists"
            if args[0] == "fetch":
                return b""
            if args[0] == "ls-tree":
                return "\n".join(files).encode()
            if args[0] == "show":
                return files[args[1].removeprefix("origin/data:")]
            raise AssertionError(args)
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {"STUID": "test-student", "UISPSW": "test-password", "DASHSCOPE_API_KEY": "test-api", "SMTP_PASSWORD": "test-smtp"}), patch.object(restore_database, "git", side_effect=fake_git):
                return restore_database.restore_database(**options)
        finally:
            os.chdir(old_cwd)


    def test_only_absent_branch_can_start_empty(self):
        self.assertFalse(self.restore(error=2, allow_empty=True))
        with self.assertRaises(subprocess.CalledProcessError):
            self.restore(error=128, allow_empty=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.restore(error=2)
        with self.assertRaises(ValueError):
            self.restore({"data/unrelated": b"x"}, allow_empty=True)


    def test_legacy_database_is_restored_with_old_key(self):
        key = crypto_box.derive_legacy_password("test-student", "test-password", "test-api", "test-smtp")
        encrypted = crypto_box.encrypt(self.db.read_bytes(), key, crypto_box.LEGACY_ITERATIONS)
        self.assertTrue(self.restore({"data/icourse.db.enc": encrypted}))
        self.assertEqual((self.root / "data/icourse.db").read_bytes(), self.db.read_bytes())


    def test_wrong_password_cannot_replace_existing_database(self):
        existing = self.root / "data/icourse.db"
        existing.parent.mkdir()
        existing.write_bytes(b"preserve existing database")
        encrypted = crypto_box.encrypt(self.db.read_bytes(), "wrong-password")
        with self.assertRaises(ValueError):
            self.restore({"data/icourse.db.enc": encrypted}, allow_empty=True)
        self.assertEqual(existing.read_bytes(), b"preserve existing database")


    def test_compressed_database_and_gzip_corruption(self):
        password = crypto_box.derive_new_password("test-student", "test-password")
        encrypted = crypto_box.encrypt(gzip.compress(self.db.read_bytes()), password)
        self.assertTrue(self.restore({"data/icourse.db.gz.enc": encrypted}))
        restored = self.root / "data/icourse.db"
        before = restored.read_bytes()
        corrupt = crypto_box.encrypt(b"\x1f\x8bbroken gzip", password)
        with self.assertRaises((EOFError, gzip.BadGzipFile)):
            self.restore({"data/icourse.db.gz.enc": corrupt})
        self.assertEqual(restored.read_bytes(), before)


    def test_index_paths_cannot_escape_shard_directory(self):
        password = crypto_box.derive_new_password("test-student", "test-password")
        index = json.dumps({"shards": [{"name": "../../outside.db.gz.enc"}]}).encode()
        encrypted = crypto_box.encrypt(index, password)
        with self.assertRaises(ValueError):
            self.restore({"data/icourse-index.enc": encrypted})
        self.assertFalse((self.root / "outside.db.gz.enc").exists())


    def test_shards_restore_and_missing_or_corrupt_shard_fail_closed(self):
        output = self.root / "encrypted"
        password = crypto_box.derive_new_password("test-student", "test-password")
        shard_database(str(self.db), str(output), password)
        files = {"data/" + str(path.relative_to(output)).replace("\\", "/"): path.read_bytes() for path in output.rglob("*") if path.is_file()}
        self.assertTrue(self.restore(files))
        restored = self.root / "data/icourse.db"
        before = restored.read_bytes()
        shard_path = next(name for name in files if name.startswith("data/shards/"))
        missing = {name: value for name, value in files.items() if name != shard_path}
        with self.assertRaises(ValueError):
            self.restore(missing)
        self.assertEqual(restored.read_bytes(), before)
        files[shard_path] = b"corrupt shard"
        with self.assertRaises(ValueError):
            self.restore(files)
        self.assertEqual(restored.read_bytes(), before)


