from jobagent.models import Posting
from datetime import date

from jobagent import config
from jobagent.rules import (blocked_company, canonical_key, detect_ats, hard_filter, is_us_location, job_id,
                            pick_resume)
from jobagent.screen import cover_letter_required, required_free_text


def P(title="Software Engineer Intern", **kw):
    return Posting(source="Simplify", company="Acme", title=title, url="https://x.com/j/1", **kw)


def test_canonical_key_collapses_variants():
    a = "https://boards.greenhouse.io/twitch/jobs/8886748002?utm_source=Simplify&ref=Simplify"
    b = "https://job-boards.greenhouse.io/twitch/jobs/8886748002"
    c = "https://www.twitch.tv/jobs?gh_jid=8886748002"
    assert canonical_key(a) == canonical_key(b) == canonical_key(c) == "greenhouse:8886748002"
    wd1 = "https://netflix.wd108.myworkdayjobs.com/en-US/netflix/job/Los-Gatos/SWE-Intern_JR42989"
    wd2 = "https://netflix.wd108.myworkdayjobs.com/netflix/job/Los-Gatos/SWE-Intern_JR42989/apply"
    assert job_id(wd1) == job_id(wd2)
    assert canonical_key("https://jobs.lever.co/palantir/d5486403-c050-4920-b2e0-91b69b61ebb2/apply") == \
        "jobs.lever.co/palantir/d5486403-c050-4920-b2e0-91b69b61ebb2"


def test_detect_ats():
    assert detect_ats("https://salesforce.wd12.myworkdayjobs.com/x/job/y") == "workday"
    assert detect_ats("https://careers.roblox.com/jobs/8072713?gh_jid=8072713") == "greenhouse"
    assert detect_ats("https://jobs.ashbyhq.com/ctgt/f657c2f5-125e-42b6-a68a-646bbea3d155/application") == "ashby"
    assert detect_ats("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1") == "oracle"
    assert detect_ats("https://example.com/careers/1") == "other"


def test_us_location():
    for loc in ["Austin, TX", "Remote in USA", "New York, NY", "California, USA +2", "Remote", "Washington, DC"]:
        assert is_us_location(loc), loc
    for loc in ["Toronto, ON, Canada", "Vancouver, BC", "London, UK", "Remote in Canada", "Bengaluru, India"]:
        assert not is_us_location(loc), loc


def test_hard_filter():
    assert hard_filter(P()) is None
    assert hard_filter(P(active=False)) == "岗位已关闭"
    assert "公民" in hard_filter(P(sponsorship="U.S. Citizenship is Required"))
    assert "非美国" in hard_filter(P(locations=["Toronto, ON, Canada"]))
    assert hard_filter(P(locations=["Toronto, ON, Canada", "Seattle, WA"])) is None
    assert "本科" in hard_filter(P("Undergrad Software Engineering Intern - Grad Year: Spring 2028"))
    assert "PhD" in hard_filter(P("Research Intern - Embedded AI - PhD"))
    assert hard_filter(P("Research Intern (Master's/PhD)")) is None
    assert "PhD" in hard_filter(P(degrees=["PhD"]))
    assert hard_filter(P(degrees=["Bachelor's", "Master's"])) is None
    assert "Fall 2026" in hard_filter(P("Software Engineer Co-op (Fall 2026)"))
    assert hard_filter(P("Software Engineer Intern - Summer 2027")) is None
    assert hard_filter(P("SWE Intern (Fall 2027 / Summer 2027)")) is None
    assert hard_filter(P("SWE Intern - Graduating Spring 2028")) is None
    assert "非实习" in hard_filter(P("New Grad Software Engineer"))


