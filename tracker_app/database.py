"""
database.py
============

Local SQLite storage for the GATE question tracker:

  - which PDF(s) you've indexed ("sources")
  - the parsed questions from each PDF (title/answer/link/page/chapter)
  - your per-question progress: level (L1/L2/L3) + free-text notes

A question is identified by (source_id, question_id) rather than just
question_id, because the same id like "3.6.27" means something different
in each subject PDF. A source is identified by a content fingerprint, so
re-opening the same PDF from a different folder (or after renaming it)
still finds your existing progress.

By default the database lives at ~/.gate_tracker/gate_tracker.db so it
survives independently of wherever your PDFs happen to be.
"""

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VALID_LEVELS = ("L1", "L2", "L3")

DEFAULT_DB_PATH = Path.home() / ".gate_tracker" / "gate_tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    filepath    TEXT,
    fingerprint TEXT UNIQUE NOT NULL,
    added_at    TEXT NOT NULL,
    last_opened TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    source_id     INTEGER NOT NULL,
    question_id   TEXT NOT NULL,
    chapter_num   INTEGER,
    chapter_name  TEXT,
    title         TEXT,
    answer        TEXT,
    url           TEXT,
    page          INTEGER,
    y_top         REAL,
    y_bottom      REAL,
    PRIMARY KEY (source_id, question_id),
    FOREIGN KEY (source_id) REFERENCES pdf_sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS progress (
    source_id     INTEGER NOT NULL,
    question_id   TEXT NOT NULL,
    level         TEXT,
    notes         TEXT,
    updated_at    TEXT,
    PRIMARY KEY (source_id, question_id),
    FOREIGN KEY (source_id) REFERENCES pdf_sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS activity_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL,
    question_id   TEXT NOT NULL,
    level         TEXT NOT NULL,
    logged_at     TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES pdf_sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_questions_chapter
    ON questions (source_id, chapter_name);
CREATE INDEX IF NOT EXISTS idx_progress_level
    ON progress (source_id, level);
CREATE INDEX IF NOT EXISTS idx_activity_day
    ON activity_log (source_id, logged_at);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add columns introduced after a database may already have been
        created, without touching anyone's existing tracked data."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(questions)")}
        if "y_top" not in cols:
            self.conn.execute("ALTER TABLE questions ADD COLUMN y_top REAL")
        if "y_bottom" not in cols:
            self.conn.execute("ALTER TABLE questions ADD COLUMN y_bottom REAL")

    def close(self):
        self.conn.close()

    # -- sources -------------------------------------------------------

    def get_or_create_source(self, filename, filepath, fingerprint):
        cur = self.conn.execute(
            "SELECT id FROM pdf_sources WHERE fingerprint = ?", (fingerprint,)
        )
        row = cur.fetchone()
        now = _now()
        if row:
            source_id = row["id"]
            self.conn.execute(
                "UPDATE pdf_sources SET filename=?, filepath=?, last_opened=? "
                "WHERE id=?",
                (filename, filepath, now, source_id),
            )
        else:
            cur = self.conn.execute(
                "INSERT INTO pdf_sources (filename, filepath, fingerprint, "
                "added_at, last_opened) VALUES (?, ?, ?, ?, ?)",
                (filename, filepath, fingerprint, now, now),
            )
            source_id = cur.lastrowid
        self.conn.commit()
        return source_id

    def list_sources(self):
        cur = self.conn.execute(
            "SELECT id, filename, filepath, added_at, last_opened "
            "FROM pdf_sources ORDER BY last_opened DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    # -- questions (parsed PDF content, refreshed on every load) -------

    def sync_questions(self, source_id, index):
        """index is the dict returned by indexer.build_index()."""
        rows = [
            (
                source_id,
                qid,
                e.get("chapter_num"),
                e.get("chapter_name"),
                e.get("title"),
                e.get("answer"),
                e.get("url"),
                e.get("page"),
                e.get("y_top"),
                e.get("y_bottom"),
            )
            for qid, e in index.items()
        ]
        self.conn.executemany(
            """
            INSERT INTO questions
                (source_id, question_id, chapter_num, chapter_name,
                 title, answer, url, page, y_top, y_bottom)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, question_id) DO UPDATE SET
                chapter_num = excluded.chapter_num,
                chapter_name = excluded.chapter_name,
                title = excluded.title,
                answer = excluded.answer,
                url = excluded.url,
                page = excluded.page,
                y_top = excluded.y_top,
                y_bottom = excluded.y_bottom
            """,
            rows,
        )
        self.conn.commit()

    def get_chapters(self, source_id):
        """Returns [(chapter_num, chapter_name, question_count), ...]."""
        cur = self.conn.execute(
            "SELECT chapter_num, chapter_name, COUNT(*) as cnt "
            "FROM questions WHERE source_id=? "
            "GROUP BY chapter_num, chapter_name ORDER BY chapter_num",
            (source_id,),
        )
        return [(r["chapter_num"], r["chapter_name"], r["cnt"]) for r in cur.fetchall()]

    # -- progress (your L1/L2/L3 + notes) -------------------------------

    def get_progress(self, source_id, question_id):
        cur = self.conn.execute(
            "SELECT level, notes, updated_at FROM progress "
            "WHERE source_id=? AND question_id=?",
            (source_id, question_id),
        )
        row = cur.fetchone()
        if not row:
            return {"level": None, "notes": "", "updated_at": None}
        return {
            "level": row["level"],
            "notes": row["notes"] or "",
            "updated_at": row["updated_at"],
        }

    def set_level(self, source_id, question_id, level):
        if level is not None and level not in VALID_LEVELS:
            raise ValueError(f"level must be one of {VALID_LEVELS} or None")
        now = _now()
        self.conn.execute(
            """
            INSERT INTO progress (source_id, question_id, level, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id, question_id) DO UPDATE SET
                level = excluded.level,
                updated_at = excluded.updated_at
            """,
            (source_id, question_id, level, now),
        )
        if level is not None:
            # only log actively marking a question, not clearing it --
            # this is what powers the "solved on what day" activity view.
            self.conn.execute(
                "INSERT INTO activity_log (source_id, question_id, level, logged_at) "
                "VALUES (?, ?, ?, ?)",
                (source_id, question_id, level, now),
            )
        self.conn.commit()

    def set_notes(self, source_id, question_id, notes):
        self.conn.execute(
            """
            INSERT INTO progress (source_id, question_id, notes, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id, question_id) DO UPDATE SET
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (source_id, question_id, notes, _now()),
        )
        self.conn.commit()

    # -- activity log (what you marked, on what day) --------------------

    def get_daily_activity(self, source_id=None):
        """
        Returns [{"date": "2026-07-30", "total": N, "L1": a, "L2": b, "L3": c}, ...]
        sorted most-recent-first.

        Timestamps are stored in UTC but bucketed into days using your
        local timezone (so a late-night session lands on the day you'd
        actually call it, not UTC's). If the same question gets marked
        more than once on the same day, only its last mark that day
        counts -- re-marking something doesn't inflate the count.
        """
        sql = "SELECT source_id, question_id, level, logged_at FROM activity_log"
        params = []
        if source_id is not None:
            sql += " WHERE source_id = ?"
            params.append(source_id)
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, params).fetchall()

        latest_per_day_q = {}  # (day, source_id, question_id) -> (logged_at, level)
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["logged_at"]).astimezone()
            except Exception:
                continue
            day = dt.date().isoformat()
            key = (day, r["source_id"], r["question_id"])
            prev = latest_per_day_q.get(key)
            # rows are processed in insertion order, so on a timestamp tie
            # (same-second re-marks) the later row -- i.e. this one -- is
            # the actual latest and should win.
            if prev is None or r["logged_at"] >= prev[0]:
                latest_per_day_q[key] = (r["logged_at"], r["level"])

        daily = {}
        for (day, _sid, _qid), (_ts, level) in latest_per_day_q.items():
            d = daily.setdefault(day, {"date": day, "total": 0, "L1": 0, "L2": 0, "L3": 0})
            if level in d:
                d[level] += 1
            d["total"] += 1

        return sorted(daily.values(), key=lambda d: d["date"], reverse=True)

    # -- dashboard -------------------------------------------------------

    def query_questions(self, source_id, chapter_name=None, levels=None):
        """
        levels: iterable subset of {'L1','L2','L3','NONE'} (NONE = not
        attempted yet), or None/empty for no level filtering.
        """
        sql = """
            SELECT q.source_id, s.filepath as source_filepath,
                   s.filename as source_filename,
                   q.question_id, q.chapter_name, q.title, q.answer,
                   q.url, q.page, q.y_top, q.y_bottom,
                   p.level, p.notes, p.updated_at
            FROM questions q
            JOIN pdf_sources s ON s.id = q.source_id
            LEFT JOIN progress p
                ON p.source_id = q.source_id AND p.question_id = q.question_id
            WHERE q.source_id = ?
        """
        params = [source_id]

        if chapter_name and chapter_name != "All subjects":
            sql += " AND q.chapter_name = ?"
            params.append(chapter_name)

        if levels:
            levels = set(levels)
            clauses = []
            if "NONE" in levels:
                clauses.append("p.level IS NULL")
                levels.discard("NONE")
            if levels:
                placeholders = ",".join("?" for _ in levels)
                clauses.append(f"p.level IN ({placeholders})")
                params.extend(levels)
            if clauses:
                sql += " AND (" + " OR ".join(clauses) + ")"

        cur = self.conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        rows.sort(key=lambda r: tuple(int(p) for p in r["question_id"].split(".")))
        return rows

    def get_stats(self, source_id, chapter_name=None):
        sql = """
            SELECT p.level, COUNT(*) as cnt
            FROM questions q
            LEFT JOIN progress p
                ON p.source_id = q.source_id AND p.question_id = q.question_id
            WHERE q.source_id = ?
        """
        params = [source_id]
        if chapter_name and chapter_name != "All subjects":
            sql += " AND q.chapter_name = ?"
            params.append(chapter_name)
        sql += " GROUP BY p.level"

        cur = self.conn.execute(sql, params)
        stats = {"total": 0, "L1": 0, "L2": 0, "L3": 0, "NONE": 0}
        for row in cur.fetchall():
            level = row["level"] or "NONE"
            stats[level] = row["cnt"]
            stats["total"] += row["cnt"]
        return stats

    def get_stats_by_chapter(self, source_id):
        """{'chapter_name': {'total','L1','L2','L3','NONE'}, ...} for one source."""
        sql = """
            SELECT q.chapter_name, p.level, COUNT(*) as cnt
            FROM questions q
            LEFT JOIN progress p
                ON p.source_id = q.source_id AND p.question_id = q.question_id
            WHERE q.source_id = ?
            GROUP BY q.chapter_name, p.level
        """
        out = {}
        for row in self.conn.execute(sql, (source_id,)):
            chap = row["chapter_name"]
            stats = out.setdefault(chap, {"total": 0, "L1": 0, "L2": 0, "L3": 0, "NONE": 0})
            level = row["level"] or "NONE"
            stats[level] = row["cnt"]
            stats["total"] += row["cnt"]
        return out

    # -- unified (all sources) -----------------------------------------

    def get_unified_chapters(self):
        """Distinct subjects across every loaded PDF, with question counts."""
        cur = self.conn.execute(
            "SELECT chapter_name, COUNT(*) as cnt FROM questions "
            "GROUP BY chapter_name ORDER BY chapter_name"
        )
        return [(r["chapter_name"], r["cnt"]) for r in cur.fetchall()]

    def get_unified_stats(self, chapter_name=None):
        sql = """
            SELECT p.level, COUNT(*) as cnt
            FROM questions q
            LEFT JOIN progress p
                ON p.source_id = q.source_id AND p.question_id = q.question_id
        """
        params = []
        if chapter_name and chapter_name != "All subjects":
            sql += " WHERE q.chapter_name = ?"
            params.append(chapter_name)
        sql += " GROUP BY p.level"

        cur = self.conn.execute(sql, params)
        stats = {"total": 0, "L1": 0, "L2": 0, "L3": 0, "NONE": 0}
        for row in cur.fetchall():
            level = row["level"] or "NONE"
            stats[level] = row["cnt"]
            stats["total"] += row["cnt"]
        return stats

    def get_unified_stats_by_chapter(self):
        """{'chapter_name': {'total','L1','L2','L3','NONE'}, ...} across all sources."""
        sql = """
            SELECT q.chapter_name, p.level, COUNT(*) as cnt
            FROM questions q
            LEFT JOIN progress p
                ON p.source_id = q.source_id AND p.question_id = q.question_id
            GROUP BY q.chapter_name, p.level
        """
        out = {}
        for row in self.conn.execute(sql):
            chap = row["chapter_name"]
            stats = out.setdefault(chap, {"total": 0, "L1": 0, "L2": 0, "L3": 0, "NONE": 0})
            level = row["level"] or "NONE"
            stats[level] = row["cnt"]
            stats["total"] += row["cnt"]
        return out

    def query_unified_questions(self, chapter_name=None, levels=None):
        """Like query_questions(), but spans every loaded PDF. Each row also
        carries source_id / source_filename / source_filepath."""
        sql = """
            SELECT q.source_id, s.filename as source_filename,
                   s.filepath as source_filepath,
                   q.question_id, q.chapter_name, q.title, q.answer,
                   q.url, q.page, q.y_top, q.y_bottom,
                   p.level, p.notes, p.updated_at
            FROM questions q
            JOIN pdf_sources s ON s.id = q.source_id
            LEFT JOIN progress p
                ON p.source_id = q.source_id AND p.question_id = q.question_id
            WHERE 1=1
        """
        params = []

        if chapter_name and chapter_name != "All subjects":
            sql += " AND q.chapter_name = ?"
            params.append(chapter_name)

        if levels:
            levels = set(levels)
            clauses = []
            if "NONE" in levels:
                clauses.append("p.level IS NULL")
                levels.discard("NONE")
            if levels:
                placeholders = ",".join("?" for _ in levels)
                clauses.append(f"p.level IN ({placeholders})")
                params.extend(levels)
            if clauses:
                sql += " AND (" + " OR ".join(clauses) + ")"

        cur = self.conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        rows.sort(
            key=lambda r: (
                r["chapter_name"] or "",
                tuple(int(p) for p in r["question_id"].split(".")),
            )
        )
        return rows

    # -- backup / restore --------------------------------------------------

    def backup_to(self, dest_path):
        """Safe, consistent copy of the whole database via SQLite's own
        backup API (not a raw file copy, which could catch a half-written
        page)."""
        dest_conn = sqlite3.connect(dest_path)
        try:
            with dest_conn:
                self.conn.backup(dest_conn)
        finally:
            dest_conn.close()

    def restore_from(self, src_path):
        """Overwrites the current database with a previously-made backup."""
        # sanity-check that this actually looks like one of our backups
        test_conn = sqlite3.connect(src_path)
        try:
            tables = {
                r[0]
                for r in test_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            test_conn.close()
        if not {"pdf_sources", "questions", "progress"}.issubset(tables):
            raise ValueError("That file doesn't look like a gate_tracker backup.")

        self.conn.close()
        shutil.copy2(src_path, self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
