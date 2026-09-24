"""
SQLite persistence layer for the adaptive learning app.

Schema
------
courses          one row per uploaded syllabus
topics            one row per topic extracted from a course's syllabus
quiz_attempts     one row per quiz a learner takes (initial assessment or practice)
material_cache    generated text material per (topic, level), so we don't
                   regenerate the same explanation every time a page reloads
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "adaptive_learning.db"

# Topic lifecycle:
#   not_started -> assessed -> (learning <-> needs_review) -> mastered
VALID_TOPIC_STATUSES = {
    "not_started",
    "assessed",
    "learning",
    "needs_review",
    "mastered",
}

VALID_LEVELS = {"beginner", "intermediate", "advanced"}

MASTERY_THRESHOLD = 0.60  # 60% — below this, the learner must relearn the topic


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS courses (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            filename      TEXT,
            raw_text      TEXT,
            phase         TEXT NOT NULL DEFAULT 'new',  -- new, assessing, learning, done
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS topics (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id     INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            order_index   INTEGER NOT NULL,
            name          TEXT NOT NULL,
            description   TEXT,
            status        TEXT NOT NULL DEFAULT 'not_started',
            level         TEXT,               -- beginner / intermediate / advanced
            best_score    REAL,               -- best practice-quiz score (0-1)
            attempts      INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id        INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
            phase           TEXT NOT NULL,     -- initial_assessment / practice
            score           REAL NOT NULL,     -- 0.0 - 1.0
            correct_count   INTEGER NOT NULL,
            total_questions INTEGER NOT NULL,
            timestamp       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS material_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
            level       TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE(topic_id, level)
        );
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- courses --

def create_course(conn, name: str, filename: str, raw_text: str) -> int:
    cur = conn.execute(
        "INSERT INTO courses (name, filename, raw_text, phase, created_at) "
        "VALUES (?, ?, ?, 'new', ?)",
        (name, filename, raw_text, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_course(conn, course_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    return dict(row) if row else None


def list_courses(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM courses ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_course_phase(conn, course_id: int, phase: str) -> None:
    conn.execute("UPDATE courses SET phase = ? WHERE id = ?", (phase, course_id))
    conn.commit()


def delete_course(conn, course_id: int) -> None:
    conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    conn.commit()


# ----------------------------------------------------------------- topics --

def add_topic(conn, course_id: int, order_index: int, name: str, description: str) -> int:
    cur = conn.execute(
        "INSERT INTO topics (course_id, order_index, name, description, status, attempts) "
        "VALUES (?, ?, ?, ?, 'not_started', 0)",
        (course_id, order_index, name, description),
    )
    conn.commit()
    return cur.lastrowid


def get_topics(conn, course_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM topics WHERE course_id = ? ORDER BY order_index ASC",
        (course_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_topic(conn, topic_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    return dict(row) if row else None


def update_topic(
    conn,
    topic_id: int,
    status: str | None = None,
    level: str | None = None,
    best_score: float | None = None,
    increment_attempts: bool = False,
) -> None:
    if status is not None and status not in VALID_TOPIC_STATUSES:
        raise ValueError(f"invalid topic status: {status}")
    if level is not None and level not in VALID_LEVELS:
        raise ValueError(f"invalid level: {level}")

    fields, values = [], []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if level is not None:
        fields.append("level = ?")
        values.append(level)
    if best_score is not None:
        fields.append("best_score = ?")
        values.append(best_score)
    if increment_attempts:
        fields.append("attempts = attempts + 1")

    if not fields:
        return
    values.append(topic_id)
    conn.execute(f"UPDATE topics SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()


def reorder_topics_by_score(conn, course_id: int) -> None:
    """Push weaker topics (lower initial score / lower level) earlier in the
    learning path, so the flow tackles what the learner knows least first.
    Ties keep the original syllabus order."""
    topics = get_topics(conn, course_id)
    level_rank = {"beginner": 0, "intermediate": 1, "advanced": 2, None: 0}
    original_order = {t["id"]: t["order_index"] for t in topics}
    ordered = sorted(
        topics,
        key=lambda t: (level_rank.get(t["level"], 0), original_order[t["id"]]),
    )
    for new_index, t in enumerate(ordered):
        conn.execute("UPDATE topics SET order_index = ? WHERE id = ?", (new_index, t["id"]))
    conn.commit()


# ----------------------------------------------------------- quiz_attempts --

def record_quiz_attempt(
    conn, topic_id: int, phase: str, correct_count: int, total_questions: int
) -> int:
    score = correct_count / total_questions if total_questions else 0.0
    cur = conn.execute(
        "INSERT INTO quiz_attempts (topic_id, phase, score, correct_count, total_questions, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (topic_id, phase, score, correct_count, total_questions, _now()),
    )
    conn.commit()

    topic = get_topic(conn, topic_id)
    best = topic["best_score"]
    if best is None or score > best:
        update_topic(conn, topic_id, best_score=score)

    return cur.lastrowid


def get_quiz_attempts(conn, topic_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM quiz_attempts WHERE topic_id = ? ORDER BY timestamp ASC",
        (topic_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def score_to_level(score: float) -> str:
    """Map an initial-assessment score (0-1) to a starting difficulty level."""
    if score < 0.40:
        return "beginner"
    if score < 0.75:
        return "intermediate"
    return "advanced"


# --------------------------------------------------------- material_cache --

def get_cached_material(conn, topic_id: int, level: str) -> str | None:
    row = conn.execute(
        "SELECT content FROM material_cache WHERE topic_id = ? AND level = ?",
        (topic_id, level),
    ).fetchone()
    return row["content"] if row else None


def cache_material(conn, topic_id: int, level: str, content: str) -> None:
    conn.execute(
        "INSERT INTO material_cache (topic_id, level, content, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(topic_id, level) DO UPDATE SET content = excluded.content, created_at = excluded.created_at",
        (topic_id, level, content, _now()),
    )
    conn.commit()