def test_pick_resume():
    assert pick_resume("Software Engineer Intern", "Software") == "SDE"
    assert pick_resume("Machine Learning Engineer Intern", "Software") == "AIE"
    assert pick_resume("Software Engineer, AI Platform", "Software") == "AIE"
    assert pick_resume("Data Engineer Intern", "AI/ML/Data") == "AIE"            # data roles → AIE (2026-10-29)
    assert pick_resume("Business Intelligence Developer Intern", "Software") == "AIE"
    assert pick_resume("Technology Consultant Intern", "Software") == "AIE"
    assert pick_resume("Business Analyst Intern", "Software") == "AIE"
    assert pick_resume("Data Infra Software Engineering Intern", "Software") == "SDE"  # software role touching data
    assert pick_resume("Application Developer Intern", "Software") == "SDE"
    assert pick_resume("Data Analyst Intern", "AI/ML/Data") == "AIE"
    assert pick_resume("Research Scientist Intern", "Software") == "AIE"
    assert pick_resume("Email Marketing Intern", "Software") == "SDE"  # "ai" inside a word doesn't count


def test_greenhouse_form_rules():
    qs = [
        {"label": "Resume/CV", "required": True, "fields": [{"type": "input_file"}, {"type": "textarea"}]},
        {"label": "Cover Letter", "required": False, "fields": [{"type": "input_file"}, {"type": "textarea"}]},
        {"label": "Why Twitch?", "required": True, "fields": [{"type": "textarea"}]},
        {"label": "Anything else?", "required": False, "fields": [{"type": "textarea"}]},
    ]
    assert required_free_text(qs) == ["Why Twitch?"]
    assert required_free_text(None) is None
    assert not cover_letter_required(qs)
    qs[1]["required"] = True
    assert cover_letter_required(qs)


def test_school_and_event_restricted_titles():
    from jobagent.rules import Posting, hard_filter
    def post(title):
        return Posting(source="Simplify", company="X", title=title, url="https://x/1", locations=["Boston, MA"],
                       active=True, sponsorship=None, degrees=[], terms=["Summer 2027"], date_posted=0, category="Software")
    assert "Northeastern University" in hard_filter(post("Northeastern University Software Engineer - Application Development Co-op"))
    assert "Society of Women Engineers" in hard_filter(post("Technology Intern - Society of Women Engineers Conference"))
    assert hard_filter(post("Software Engineer Intern - University of Michigan Program"),
                       schools=("University of Michigan", "UMich")) is None
    assert "University of Michigan" in hard_filter(post("Software Engineer Intern - University of Michigan Program"))
    assert hard_filter(post("Software Engineer Intern")) is None


def test_blocked_company():
    entries = ["Waymo", {"name": "Wing", "until": date(2026, 11, 28), "reason": "30 天内最多投 4 个"}]
    assert "Waymo" in blocked_company("Waymo", entries)
    assert blocked_company("waymo llc", entries)
    assert blocked_company("Wingstop", entries) is None          # whole words only
    assert blocked_company("Red Wing Shoes", entries) is None    # from the start of the name
    assert "2026-11-28 之后恢复" in blocked_company("Wing", entries, today=date(2026, 11, 28))
    assert blocked_company("Wing", entries, today=date(2026, 11, 29)) is None   # expired
    assert blocked_company("Wing", [{"name": "Wing", "until": "2026-11-28"}], today=date(2026, 12, 1)) is None
    assert blocked_company("Waymo", None) is None


def test_refilter_pulls_blocked_companies_out_of_the_queue(tmp_path, monkeypatch):
    from jobagent import config, pipeline
    from jobagent.db import DB
    settings = {**config.settings(), "blocked_companies": [{"name": "Waymo", "reason": "上限"}]}
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "APPLICATIONS_CSV", tmp_path / "applications.csv")
    db = DB(tmp_path / "jobs.db")
    jobs = {name: Posting("Simplify", company, "Software Engineer Intern", f"https://x.com/{name}",
                          ["Chicago, IL"], "Software", 0)
            for name, company in (("new", "Waymo"), ("queued", "Waymo"), ("done", "Waymo"), ("other", "Wayfair"))}
    for p in jobs.values():
        db.upsert(p)
    db.update(job_id(jobs["queued"].url), stage="eligible")
    db.update(job_id(jobs["done"].url), stage="applied")
    pipeline.refilter(db)
    stage = {k: db.get(job_id(p.url))["stage"] for k, p in jobs.items()}
    assert stage == {"new": "filtered", "queued": "filtered", "done": "applied", "other": "pending"}
    assert db.get(job_id(jobs["queued"].url))["reason"] == "禁用列表：Waymo，上限"


