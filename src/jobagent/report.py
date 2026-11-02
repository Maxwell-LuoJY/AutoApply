"""Daily shortlist (shortlists/YYYY-MM-DD.md) and run-log section (run_logs/YYYY-MM-DD.md)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import config, history
from .db import DB
from .pipeline import FetchStats, ScreenStats, pinned, queue_order
from .rules import posted_today

STAGE_LABEL = {
    "eligible": "可投", "skip": "跳过", "unsure": "需确认", "jd_missing": "JD 未读到", "error": "出错待重试",
    "pending": "待筛选", "filtered": "规则过滤", "duplicate": "重复", "done": "已处理过",
    "applied": "已投递", "manual": "待手动投递", "blocked": "待手动投递", "failed": "待手动投递",
}


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def _age(ts: int) -> str:
    return f"{max(0, int((time.time() - ts) // 86400))}d" if ts else "?"


def _loc(locations_json: str) -> str:
    locs = json.loads(locations_json)
    if not locs:
        return ""
    return locs[0] + (f" +{len(locs) - 1}" if len(locs) > 1 else "")


def shortlist(db: DB, cap: int) -> tuple[list, int]:
    """Top `cap` eligible, still-open jobs not yet in applications.csv, in apply order; plus total eligible."""
    hist = history.load(config.APPLICATIONS_CSV)
    max_failures = config.settings()["applying"]["max_failures"]
    rows = [r for r in db.rows("stage = 'eligible' AND active = 1")
            if not hist.blocking_status(r["url"], r["company"], r["title"], max_failures)]
    rows = queue_order(db, rows)
    return rows[:cap], len(rows)


def write_shortlist(db: DB, cap: int, day: str | None = None) -> Path:
    day = day or datetime.now().strftime("%Y-%m-%d")
    top, total = shortlist(db, cap)
    unsure = db.rows("stage = 'unsure' AND active = 1", limit=30)
    missing = db.rows("stage = 'jd_missing' AND active = 1", limit=30)

    out = [
        f"# 待投清单 {day}",
        "",
        f"生成于 {datetime.now():%H:%M} · 可投 {total} 个（按投递顺序列前 {len(top)} 个：📌 指定先投，再 24 小时内发布的 🆕，再按匹配度；每日上限 {cap}）"
        f" · 需确认 {len(unsure)} · JD 未读到 {len(missing)}",
        "",
        "## 可投",
        "",
        "| # | 公司 | 岗位 | 地点 | 系统 | 简历 | 匹配 | 发布 | ID |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    now, pins = time.time(), pinned(db)
    for i, r in enumerate(top, 1):
        fit = "?" if r["fit"] is None else str(r["fit"])
        new = " 📌" if r["id"] in pins else " 🆕" if posted_today(r["date_posted"], now) else ""
        out.append(f"| {i} | {_cell(r['company'])}{new} | [{_cell(r['title'])}]({r['url']}) | {_cell(_loc(r['locations']))} "
                   f"| {r['ats']} | {r['resume']} | {fit} | {_age(r['date_posted'])} | `{r['id']}` |")
    if unsure:
        out += ["", "## 需要你确认", "",
                "确认后运行 `jobagent mark <ID> apply` 或 `jobagent mark <ID> skip --reason ...`。", "",
                "| 公司 | 岗位 | 原因 | 原文 | ID |", "|---|---|---|---|---|"]
        for r in unsure:
            evidence = (json.loads(r["screen_json"] or "{}").get("llm") or {}).get("evidence", "")
            out.append(f"| {_cell(r['company'])} | [{_cell(r['title'])}]({r['url']}) | {_cell(r['reason'])} "
                       f"| {_cell(evidence[:160])} | `{r['id']}` |")
    if missing:
        out += ["", "## JD 未读到（申请时在页面上核对资格）", "",
                "| 公司 | 岗位 | 系统 | 发布 | ID |", "|---|---|---|---|---|"]
        for r in missing:
            out.append(f"| {_cell(r['company'])} | [{_cell(r['title'])}]({r['url']}) | {r['ats']} "
                       f"| {_age(r['date_posted'])} | `{r['id']}` |")

    config.SHORTLIST_DIR.mkdir(parents=True, exist_ok=True)
    path = config.SHORTLIST_DIR / f"{day}.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def append_run_log(fs: FetchStats | None, ss: ScreenStats | None, shortlist_path: Path | None, db: DB,
                   results: list | None = None, mode: str = "dry_run", review: Path | None = None) -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    path = config.RUN_LOG_DIR / f"{day}.md"
    title = "拉取与筛选" if results is None else ("投递" if fs is None else "拉取、筛选与投递")
    lines = [f"# 独立 agent · {title}（{datetime.now():%H:%M}）", ""]
    if fs:
        src = "、".join(f"{k} {v} 条" for k, v in fs.per_source.items())
        lines.append(f"- 拉取：{src or '无'}，新增 {fs.new} 条")
        for k, v in fs.errors.items():
            lines.append(f"- **{k} 拉取失败**：{v[:200]}")
        lines.append("- 规则过滤后：" + "，".join(f"{STAGE_LABEL.get(k, k)} {v}" for k, v in sorted(fs.stages.items())))
    if ss:
        outcome = " / ".join(f"{STAGE_LABEL.get(k, k)} {v}" for k, v in sorted(ss.outcomes.items())) or "无"
        lines.append(f"- 本次 LLM 筛选 {ss.attempted} 个：{outcome}")
    totals = db.stage_counts()
    lines.append("- 数据库累计：" + "，".join(f"{STAGE_LABEL.get(k, k)} {v}" for k, v in sorted(totals.items())))
    if shortlist_path:
        lines.append(f"- 今日待投清单：`{shortlist_path.relative_to(config.ROOT)}`")
    if results is None:
        lines.append("- 本次只筛选、没有打开申请页")
    else:
        counts: dict[str, int] = {}
        for _, o in results:
            counts[o.status] = counts.get(o.status, 0) + 1
        names = {"submitted": "已提交", "dry_run": "试运行填完", "skip": "跳过", "blocked": "需人工处理",
                 "unconfirmed": "未确认", "failed": "失败"}
        mode_label = "真实投递" if mode == "submit" else "试运行（不提交）"
        lines.append(f"- {mode_label} {len(results)} 个：" + " / ".join(f"{names[k]} {v}" for k, v in counts.items()))
        need = [(r, o) for r, o in results if o.status in ("blocked", "unconfirmed", "failed")]
        if need:
            lines.append("- **需要你处理**：")
            lines += [f"  - {r['company']} — {r['title']}：{o.reason}（{r['url']}）" for r, o in need[:15]]
        if review:
            lines.append(f"- 每题答案和截图：`{review.relative_to(config.ROOT)}`")
        lines.append("- 没投成的岗位链接汇总：`manual_apply.md`")

    config.RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "\n---\n\n" if path.exists() and path.stat().st_size else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(prefix + "\n".join(lines) + "\n")
    return path


def write_manual_list(db: DB) -> Path:
    """manual_apply.md: every job the agent couldn't finish, with the link and why — rebuilt after each run."""
    rows = db.rows("stage IN ('manual', 'blocked', 'failed') AND active = 1")
    unconfirmed = [r for r in rows if (r["reason"] or "").startswith("点了提交但")]
    todo = [r for r in rows if r not in unconfirmed]
    essays = db.rows("stage = 'skip' AND active = 1 AND (reason LIKE '需要开放式回答%' OR reason LIKE '%cover letter%')")

    out = [
        "# 需要手动投递的岗位",
        "",
        f"更新于 {datetime.now():%Y-%m-%d %H:%M} · 待手动投 {len(todo)} 个 · 可能已提交 {len(unconfirmed)} 个 · 需写短文 {len(essays)} 个",
        "",
        "投完一个就运行 `uv run jobagent mark <ID> applied`：它会从这里消失，并在 applications.csv 记一条\"已提交\"，"
        "agent 以后不会再投它。不想投的：`uv run jobagent mark <ID> skip`。",
    ]
    if unconfirmed:
        out += ["", "## 可能已经提交（先查邮箱有没有确认信）", "", "| 公司 | 岗位 | 说明 | ID |", "|---|---|---|---|"]
        out += [f"| {_cell(r['company'])} | [{_cell(r['title'])}]({r['url']}) | {_cell(r['reason'])} | `{r['id']}` |" for r in unconfirmed]
    out += ["", "## 待手动投递", "", "| # | 公司 | 岗位 | 系统 | 简历 | 没投成的原因 | ID |", "|---|---|---|---|---|---|---|"]
    out += [f"| {i} | {_cell(r['company'])} | [{_cell(r['title'])}]({r['url']}) | {r['ats']} | {r['resume']} "
            f"| {_cell(r['reason'])} | `{r['id']}` |" for i, r in enumerate(todo, 1)]
    if essays:
        out += ["", "## 按规则跳过：要写短文或 cover letter（想投的话可以手动投）", "",
                "| 公司 | 岗位 | 需要什么 | ID |", "|---|---|---|---|"]
        out += [f"| {_cell(r['company'])} | [{_cell(r['title'])}]({r['url']}) | {_cell(r['reason'])} | `{r['id']}` |" for r in essays]
    path = config.ROOT / "manual_apply.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path
