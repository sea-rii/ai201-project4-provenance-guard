"""
db.py — SQLite setup and audit log helpers for Provenance Guard.

All database access goes through this module. The rest of the app
never imports sqlite3 directly.
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "provenance.db")


def get_conn():
    """Return a connection with row_factory set so rows behave like dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Create tables if they don't exist.
    Safe to call on every app startup — uses IF NOT EXISTS.
    """
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id          TEXT,
                text                TEXT NOT NULL,

                -- Signal scores (0.0–1.0 each; NULL until computed)
                llm_score           REAL,
                stylometric_score   REAL,
                ngram_score         REAL,

                -- Combined result
                combined_score      REAL,
                label               TEXT,
                signals_disagree    INTEGER DEFAULT 0,  -- boolean 0/1
                low_confidence_reason TEXT,

                -- Appeal fields
                appealed            INTEGER DEFAULT 0,  -- boolean 0/1
                appeal_reason       TEXT,
                appealed_at         TEXT,

                -- Timestamps (ISO 8601)
                submitted_at        TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'classified'
            );

            CREATE TABLE IF NOT EXISTS certificates (
                id                  TEXT PRIMARY KEY,   -- UUID
                submission_id       INTEGER NOT NULL REFERENCES submissions(id),
                creator_id          TEXT,
                issued_at           TEXT NOT NULL,
                fingerprint_summary TEXT,
                confidence          REAL,
                badge_text          TEXT
            );
        """)


def insert_submission(creator_id, text):
    """
    Insert a new submission row before analysis runs.
    Returns the new row's integer ID.
    This ensures every submission has an ID even if detection fails.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO submissions (creator_id, text, submitted_at, status)
               VALUES (?, ?, ?, 'pending')""",
            (creator_id, text, now),
        )
        return cur.lastrowid


def update_submission_scores(
    submission_id,
    llm_score,
    stylometric_score,
    ngram_score,
    combined_score,
    label,
    signals_disagree,
    low_confidence_reason=None,
):
    """Write detection results back to the submission row."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE submissions
               SET llm_score=?, stylometric_score=?, ngram_score=?,
                   combined_score=?, label=?, signals_disagree=?,
                   low_confidence_reason=?, status='classified'
               WHERE id=?""",
            (
                llm_score,
                stylometric_score,
                ngram_score,
                combined_score,
                label,
                int(signals_disagree),
                low_confidence_reason,
                submission_id,
            ),
        )


def get_submission(submission_id):
    """Return a single submission row as a dict, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id=?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None


def set_appeal(submission_id, reason):
    """
    Mark a submission as appealed.
    Returns False if it doesn't exist or is already appealed.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT appealed FROM submissions WHERE id=?", (submission_id,)
        ).fetchone()
        if row is None:
            return "not_found"
        if row["appealed"]:
            return "already_appealed"
        conn.execute(
            """UPDATE submissions
               SET appealed=1, appeal_reason=?, appealed_at=?, status='appealed'
               WHERE id=?""",
            (reason, now, submission_id),
        )
        return "ok"


def get_log(appealed_only=False, limit=100):
    """
    Return recent audit log entries as a list of dicts.
    Excludes the raw text to keep log responses lean.
    """
    query = """
        SELECT id, creator_id, submitted_at, status, label,
               combined_score, llm_score, stylometric_score, ngram_score,
               signals_disagree, low_confidence_reason,
               appealed, appeal_reason, appealed_at
        FROM submissions
    """
    if appealed_only:
        query += " WHERE appealed=1"
    query += " ORDER BY submitted_at DESC LIMIT ?"

    with get_conn() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]


def insert_certificate(cert_id, submission_id, creator_id,
                       issued_at, fingerprint, confidence, badge):
    """Insert a new Verified Human certificate."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO certificates
               (id, submission_id, creator_id, issued_at,
                fingerprint_summary, confidence, badge_text)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cert_id, submission_id, creator_id, issued_at,
             fingerprint, confidence, badge),
        )


def get_certificate(cert_id):
    """Return a certificate by UUID, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM certificates WHERE id=?", (cert_id,)
        ).fetchone()
        return dict(row) if row else None


def get_analytics():
    """
    Aggregate stats for the analytics dashboard (SF-3).
    Returns a dict with detection distribution, appeal rate,
    signal disagreement rate, and daily submission counts.
    """
    with get_conn() as conn:
        # Total submissions
        total = conn.execute("SELECT COUNT(*) FROM submissions WHERE status != 'pending'").fetchone()[0]

        if total == 0:
            return {
                "total_submissions": 0,
                "label_distribution": {},
                "appeal_rate_pct": 0,
                "signal_disagreement_rate_pct": 0,
                "daily_counts": [],
                "avg_confidence": 0,
            }

        # Label distribution
        label_rows = conn.execute("""
            SELECT label, COUNT(*) as cnt
            FROM submissions
            WHERE status != 'pending' AND label IS NOT NULL
            GROUP BY label
        """).fetchall()
        label_dist = {r["label"]: r["cnt"] for r in label_rows}

        # Appeal rate
        appealed = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE appealed=1"
        ).fetchone()[0]

        # Signal disagreement rate
        disagree = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE signals_disagree=1"
        ).fetchone()[0]

        # Daily submission counts (last 14 days)
        daily = conn.execute("""
            SELECT DATE(submitted_at) as day, COUNT(*) as cnt
            FROM submissions
            WHERE status != 'pending'
            GROUP BY DATE(submitted_at)
            ORDER BY day DESC
            LIMIT 14
        """).fetchall()

        # Average combined score
        avg_score = conn.execute(
            "SELECT AVG(combined_score) FROM submissions WHERE combined_score IS NOT NULL"
        ).fetchone()[0] or 0

        return {
            "total_submissions":              total,
            "label_distribution":             label_dist,
            "appeal_rate_pct":                round((appealed / total) * 100, 1),
            "signal_disagreement_rate_pct":   round((disagree / total) * 100, 1),
            "daily_counts":                   [dict(r) for r in daily],
            "avg_combined_score":             round(avg_score, 4),
            "total_appealed":                 appealed,
            "total_disagree":                 disagree,
        }