def _job(id, ats="workday", first_seen="2026-10-31T03:45:50", fit=None, date_posted=0):
    return {"id": id, "ats": ats, "first_seen": first_seen, "fit": fit, "date_posted": date_posted}


NOW = 1_793_570_000  # 2026-11-01 evening


def test_apply_order_puts_todays_jobs_first_then_best_fit():
    from jobagent.rules import apply_order
    hour = 3600
    rows = [
        _job("old-high", fit=95, date_posted=NOW - 5 * 86400),
        _job("old-unrated", date_posted=NOW - 2 * 86400),
        _job("today-low", fit=40, date_posted=NOW - 3 * hour),
        _job("old-mid", ats="greenhouse", fit=70, date_posted=NOW - 9 * 86400),
        _job("today-high", ats="ashby", fit=88, date_posted=NOW - 23 * hour),
        _job("today-other", ats="other", fit=99, date_posted=NOW - hour),
        _job("old-lever", ats="lever", fit=100, date_posted=NOW - 3 * 86400),
        _job("old-high-newer", fit=95, date_posted=NOW - 4 * 86400),
        _job("undated", fit=96),
    ]
    order = [r["id"] for r in apply_order(rows, NOW)]
    assert order == ["today-high", "today-low", "undated", "old-high-newer", "old-high", "old-mid", "old-unrated",
                     "today-other", "old-lever"]


def test_too_old_and_posted_today():
    from jobagent.rules import posted_today, too_old
    assert too_old(NOW - 31 * 86400, 30, NOW) == f"发布超过 30 天（{__import__('datetime').datetime.fromtimestamp(NOW - 31 * 86400):%Y-%m-%d}）"
    assert too_old(NOW - 29 * 86400, 30, NOW) is None
    assert too_old(0, 30, NOW) is None              # no date: kept
    assert too_old(NOW - 99 * 86400, None, NOW) is None  # rule off
    assert posted_today(NOW - 23 * 3600, NOW) and not posted_today(NOW - 25 * 3600, NOW) and not posted_today(0, NOW)


def test_refilter_drops_postings_older_than_max_age(tmp_path, monkeypatch):
    import json
    import time
    from jobagent import config, pipeline
    from jobagent.db import DB
    settings = {**config.settings(), "blocked_companies": [], "max_age_days": 30}
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "APPLICATIONS_CSV", tmp_path / "applications.csv")
    db = DB(tmp_path / "jobs.db")
    now, day = time.time(), 86400
    ages = {"old-pending": 40, "old-eligible": 31, "old-manual": 45, "old-pinned": 50, "fresh": 3, "undated": None}
    jobs = {name: Posting("Simplify", f"Co {name}", "Software Engineer Intern", f"https://x.com/{name}",
                          ["Chicago, IL"], "Software", int(now - age * day) if age else 0)
            for name, age in ages.items()}
    for p in jobs.values():
        db.upsert(p)
    jid = {k: job_id(p.url) for k, p in jobs.items()}
    db.update(jid["old-eligible"], stage="eligible")
    db.update(jid["old-manual"], stage="manual")
    db.update(jid["old-pinned"], stage="eligible")
    db.meta("apply_first", json.dumps([jid["old-pinned"]]))
    pipeline.refilter(db)
    stage = {k: db.get(i)["stage"] for k, i in jid.items()}
    assert stage == {"old-pending": "filtered", "old-eligible": "filtered", "old-manual": "filtered",
                     "old-pinned": "eligible", "fresh": "pending", "undated": "pending"}
    assert db.get(jid["old-eligible"])["reason"].startswith("发布超过 30 天（")
    db.upsert(Posting("Simplify", "Co abroad", "Software Engineer Intern", "https://x.com/abroad", ["Toronto, Canada"],
                      "Software", int(now - 60 * day)))
    pipeline.refilter(db)
    assert db.get(job_id("https://x.com/abroad"))["reason"].startswith("非美国地点")  # the other rule's reason is kept


