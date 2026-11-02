"""SQLite store for every posting seen and where it is in the pipeline.

Stages:
  new        just inserted, not yet checked
  filtered   rejected by a hard rule (reason says which)
  duplicate  same company + title as another row
  done       applications.csv already has 已提交/跳过 (or too many failures)
  pending    passed hard rules, waiting for the JD/LLM screen
  error      JD fetch or LLM failed; retried next run
  eligible   LLM says apply
  skip       LLM (or a form rule) says skip
  unsure     needs a human look
  jd_missing could not read the JD without a browser
  applied    application submitted
  failed     technical failure while applying; retried (max 3 in applications.csv)
  blocked    needs a human (CAPTCHA, login, unanswerable question, unconfirmed submit)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Posting
from .rules import detect_ats, job_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    ats             TEXT NOT NULL,
    category        TEXT,
    locations       TEXT NOT NULL DEFAULT '[]',
    date_posted     INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    sponsorship     TEXT,
    degrees         TEXT NOT NULL DEFAULT '[]',
    terms           TEXT NOT NULL DEFAULT '[]',
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'new',
    reason          TEXT,
    resume          TEXT,
    screen_attempts INTEGER NOT NULL DEFAULT 0,
    screen_json     TEXT,
    screened_at     TEXT,
    fit             INTEGER             -- 0-100: how well the job matches the resume (LLM); NULL = not rated yet
);
CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(stage, date_posted);
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    at          TEXT NOT NULL,
    mode        TEXT NOT NULL,      -- dry_run | submit
    status      TEXT NOT NULL,      -- submitted | dry_run | skip | blocked | unconfirmed | failed
    reason      TEXT,
    answers     TEXT,               -- JSON list of {label, value, basis, error}
    screenshots TEXT,               -- JSON list of paths
    final_url   TEXT
);
CREATE TABLE IF NOT EXISTS inbox_items (     -- recruiting emails that may need the applicant (jobagent.inbox)
    msg_id      TEXT PRIMARY KEY,            -- Message-ID header
    received    TEXT NOT NULL,               -- local ISO time
    sender      TEXT,
    subject     TEXT,
    company     TEXT,
    kind        TEXT NOT NULL,               -- assessment | interview | task | rejection | update | completed | other
    action      INTEGER NOT NULL DEFAULT 0,  -- 1 = the applicant has to do something
    deadline    TEXT,                        -- YYYY-MM-DD or ''
    link        TEXT,
    summary     TEXT,
    status      TEXT NOT NULL DEFAULT 'open',-- open | done | closed (auto) | expired
    note        TEXT
);
CREATE INDEX IF NOT EXISTS inbox_status ON inbox_items(status, received);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)  # the daily run screens on a second connection while applying
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if "fit" not in {c[1] for c in self.conn.execute("PRAGMA table_info(jobs)")}:  # databases from before fit
            self.conn.execute("ALTER TABLE jobs ADD COLUMN fit INTEGER")

    def upsert(self, p: Posting) -> bool:
        """Insert or refresh a posting. Returns True if it is new. Pipeline state is never touched."""
        jid, now = job_id(p.url), _now()
        fields = dict(
            source=p.source, company=p.company, title=p.title, url=p.url, ats=detect_ats(p.url),
            category=p.category, locations=json.dumps(p.locations, ensure_ascii=False),
            date_posted=p.date_posted, active=int(p.active), sponsorship=p.sponsorship,
            degrees=json.dumps(p.degrees), terms=json.dumps(p.terms),
        )
        exists = self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone()
        if exists:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            self.conn.execute(f"UPDATE jobs SET {sets}, last_seen = :now WHERE id = :id", {**fields, "now": now, "id": jid})
            return False
        cols = ", ".join(fields)
        self.conn.execute(
            f"INSERT INTO jobs (id, {cols}, first_seen, last_seen) VALUES (:id, :{', :'.join(fields)}, :now, :now)",
            {**fields, "now": now, "id": jid},
        )
        return True

    def rows(self, where: str = "1", params: tuple = (), order: str = "date_posted DESC", limit: int | None = None):
        sql = f"SELECT * FROM jobs WHERE {where} ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, params).fetchall()

    def get(self, jid: str):
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()

    def update(self, jid: str, **fields) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = :id", {**fields, "id": jid})

    def add_attempt(self, job_id: str, mode: str, status: str, reason: str, answers: list, screenshots: list, final_url: str) -> None:
        self.conn.execute(
            "INSERT INTO attempts (job_id, at, mode, status, reason, answers, screenshots, final_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, _now(), mode, status, reason, json.dumps(answers, ensure_ascii=False), json.dumps(screenshots), final_url),
        )

    def meta(self, key: str, value: str | None = None) -> str | None:
        """Read a value from the meta table, or store one when `value` is given."""
        if value is None:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, value))
        self.conn.commit()
        return value

    def stage_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT stage, COUNT(*) FROM jobs GROUP BY stage").fetchall())

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def row_to_posting(r: sqlite3.Row) -> Posting:
    return Posting(
        source=r["source"], company=r["company"], title=r["title"], url=r["url"],
        locations=json.loads(r["locations"]), category=r["category"] or "",
        date_posted=r["date_posted"], active=bool(r["active"]), sponsorship=r["sponsorship"] or "",
        degrees=json.loads(r["degrees"]), terms=json.loads(r["terms"]),
    )
