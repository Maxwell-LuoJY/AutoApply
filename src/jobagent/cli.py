"""jobagent command line.

  jobagent init                copy the example config into place (config/*.yaml, .env, credentials.txt)
  jobagent check [--llm] [--mail]   what is set up and what is still missing (optionally call the LLM / log in to Gmail)
  jobagent daily               the unattended daily job: fetch + screen + submit + daily_reports/YYYY-MM-DD.md
  jobagent run [--apply]       fetch + screen + shortlist (+ apply) + run log
  jobagent fetch               pull both lists and apply hard rules
  jobagent resume parse|show   parse each resume once into config/resumes/<KIND>.yaml (used to fill experience fields)
  jobagent inbox [done ID]     scan Gmail for assessments / interviews / tasks; list what's open, or mark one done
  jobagent brief               today's briefing: applications counted from confirmation emails + open tasks
  jobagent screen [--limit N]  read JDs and let the LLM screen pending jobs
  jobagent apply [--submit]    fill applications for eligible jobs (dry run unless --submit or mode: submit)
  jobagent shortlist           rewrite today's shortlist file
  jobagent stats               counts per stage
  jobagent show ID             everything stored about one job
  jobagent mark ID apply|next|skip|applied   decide a job by hand (next = first in the next run)
"""
from __future__ import annotations

import argparse
import json
import logging

