"""Fetch → hard filter → LLM screen. Each step updates the DB; report.py turns it into files."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

from . import config, history
from .db import DB, row_to_posting
from .llm import LLM
from .rules import (DAY, applicant_schools, apply_order, blocked_company, company_title_key, csv_category, hard_filter,
                    pick_resume, too_old)
from .screen import fit_score, rate_fit, screen
from .sources import simplify, speedyapply

log = logging.getLogger("jobagent")

# Stages recomputed from scratch on every fetch; everything else is a settled decision.
REEVALUATED = ("new", "filtered", "duplicate", "pending", "error")
# Settled stages that still lead somewhere (the apply queue, the manual list): a blocklisted company, or a posting
# older than max_age_days, is pulled out of them.
QUEUED = ("eligible", "unsure", "jd_missing", "manual", "blocked", "failed")
SOURCE_LABEL = {"Simplify": "SimplifyJobs", "SpeedyApply": "SpeedyApply"}


@dataclass
class FetchStats:
    per_source: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    new: int = 0
    stages: dict[str, int] = field(default_factory=dict)


@dataclass
class ScreenStats:
    attempted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    def merge(self, other: "ScreenStats") -> None:
        self.attempted += other.attempted
        for k, v in other.outcomes.items():
            self.outcomes[k] = self.outcomes.get(k, 0) + v


def fetch(db: DB) -> FetchStats:
    cfg, stats = config.settings(), FetchStats()
    term = cfg["term"]
    # SpeedyApply first so Simplify's richer fields (active flag, sponsorship, degrees) win on overlap.
    for name, module in (("SpeedyApply", speedyapply), ("Simplify", simplify)):
        key = name.lower()
        try:
            postings = module.fetch(cfg["sources"][key], term)
        except Exception as e:  # one broken source shouldn't stop the other
            log.error("拉取 %s 失败：%s", name, e)
            stats.errors[name] = str(e)
            continue
        stats.per_source[name] = len(postings)
        stats.new += sum(db.upsert(p) for p in postings)
    db.commit()
    stats.stages = refilter(db)
    return stats


def refilter(db: DB) -> dict[str, int]:
    """Apply hard rules, applications.csv history, and company+title dedup to every unsettled row."""
    cfg = config.settings()
    hist = history.load(config.APPLICATIONS_CSV)
    term, max_failures = cfg["term"], cfg["applying"]["max_failures"]
    blocklist = cfg.get("blocked_companies")
    max_age, now, pins = cfg.get("max_age_days"), time.time(), set(pinned(db))
    prof = config.profile()
    who = {"level": config.applicant_level(prof), "schools": applicant_schools(prof),
           "citizen": bool(prof["work_authorization"]["us_citizen_or_permanent_resident"])}

    def stale(r) -> str | None:  # a job you pinned is applied to whatever its age
        return None if r["id"] in pins else too_old(r["date_posted"], max_age, now)

    for r in db.rows(f"stage IN ({','.join('?' * len(QUEUED))})", QUEUED):
        if reason := blocked_company(r["company"], blocklist) or stale(r):
            db.update(r["id"], stage="filtered", reason=reason)  # back in REEVALUATED: returns once the rule lets it

    marks = ",".join("?" * len(REEVALUATED))
    taken: dict[str, str] = {}
    for r in db.rows(f"stage NOT IN ({marks})", REEVALUATED, order="date_posted DESC"):
        taken.setdefault(company_title_key(r["company"], r["title"]), r["id"])

    counts: dict[str, int] = {}
    for r in db.rows(f"stage IN ({marks})", REEVALUATED, order="date_posted DESC"):
        p = row_to_posting(r)
        key = company_title_key(p.company, p.title)
        reason = blocked_company(p.company, blocklist) or hard_filter(p, term, **who) or stale(r)  # keep the original reason
        if reason:
            stage = "filtered"
        elif blocked := hist.blocking_status(p.url, p.company, p.title, max_failures):
            stage, reason = "done", f"applications.csv 已有记录（{blocked}）"
        elif taken.get(key, r["id"]) != r["id"]:
            stage, reason = "duplicate", f"与 {taken[key]} 同公司同岗位"
        else:
            taken[key] = r["id"]
            # keep an earlier error visible until the retry actually runs
            stage = "error" if r["stage"] == "error" else "pending"
            reason = r["reason"] if stage == "error" else None
        if stage != r["stage"] or reason != r["reason"] or r["resume"] is None:
            db.update(r["id"], stage=stage, reason=reason, resume=pick_resume(p.title, p.category))
        counts[stage] = counts.get(stage, 0) + 1
    db.commit()
    return counts


def screen_pending(db: DB, limit: int | None = None, llm: LLM | None = None, ids: list[str] | None = None,
                   posted_after: float | None = None) -> ScreenStats:
    """LLM-screen the newest pending jobs posted within max_age_days, or only those posted after `posted_after`
    (or exactly `ids`, if they are still pending)."""
    cfg = config.settings()
    sc, term = cfg["screening"], cfg["term"]
    limit = sc["per_run"] if limit is None else limit
    if ids:
        rows = [r for r in (db.get(i) for i in ids) if r is not None and r["stage"] in ("pending", "error")]
    else:
        where, params = "stage IN ('pending', 'error') AND active = 1", ()
        if posted_after:
            where, params = where + " AND date_posted >= ?", (posted_after,)
        elif max_age := cfg.get("max_age_days"):
            where, params = where + " AND (date_posted = 0 OR date_posted >= ?)", (time.time() - max_age * DAY,)
        rows = db.rows(where, params, order="date_posted DESC", limit=limit)
    rows = [r for r in rows if not blocked_company(r["company"], cfg.get("blocked_companies"))]
    stats = ScreenStats(attempted=len(rows))
    if not rows:
        return stats

    llm, profile = llm or LLM(rps=sc.get("llm_rps", 1.0)), config.profile()
    csv_rows = []
    with ThreadPoolExecutor(max_workers=sc["workers"]) as pool:
        futures = {pool.submit(screen, r, llm, profile, term, sc["jd_max_chars"]): r for r in rows}
        for fut in as_completed(futures):
            r = futures[fut]
            attempts = r["screen_attempts"] + 1
            now = datetime.now().isoformat(timespec="seconds")
            try:
                res = fut.result()
            except Exception as e:
                stage = "unsure" if attempts >= sc["max_attempts"] else "error"
                reason = f"JD/LLM 出错 {attempts} 次：{type(e).__name__}: {str(e)[:160]}"
                db.update(r["id"], stage=stage, reason=reason, screen_attempts=attempts, screened_at=now)
                stats.outcomes[stage] = stats.outcomes.get(stage, 0) + 1
                log.warning("[%s] %s — %s：%s", stage, r["company"], r["title"], reason)
                continue
            db.update(r["id"], stage=res.stage, reason=res.reason, screen_attempts=attempts,
                      screen_json=json.dumps(res.data, ensure_ascii=False), screened_at=now,
                      fit=fit_score(res.data.get("llm") or {}))
            stats.outcomes[res.stage] = stats.outcomes.get(res.stage, 0) + 1
            log.info("[%s] %s — %s：%s", res.stage, r["company"], r["title"], res.reason)
            if res.stage == "skip":
                csv_rows.append({
                    "申请时间": history.now_str(), "公司名称": r["company"], "岗位名称": r["title"],
                    "类别": csv_category(r["category"] or ""), "使用简历": r["resume"] or "",
                    "申请链接": r["url"], "来源": SOURCE_LABEL.get(r["source"], r["source"]),
                    "状态": history.SKIPPED, "失败原因": f"筛选规则：{res.reason}",
                })
            db.commit()
    history.append(config.APPLICATIONS_CSV, csv_rows)
    return stats


class BacklogScreen(threading.Thread):
    """Screens older pending jobs on its own DB connection while today's applications run (user, 2026-11-01:
    当日投递和过往筛查同步进行). Shares the run's LLM, so both stay within one rate limit."""

    def __init__(self, db_path, limit: int, llm: LLM) -> None:
        super().__init__(name="screen-backlog", daemon=True)
        self.db_path, self.limit, self.llm, self.stats = db_path, limit, llm, ScreenStats()

    def run(self) -> None:
        db = DB(self.db_path)
        try:
            self.stats = screen_pending(db, self.limit, self.llm)
        except Exception:
            log.exception("后台筛选出错")
        finally:
            db.close()