def test_screen_pending_today_first_and_never_past_max_age(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from jobagent import config, pipeline
    from jobagent.db import DB
    settings = {**config.settings(), "blocked_companies": [], "max_age_days": 30}
    monkeypatch.setattr(config, "settings", lambda: settings)
    monkeypatch.setattr(config, "APPLICATIONS_CSV", tmp_path / "applications.csv")
    screened = []

    def fake_screen(r, llm, profile, term, jd_max_chars):
        screened.append(r["company"])
        return SimpleNamespace(stage="eligible", reason="ok", data={"llm": {"fit": 70}})
    monkeypatch.setattr(pipeline, "screen", fake_screen)
    db = DB(tmp_path / "jobs.db")
    now, day = time.time(), 86400
    for name, age in (("today", 0.2), ("week", 7), ("month-old", 35)):
        db.upsert(Posting("Simplify", name, "SWE Intern", f"https://x.com/{name}", ["Chicago, IL"], "Software",
                          int(now - age * day)))
    db.conn.execute("UPDATE jobs SET stage = 'pending'")  # even if refilter hasn't run yet
    assert pipeline.screen_pending(db, 10, llm=object(), posted_after=now - day).attempted == 1
    assert screened == ["today"]
    stats = pipeline.screen_pending(db, 10, llm=object())
    assert screened == ["today", "week"] and stats.outcomes == {"eligible": 1}
    assert db.rows("company = 'week'")[0]["fit"] == 70


def test_fit_score_parsing():
    from jobagent.screen import fit_score
    assert fit_score({"fit": 87}) == 87
    assert fit_score({"fit": "72"}) == 72
    assert fit_score({"fit": 101.4}) == 100
    assert fit_score({"fit": -3}) == 0
    assert fit_score({"fit": "high"}) is None
    assert fit_score({}) is None


def test_db_adds_fit_column_and_meta(tmp_path):
    import sqlite3
    from jobagent.db import DB
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)  # a database from before the fit column
    old.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL, "
                "url TEXT NOT NULL, ats TEXT NOT NULL, category TEXT, locations TEXT NOT NULL DEFAULT '[]', "
                "date_posted INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, sponsorship TEXT, "
                "degrees TEXT NOT NULL DEFAULT '[]', terms TEXT NOT NULL DEFAULT '[]', first_seen TEXT NOT NULL, "
                "last_seen TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'new', reason TEXT, resume TEXT, "
                "screen_attempts INTEGER NOT NULL DEFAULT 0, screen_json TEXT, screened_at TEXT)")
    old.commit()
    old.close()
    db = DB(path)
    assert "fit" in {c[1] for c in db.conn.execute("PRAGMA table_info(jobs)")}
    assert db.meta("last_fetch") is None
    db.meta("last_fetch", "2026-11-01T09:11:00")
    db.meta("last_fetch", "2026-11-02T09:11:00")
    assert db.meta("last_fetch") == "2026-11-02T09:11:00"
    db.close()
    assert DB(path).meta("last_fetch") == "2026-11-02T09:11:00"  # reopening doesn't add the column twice


def test_pinned_jobs_go_before_everything_in_pin_order():
    from jobagent.rules import apply_order
    rows = [_job("today-high", ats="ashby", fit=99, date_posted=NOW - 3600), _job("pin-b", fit=58),
            _job("pin-a", ats="lever", fit=10), _job("old", fit=90)]
    order = [r["id"] for r in apply_order(rows, NOW, ["pin-a", "pin-b"])]
    assert order == ["pin-a", "pin-b", "today-high", "old"]


def test_mark_next_pins_once_and_other_decisions_unpin(tmp_path, monkeypatch):
    from jobagent import config, pipeline
    from jobagent.db import DB
    from jobagent.models import Posting
    from jobagent.rules import job_id
    monkeypatch.setattr(config, "APPLICATIONS_CSV", tmp_path / "applications.csv")  # never touch the real history
    db = DB(tmp_path / "j.db")
    urls = ["https://x.wd1.myworkdayjobs.com/a/job/1", "https://x.wd1.myworkdayjobs.com/a/job/2"]
    for u in urls:
        db.upsert(Posting(source="Simplify", company="X", title="SWE Intern", url=u, locations=["Chicago, IL"]))
    a, b = (job_id(u) for u in urls)
    pipeline.mark(db, a, "next", None)
    pipeline.mark(db, b, "next", None)
    pipeline.mark(db, a, "next", None)  # pinning twice keeps one entry
    assert pipeline.pinned(db) == [a, b] and db.get(a)["stage"] == "eligible"
    pipeline.mark(db, a, "apply", None)  # back to the normal queue order
    assert pipeline.pinned(db) == [b]
    pipeline.unpin(db, b)  # what a real attempt does
    assert pipeline.pinned(db) == []