from . import config, pipeline, report
from .db import DB


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="jobagent", description="Internship application agent")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create config/*.yaml, .env and credentials.txt from the examples")
    chk = sub.add_parser("check", help="show what is configured and what is missing")
    chk.add_argument("--llm", action="store_true", help="also send one request to the LLM")
    chk.add_argument("--mail", action="store_true", help="also log in to Gmail over IMAP")
    run = sub.add_parser("run", help="fetch + screen + shortlist (+ apply) + run log")
    run.add_argument("--limit", type=int, help="max JDs to screen this run")
    run.add_argument("--apply", action="store_true", help="also fill applications (mode from settings.yaml)")
    daily = sub.add_parser("daily", help="fetch + screen + submit applications + write today's report")
    daily.add_argument("--max", type=int, help="most submissions today (default daily.max_submissions)")
    daily.add_argument("--workers", type=int, help="parallel browsers (default daily.workers)")
    daily.add_argument("--dry-run", action="store_true", help="fill but never submit (for testing the daily job)")
    sub.add_parser("fetch", help="pull job lists and apply hard rules")
    ib = sub.add_parser("inbox", help="recruiting emails that need you (assessments, interviews, tasks)")
    ib.add_argument("action", nargs="?", default="scan", choices=["scan", "done", "list"])
    ib.add_argument("item", nargs="?", type=int, help="item ID for 'done'")
    ib.add_argument("--days", type=int, default=30, help="first scan: how many days back")
    sub.add_parser("brief", help="scan the inbox and print today's briefing (also daily_reports/<day>-brief.md)")
    rs = sub.add_parser("resume", help="structured resumes used to fill experience / education / skills")
    rs.add_argument("action", choices=["parse", "show"])
    rs.add_argument("--kind", choices=["AIE", "SDE"], help="only this resume (default: both)")
    scr = sub.add_parser("screen", help="LLM-screen pending jobs")
    scr.add_argument("--limit", type=int)
    scr.add_argument("--id", action="append", dest="ids", help="only this job ID (repeatable)")
    app = sub.add_parser("apply", help="fill applications for eligible jobs")
    mode = app.add_mutually_exclusive_group()
    mode.add_argument("--submit", action="store_true", help="really submit (overrides settings.yaml)")
    mode.add_argument("--dry-run", action="store_true", help="fill but never submit (overrides settings.yaml)")
    app.add_argument("--target", type=int, help="stop after this many submissions (default applying.batch_size)")
    app.add_argument("--workers", type=int, help="parallel browsers (default applying.workers)")
    app.add_argument("--id", action="append", dest="ids", help="only this job ID (repeatable)")
    app.add_argument("--headed", action="store_true", help="show the browser window")
    app.add_argument("--allow-accounts", action="store_true", help="dry run may sign in / sign up on Workday")
    sub.add_parser("shortlist", help="rewrite today's shortlist")
    sub.add_parser("stats", help="job counts per stage")
    show = sub.add_parser("show", help="show one job")
    show.add_argument("id")
    mk = sub.add_parser("mark", help="manually decide a job")
    mk.add_argument("id")
    mk.add_argument("decision", choices=["apply", "next", "skip", "applied"],
                    help="apply = queue it again; next = queue it first for the next run; skip = never apply; "
                         "applied = you applied by hand")
    mk.add_argument("--reason")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd in ("init", "check"):
        from . import onboard
        if args.cmd == "init":
            return onboard.init()
        raise SystemExit(0 if onboard.check(args.llm, args.mail) else 1)

    db = DB(config.DB_PATH)
    cap = config.settings()["applying"].get("batch_size", 20)
    try:
        if args.cmd == "daily":
            from .daily import run_daily
            path = run_daily(db, args.max, args.workers, "dry_run" if args.dry_run else "submit")
            print(f"\n===== 每日报告：{path} =====\n")
            print(path.read_text(encoding="utf-8"))
        elif args.cmd == "run":
            fs = pipeline.fetch(db)
            ss = pipeline.screen_pending(db, args.limit)
            sl = report.write_shortlist(db, cap)
            results, review, mode = None, None, config.settings()["applying"]["mode"]
            if args.apply:
                from .apply import runner
                results = runner.run_apply(db, mode)
                review = runner.write_review(results, mode)
                sl = report.write_shortlist(db, cap)
                report.write_manual_list(db)
            log_path = report.append_run_log(fs, ss, sl, db, results, mode, review)
            print(f"\n待投清单：{sl}\n运行日志：{log_path}" + (f"\n投递记录：{review}" if review else ""))
        elif args.cmd == "apply":
            from .apply import runner
            mode = "submit" if args.submit else "dry_run" if args.dry_run else config.settings()["applying"]["mode"]
            results = runner.run_apply(db, mode, args.target, args.ids, headless=False if args.headed else None,
                                       allow_accounts=args.allow_accounts, workers=args.workers)
            review = runner.write_review(results, mode)
            report.write_shortlist(db, cap)
            manual = report.write_manual_list(db)
            report.append_run_log(None, None, None, db, results, mode, review)
            print(f"\n投递记录：{review}\n待手动投递：{manual}")
        elif args.cmd in ("inbox", "brief"):
            from . import inbox
            from .llm import LLM
            if args.cmd == "inbox" and args.action == "done":
                if args.item is None or not inbox.mark(db, args.item):
                    raise SystemExit("找不到这个 ID（运行 `jobagent inbox list` 查看）")
                print(f"{args.item} → 已完成")
            else:
                if args.cmd == "brief" or args.action == "scan":
                    inbox.scan(db, LLM(rps=config.settings()["screening"].get("llm_rps", 1.0)), days=getattr(args, "days", 30))
                if args.cmd == "brief":
                    from datetime import date
                    text = "\n".join([f"# 每日简报 {date.today()}", ""] + inbox.briefing(db)) + "\n"
                    path = config.ROOT / "daily_reports" / f"{date.today()}-brief.md"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                    print(text + f"\n（已保存：{path}）")
                else:
                    for i in inbox.open_items(db):
                        print(f"[{i['id']}] {inbox.LABEL[i['kind']]} | {i['company']} | {i['summary'] or i['subject']} | "
                              f"截止 {i['deadline'] or '-'} | {i['link'] or '-'}")
        elif args.cmd == "resume":
            from . import resume
            from .llm import LLM
            kinds = [args.kind] if args.kind else [k for k in resume.KINDS if k in config.profile()["resumes"]]
            if args.action == "parse":
                llm = LLM(rps=config.settings()["screening"].get("llm_rps", 1.0))
                for k in kinds:
                    print(f"{k}：已保存 {resume.parse(k, llm)}")
            for k in kinds:
                path = resume.DIR / f"{k}.yaml"
                state = "可用" if resume.structured(k) else ("已过期（简历文件改过）" if path.exists() else "还没解析")
                print(f"\n===== {k}（{state}）=====")
                if path.exists():
                    print(path.read_text(encoding="utf-8"))
        elif args.cmd == "fetch":
            fs = pipeline.fetch(db)
            print(json.dumps({"per_source": fs.per_source, "new": fs.new, "stages": fs.stages, "errors": fs.errors},
                             ensure_ascii=False, indent=2))
        elif args.cmd == "screen":
            ss = pipeline.screen_pending(db, args.limit, ids=args.ids)
            print(json.dumps({"attempted": ss.attempted, "outcomes": ss.outcomes}, ensure_ascii=False, indent=2))
        elif args.cmd == "shortlist":
            print(report.write_shortlist(db, cap))
        elif args.cmd == "stats":
            for stage, n in sorted(db.stage_counts().items(), key=lambda kv: -kv[1]):
                print(f"{report.STAGE_LABEL.get(stage, stage):10s} {stage:11s} {n}")
        elif args.cmd == "show":
            r = db.get(args.id)
            if r is None:
                raise SystemExit(f"找不到岗位 {args.id}")
            d = dict(r)
            for k in ("locations", "degrees", "terms", "screen_json"):
                if d.get(k):
                    d[k] = json.loads(d[k])
            print(json.dumps(d, ensure_ascii=False, indent=2))
        elif args.cmd == "mark":
            pipeline.mark(db, args.id, args.decision, args.reason)
            print(f"{args.id} → {args.decision}")
            report.write_shortlist(db, cap)
            report.write_manual_list(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
