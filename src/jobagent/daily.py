"""The unattended daily run (`jobagent daily`), started each morning by a scheduled Claude conversation:

new listings from both GitHub lists → hard rules → LLM screening → real applications → daily_reports/YYYY-MM-DD.md.
Jobs posted in the last 24 hours are screened first and applied to first; older ones (up to max_age_days) are screened
in the background while the applications run.
The report ends with every job that still needs a person (link + reason), plus the questions only the user can answer;
the scheduled conversation shows that list to the user.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, pipeline, report
from .rules import DAY
from .db import DB

log = logging.getLogger("jobagent")

REPORT_DIR = config.ROOT / "daily_reports"
_GENERIC = {"inc", "llc", "ltd", "corp", "corporation", "company", "group", "the", "and", "global", "international",
            "technologies", "technology", "systems", "services", "health", "insurance", "financial", "bank"}


def _company_words(company: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) > 2 and w not in _GENERIC][:2]


def _cell(s: str) -> str:
    return (s or "").replace("\n", " ").strip()


def run_daily(db: DB, max_submissions: int | None = None, workers: int | None = None, mode: str = "submit") -> Path:
    cfg = config.settings().get("daily") or {}
    started = datetime.now(timezone.utc)
    from . import inbox
    from .llm import LLM
    llm = LLM(rps=config.settings()["screening"].get("llm_rps", 1.0))
    log.info("每日任务开始：先扫描邮箱里的测评、面试邀请和待办……")
    try:
        inbox.scan(db, llm)
        pending = inbox.open_items(db)
        if pending:
            log.info("邮箱里有 %d 个待完成任务（会写进今天的简报）", len(pending))
    except Exception as e:  # the inbox is a report, never a reason to skip the day's applications
        log.warning("扫描邮箱失败：%s", e)
    log.info("拉取新岗位……")
    fs = pipeline.fetch(db)
    limit = cfg.get("screen_limit", 120)
    log.info("新增 %d 个岗位，先筛选过去 24 小时内发布的……", fs.new)
    ss = pipeline.screen_pending(db, limit, llm, posted_after=time.time() - DAY)
    backlog = pipeline.BacklogScreen(db.path, max(0, limit - ss.attempted), llm)
    backlog.start()
    log.info("开始投递（当日发布的优先）；更早发布的岗位同时在后台筛选")

    from .apply import runner
    note, results, review = "", [], None
    try:
        results = runner.run_apply(db, mode, max_submissions or cfg.get("max_submissions", 40),
                                   workers=workers or cfg.get("workers"), llm=llm, screening=backlog)
        review = runner.write_review(results, mode)
    except runner.AlreadyRunning as e:
        note = str(e)
    if backlog.is_alive():
        log.info("投递结束，等后台筛选做完……")
    backlog.join()
    ss.merge(backlog.stats)
    cap = config.settings()["applying"].get("batch_size", 20)
    report.write_shortlist(db, cap)
    report.write_manual_list(db)
    report.append_run_log(fs, ss, None, db, results, mode, review)

    emails = _confirmations(started)
    brief: list[str] = []
    try:
        inbox.scan(db, llm)  # mail that arrived during the run: new tasks, and the day's confirmations
        brief = inbox.briefing(db)
    except Exception as e:
        log.warning("生成每日简报失败：%s", e)
        brief = ["## 📋 每日简报", "", f"- ⚠ 读取邮箱失败：{e}"]
    return write_report(db, fs, ss, results, emails, started, review, note, brief)


def _confirmations(since: datetime) -> list[tuple[datetime, str, str]]:
    """(time, sender, subject) of application-confirmation emails since the run started; [] without Gmail access."""
    from .apply.mail import Mailbox
    mailbox = Mailbox.from_env(config.profile()["contact"]["email"])
    if mailbox is None:
        return []
    try:
        return mailbox.confirmations(since)
    except Exception as e:  # mail is a cross-check only; never fail the daily report because of it
        log.warning("读取邮箱确认信失败：%s", e)
        return []


def write_report(db: DB, fs, ss, results: list, emails: list, started: datetime, review: Path | None,
                 note: str = "", brief: list[str] | None = None) -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    by = {k: [(r, o) for r, o in results if o.status == k] for k in ("submitted", "skip", "blocked", "failed", "unconfirmed", "retry")}
    need = by["blocked"] + by["failed"] + by["unconfirmed"]
    essays = [(r, o) for r, o in by["skip"] if o.reason.startswith("需要开放式回答") or "cover letter" in o.reason]
    other_skips = [(r, o) for r, o in by["skip"] if not (o.reason.startswith("需要开放式回答") or "cover letter" in o.reason)]
    mail_text = [f"{frm} {subj}".lower() for _, frm, subj in emails]

    def mailed(r) -> int:
        """How many of today's confirmation emails mention this company."""
        words = _company_words(r["company"])  # every distinctive word, so "American Fidelity" ≠ "American Family"
        return sum(all(w in t for w in words) for t in mail_text) if words else 0

    # a confirmation email can only be credited to a failed job if the company sent more of them than we submitted
    submitted_at = {}
    for r, _ in by["submitted"]:
        submitted_at[r["company"]] = submitted_at.get(r["company"], 0) + 1
    # only jobs the agent actually tried to submit (failed midway / no success page); blocked ones were never sent
    possibly = [(r, o) for r, o in need if o.status in ("failed", "unconfirmed")
                and mailed(r) > submitted_at.get(r["company"], 0)]
    flagged = {r["id"] for r, _ in possibly}
    need = [(r, o) for r, o in need if r["id"] not in flagged]
    questions: dict[str, list[str]] = {}
    for r, o in need:
        if o.reason.startswith("有必填题答不了"):
            for q in o.reason.split("：", 1)[1].split("（可在")[0].split("；"):
                questions.setdefault(q.strip(), []).append(r["company"])
    unsure = db.rows(f"stage = 'unsure' AND active = 1 AND screened_at >= '{started.astimezone():%Y-%m-%dT%H:%M}'")

    outcome = " / ".join(f"{report.STAGE_LABEL.get(k, k)} {v}" for k, v in sorted(ss.outcomes.items())) or "无"
    stale = len(db.rows("stage = 'filtered' AND reason LIKE '发布超过%'"))
    src = "、".join(f"{k} {v}" for k, v in fs.per_source.items())
    out = [
        f"# 每日投递报告 {day}",
        "",
        *(brief or []),
        *([""] if brief else []),
        "## 今天这次运行",
        "",
        f"- 新岗位：两个 GitHub 列表共 {src or '0'} 条，**新增 {fs.new} 个**；LLM 筛选 {ss.attempted} 个：{outcome}",
        f"- 发布超过 {config.settings().get('max_age_days')} 天、不再筛选和投递的岗位共 {stale} 个",
        f"- 投递：**已提交 {len(by['submitted'])} 个**，需要你处理 {len(need) + len(possibly)} 个，"
        f"要写短文 {len(essays)} 个，其他跳过 {len(other_skips)} 个" + (f"，限流没轮到 {len(by['retry'])} 个（下次再投）" if by["retry"] else ""),
        f"- Gmail 里今天收到申请确认信 {len(emails)} 封" if emails else "- Gmail 确认信：没有读到（没配置或读取失败）",
    ]
    if note:
        out.append(f"- ⚠ {note}")
    for title, rows in fs.errors.items():
        out.append(f"- ⚠ {title} 拉取失败：{rows[:160]}")

    out += ["", f"## ❗ 需要你手动申请（{len(need)} 个）", ""]
    out += [f"{i}. [{_cell(r['company'])} — {_cell(r['title'])}]({r['url']}) — {_cell(o.reason)[:140]}（ID `{r['id']}`）"
            for i, (r, o) in enumerate(need, 1)] or ["今天没有。"]
    if possibly:
        out += ["", f"## ❓ 记录为没投成，但邮箱里有这家公司的确认信（{len(possibly)} 个，先查邮箱再决定）", ""]
        out += [f"{i}. [{_cell(r['company'])} — {_cell(r['title'])}]({r['url']}) — {_cell(o.reason)[:100]}（ID `{r['id']}`）"
                for i, (r, o) in enumerate(possibly, 1)]
    if questions:
        out += ["", "## 需要你回答的问题（回答后写进 config/answers.yaml，这些岗位就能自动投）", ""]
        out += [f"- {q}（{'、'.join(dict.fromkeys(c))}）" for q, c in questions.items()]
    if essays:
        out += ["", f"## ✍ 要写短文 / cover letter，按规则跳过（{len(essays)} 个，想投可以手动投）", ""]
        out += [f"{i}. [{_cell(r['company'])} — {_cell(r['title'])}]({r['url']}) — {_cell(o.reason)[:120]}"
                for i, (r, o) in enumerate(essays, 1)]
    if unsure:
        out += ["", f"## 🤔 筛选拿不准，需要你看一眼（{len(unsure)} 个）", "",
                "要投：`uv run jobagent mark <ID> apply`；不投：`uv run jobagent mark <ID> skip`"]
        out += [f"- [{_cell(r['company'])} — {_cell(r['title'])}]({r['url']}) — {_cell(r['reason'])[:120]}（ID `{r['id']}`）"
                for r in unsure]
    out += ["", f"## ✅ 今天已提交（{len(by['submitted'])} 个）", ""]
    out += [f"- {_cell(r['company'])} — {_cell(r['title'])}" for r, _ in by["submitted"]] or ["今天没有。"]
    out += ["", "以前积累的、需要手动投的全部岗位见 `manual_apply.md`。手动投完一个就运行 `uv run jobagent mark <ID> applied`。"]
    if review:
        out.append(f"每道题填了什么：`{review.relative_to(config.ROOT)}`")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{day}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path