def score_fit(db: DB, llm: LLM | None = None) -> int:
    """Give a fit score to eligible jobs that don't have one (screened before scores existed, or the reply lacked it).
    Returns how many got one; a job whose JD can't be read stays unrated and is tried again next time."""
    rows = db.rows("stage = 'eligible' AND active = 1 AND fit IS NULL")
    if not rows:
        return 0
    cfg = config.settings()
    sc, term = cfg["screening"], cfg["term"]
    llm, profile = llm or LLM(rps=sc.get("llm_rps", 1.0)), config.profile()
    log.info("给 %d 个可投岗位打匹配分……", len(rows))
    scored = 0
    with ThreadPoolExecutor(max_workers=sc["workers"]) as pool:
        futures = {pool.submit(rate_fit, r, llm, profile, term, sc["jd_max_chars"]): r for r in rows}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                fit = fut.result()
            except Exception as e:
                log.warning("匹配分失败 %s — %s：%s: %s", r["company"], r["title"], type(e).__name__, str(e)[:120])
                continue
            if fit is not None:
                db.update(r["id"], fit=fit)
                db.commit()
                scored += 1
    return scored


def pinned(db: DB) -> list[str]:
    """Jobs the user wants in the next run, in the order they were pinned (`jobagent mark ID next`)."""
    return json.loads(db.meta("apply_first") or "[]")


