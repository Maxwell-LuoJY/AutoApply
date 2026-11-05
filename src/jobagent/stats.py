"""Cumulative application stats for the daily report (the user, 2026-11-04): since the day applying started
(settings.yaml `daily.stats_since`), how many applications in total, how many the agent sent, and how many online
assessments, interviews and rejections came back. Everything is recomputed from the stored inbox items and
applications.csv on every run, so the totals grow as new mail is scanned.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime, timedelta

from . import config
from .db import DB
from .inbox import _company_key, _tenant_companies, applied_mails, sender_company

_GENERIC = {"inc", "llc", "ltd", "corp", "corporation", "company", "group", "the", "and", "global", "international",
            "technologies", "technology", "systems", "services", "holdings", "co"}
ROWS = [("total", "投递总数"), ("agent", "其中 agent 投递"), ("oa", "OA / 测评"), ("interview", "面试邀请"),
        ("rejection", "被拒")]


def since_date() -> date:
    raw = str((config.settings().get("daily") or {}).get("stats_since") or "").strip()
    return date.fromisoformat(raw) if raw else date.min


def _words(company: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in _GENERIC]
    return [w for w in words if len(w) > 2][:2] or words[:1]  # "HP Inc." → hp


def agent_submissions(since: date) -> list[tuple[datetime, str]]:
    """(time, company) of every application the agent itself submitted (not ones you recorded with `mark applied`)."""
    if not config.APPLICATIONS_CSV.exists():
        return []
    out = []
    with config.APPLICATIONS_CSV.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("状态") != "已提交" or r.get("来源") == "手动投递":
                continue
            try:
                t = datetime.strptime(r["申请时间"][:16], "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            if t.date() >= since:
                out.append((t, r.get("公司名称", "")))
    return sorted(out)


def _distinct(db: DB, kind: str, since: date, before: date | None) -> int:
    """Companies that sent this kind of mail: an assessment and its reminders count once."""
    rows = db.conn.execute("select company, subject, received from inbox_items where kind=? and substr(received, 1, 10) >= ?",
                           (kind, since.isoformat())).fetchall()
    keys = {_company_key(c) or re.sub(r"\W+", "", (s or "").lower())[:40]
            for c, s, r in rows if before is None or r[:10] < before.isoformat()}
    return len(keys)


def _rejections(db: DB, since: date, before: date | None) -> int:
    rows = db.conn.execute("select company, subject, received from inbox_items where kind='rejection' "
                           "and substr(received, 1, 10) >= ?", (since.isoformat(),)).fetchall()
    return len({(_company_key(c), re.sub(r"\W+", "", (s or "").lower())[:40], r[:10])
                for c, s, r in rows if before is None or r[:10] < before.isoformat()})


def cumulative(db: DB, since: date | None = None, before: date | None = None) -> dict[str, int]:
    """The counts, from `since` up to (not including) `before`; `before=None` means up to now.
    Total applications = confirmation emails, plus agent submissions for which no confirmation came (Vizient sends none)."""
    since = since or since_date()
    mails = [m for m in applied_mails(db, since=since) if before is None or m[0].date() < before]
    agent = [a for a in agent_submissions(since) if before is None or a[0].date() < before]
    tenants = _tenant_companies(db)  # gaig@myworkday.com is Great American
    texts = [f"{sender} {subject} {sender_company(sender, subject, tenants)}".lower() for _, sender, subject in mails]
    used: set[int] = set()
    unmatched = 0
    for t, company in agent:  # each confirmation email accounts for at most one submission
        words = _words(company)
        hit = next((i for i, (mt, _, _) in enumerate(mails) if i not in used and words
                    and all(re.search(rf"\b{re.escape(w)}", texts[i]) for w in words)
                    and t - timedelta(minutes=15) <= mt.replace(tzinfo=None) <= t + timedelta(days=2)), None)
        if hit is None:
            unmatched += 1
        else:
            used.add(hit)
    return {"total": len(mails) + unmatched, "agent": len(agent), "oa": _distinct(db, "assessment", since, before),
            "interview": _distinct(db, "interview", since, before), "rejection": _rejections(db, since, before)}


def section(db: DB, today: date | None = None) -> list[str]:
    """Markdown for the daily report."""
    today = today or date.today()
    since = since_date()
    now, before = cumulative(db, since), cumulative(db, since, before=today)
    start = "最早的记录" if since == date.min else f"{since:%Y-%m-%d}"
    out = [f"## 📊 累计统计（{start}起）", "", "| 项目 | 累计 | 今天新增 |", "|---|---|---|"]
    out += [f"| {label} | **{now[k]}** | +{now[k] - before[k]} |" for k, label in ROWS]
    out += ["", "投递总数按确认邮件算，再加上 agent 提交了、但公司没发确认信的；agent 投递按 applications.csv 里 agent 自己提交的记录算。"
            "OA 和面试按公司去重，同一件事的提醒邮件不重复算。每次运行扫描新邮件后，数字自动累加。", ""]
    return out
