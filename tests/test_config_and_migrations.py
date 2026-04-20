import sqlite3
import unittest

import openpotd
import shared
from cogs.interface import Interface


class ConfigAndMigrationTests(unittest.TestCase):
    def test_parse_allowed_guild_ids(self):
        parse = openpotd.OpenPOTD._parse_allowed_guild_ids

        self.assertIsNone(parse(None))
        self.assertIsNone(parse(""))
        self.assertEqual(parse(123), {123})
        self.assertEqual(parse(["123", 456, "123"]), {123, 456})
        self.assertEqual(parse(["bad-value", "789"]), {789})

    def test_otd_label_formatting(self):
        self.assertEqual(shared.format_otd_label("P"), "POTD")
        self.assertEqual(shared.format_otd_label("PoTW"), "PoTW")
        self.assertEqual(shared.format_otd_label("q", lowercase=True), "qotd")
        self.assertEqual(shared.config_otd_label({"otd_prefix": "PoTW"}), "PoTW")

    def test_parse_submission_int(self):
        self.assertEqual(Interface._try_parse_submission_int("42"), 42)
        self.assertEqual(Interface._try_parse_submission_int(" -12 "), -12)
        self.assertIsNone(Interface._try_parse_submission_int("hello"))
        self.assertIsNone(Interface._try_parse_submission_int(""))
        self.assertIsNone(Interface._try_parse_submission_int(str(2 ** 80)))

    def test_migrations_add_manual_submission_support(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE config (
                server_id INTEGER PRIMARY KEY,
                potd_channel INTEGER,
                ping_role_id INTEGER,
                solved_role_id INTEGER,
                otd_prefix TEXT,
                command_prefix TEXT
            );
            CREATE TABLE problems (
                id INTEGER PRIMARY KEY,
                date DATE NOT NULL,
                season INTEGER NOT NULL,
                statement TEXT NOT NULL,
                difficulty INTEGER,
                weighted_solves INTEGER NOT NULL DEFAULT 0,
                base_points INTEGER NOT NULL DEFAULT 0,
                answer INTEGER NOT NULL,
                public BOOLEAN,
                source TEXT,
                stats_message_id INTEGER,
                difficulty_rating REAL DEFAULT 1500,
                coolness_rating REAL DEFAULT 1500
            );
            CREATE TABLE users (discord_id INTEGER PRIMARY KEY);
            CREATE TABLE seasons (id INTEGER PRIMARY KEY);
            """
        )

        openpotd.ensure_database_migrations(conn)

        config_columns = {row[1] for row in conn.execute("PRAGMA table_info(config)").fetchall()}
        problem_columns = {row[1] for row in conn.execute("PRAGMA table_info(problems)").fetchall()}
        subproblem_columns = {row[1] for row in conn.execute("PRAGMA table_info(subproblems)").fetchall()}
        manual_submission_columns = {row[1] for row in conn.execute("PRAGMA table_info(manual_submissions)").fetchall()}
        manual_message_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(manual_submission_messages)").fetchall()
        }
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        self.assertIn("submission_channel_id", config_columns)
        self.assertIn("subproblem_thread_channel_id", config_columns)
        self.assertIn("submission_ping_role_id", config_columns)
        self.assertIn("auto_publish_news", config_columns)
        self.assertIn("manual_marking", problem_columns)
        self.assertIn("label", subproblem_columns)
        self.assertIn("marks", subproblem_columns)
        self.assertIn("answer", subproblem_columns)
        self.assertIn("manual_marking", subproblem_columns)
        self.assertIn("manual_submissions", tables)
        self.assertIn("manual_submission_messages", tables)
        self.assertIn("subproblems", tables)
        self.assertIn("subproblem_images", tables)
        self.assertIn("subproblem_threads", tables)
        self.assertIn("subproblem_attempts", tables)
        self.assertIn("subproblem_solves", tables)
        self.assertIn("subproblem_id", manual_submission_columns)
        self.assertIn("claimed_by", manual_submission_columns)
        self.assertIn("claimed_at", manual_submission_columns)
        self.assertIn("thread_id", manual_message_columns)
        self.assertIn("control_message_id", manual_message_columns)

        # Zero-mark subproblems should be accepted for unmarked/non-scoring questions.
        conn.execute("INSERT INTO seasons (id) VALUES (1)")
        conn.execute(
            "INSERT INTO problems (id, date, season, statement, answer, public) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "2026-01-01", 1, "Main", 0, False),
        )
        conn.execute(
            "INSERT INTO subproblems (potd_id, label, statement, marks, answer, manual_marking, order_index, public) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "A", "Unmarked", 0, None, True, 1, True),
        )
        marks = conn.execute("SELECT marks FROM subproblems WHERE potd_id = ? AND label = ?", (1, "A")).fetchone()[0]
        self.assertEqual(marks, 0)


if __name__ == "__main__":
    unittest.main()