def unpin(db: DB, jid: str) -> None:
    if jid in (ids := pinned(db)):
        db.meta("apply_first", json.dumps([i for i in ids if i != jid]))


def queue_order(db: DB, rows: list) -> list:
    return apply_order(rows, time.time(), pinned(db))


def mark(db: DB, jid: str, decision: str, reason: str | None) -> None:
    """Manual override: queue a job again (apply), put it first in the next run (next), never apply (skip), or record
    that you applied by hand (applied)."""
    r = db.get(jid)
    if r is None:
        raise SystemExit(f"找不到岗位 {jid}")
    stage = {"apply": "eligible", "next": "eligible", "skip": "skip", "applied": "applied"}[decision]
    db.update(jid, stage=stage, reason=reason or f"人工标记为 {decision}")
    if decision == "next" and jid not in (ids := pinned(db)):
        db.meta("apply_first", json.dumps(ids + [jid]))
    elif decision != "next":
        unpin(db, jid)
    if stage == "applied":  # you applied by hand: record it so the agent never applies again
        history.append(config.APPLICATIONS_CSV, [{
            "申请时间": history.now_str(), "公司名称": r["company"], "岗位名称": r["title"],
            "类别": csv_category(r["category"] or ""), "使用简历": r["resume"] or "",
            "申请链接": r["url"], "来源": "手动投递", "状态": history.SUBMITTED, "失败原因": "",
        }])
    if stage == "skip":
        history.append(config.APPLICATIONS_CSV, [{
            "申请时间": history.now_str(), "公司名称": r["company"], "岗位名称": r["title"],
            "类别": csv_category(r["category"] or ""), "使用简历": r["resume"] or "",
            "申请链接": r["url"], "来源": SOURCE_LABEL.get(r["source"], r["source"]),
            "状态": history.SKIPPED, "失败原因": reason or "人工标记跳过",
        }])
    db.commit()