def test_hard_filter_follows_the_applicants_degree_and_citizenship():
    post = lambda title, **kw: Posting(source="Simplify", company="X", title=title, url="https://x.com/1",  # noqa: E731
                                       locations=["Austin, TX"], active=True, degrees=kw.get("degrees", []),
                                       sponsorship=kw.get("sponsorship"), terms=["Summer 2027"])
    assert hard_filter(post("Software Engineer Intern - Undergraduate")) == "仅限本科生（岗位名称）"
    assert hard_filter(post("Software Engineer Intern - Undergraduate"), level="bachelor") is None
    assert hard_filter(post("Research Intern - PhD"), level="bachelor") == "仅限研究生（岗位名称）"
    assert hard_filter(post("AI/ML Engineer Intern - Masters"), level="bachelor") == "仅限研究生（岗位名称）"
    assert hard_filter(post("Research Intern - PhD"), level="phd") is None
    assert hard_filter(post("SWE Intern"), level="phd") is None
    us_only = post("SWE Intern", sponsorship="U.S. Citizenship is Required")
    assert hard_filter(us_only) is not None and hard_filter(us_only, citizen=True) is None


def test_screen_rules_follow_the_profile():
    import copy
    from jobagent.screen import _bad_flags, eligibility_rules, sponsorship_note
    p = copy.deepcopy(config.profile())
    rules = eligibility_rules(p)
    assert "U.S. citizenship" in rules and "Open only to PhD students" in rules and "2028-05" in rules
    assert "IS a reason" not in sponsorship_note(p) and "no_sponsorship" not in _bad_flags(p)
    p["work_authorization"].update(us_citizen_or_permanent_resident=True, requires_sponsorship_now_or_future=True)
    p["education"][0]["level"] = "bachelor"
    rules = eligibility_rules(p)
    assert "U.S. citizenship" not in rules and "Open only to graduate students" in rules and "will not sponsor" in rules
    assert rules.startswith("3. ") and "IS a reason" in sponsorship_note(p)
    assert "no_sponsorship" in _bad_flags(p) and "citizenship_or_clearance_required" not in _bad_flags(p)


def test_sensitive_answers_follow_the_profile():
    import copy
    from jobagent.apply.answers import _sensitive_conflict, facts_text
    p = copy.deepcopy(config.profile())
    sponsor = {"label": "Will you now or in the future require visa sponsorship?"}
    assert _sensitive_conflict(sponsor, "Yes", p) is not None and _sensitive_conflict(sponsor, "No", p) is None
    p["work_authorization"]["requires_sponsorship_now_or_future"] = True
    assert _sensitive_conflict(sponsor, "Yes", p) is None and _sensitive_conflict(sponsor, "No", p) is not None
    text = facts_text(p)
    assert "no middle name" in text and "expected graduation May 2028" in text
    assert "a GPA box inside the University of Michigan entry stays blank" in text and "3.70/4.00 (B.Eng." in text
    p["name"]["middle"] = "Jordan"
    assert "Alex Jordan Rivera" in facts_text(p) and "middle: Jordan" in facts_text(p)


def test_degree_level_is_read_from_the_degree_name():
    assert config.degree_level({"degree": "M.S. in Computer Science"}) == "master"
    assert config.degree_level({"degree": "B.Eng. in Artificial Intelligence"}) == "bachelor"
    assert config.degree_level({"degree": "Ph.D. in Physics"}) == "phd"
    assert config.degree_level({"degree": "MBA"}) == "master"
    assert config.degree_level({"degree": "Whatever", "level": "Bachelor"}) == "bachelor"
