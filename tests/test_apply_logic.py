from jobagent.apply.answers import _deterministic, _sensitive_conflict
from jobagent.apply.forms import _search_terms, best_option, missing_required
from jobagent.apply.runner import read_credentials

PROFILE = {
    "name": {"first": "Alex", "last": "Rivera", "preferred": "Al"},
    "contact": {"email": "a@b.com", "phone": "(555) 010-0199", "address": {"country": "United States"}},
    "links": {"linkedin": "https://linkedin.com/in/x", "github": "https://github.com/x"},
}


def test_best_option():
    assert best_option("Yes", ["Yes", "No"]) == "Yes"
    assert best_option("No", ["Yes", "No, I do not require sponsorship"]) == "No, I do not require sponsorship"
    assert best_option("No", ["I am not a protected veteran", "Decline"]) is None  # "no" is not "not"
    assert best_option("United States", ["United States Minor Outlying Islands", "United States +1", "Canada +1"]) == "United States +1"
    assert best_option("Chicago", ["Chicago Heights, Illinois, United States", "Chicago, Illinois, United States"]) == \
        "Chicago, Illinois, United States"
    assert best_option("The University of Michigan", ["University of Michigan", "Michigan State University"]) == "University of Michigan"
    assert best_option("Master's", ["Bachelor's Degree", "Master's Degree", "Doctorate"]) == "Master's Degree"
    assert best_option("Asian", ["Asian (Not Hispanic or Latino)", "White"]) == "Asian (Not Hispanic or Latino)"
    assert best_option("", ["Yes"]) is None


def test_search_terms():
    assert _search_terms("The University of Michigan") == ["The University of Michigan", "University of Michigan"]
    assert _search_terms("Chicago") == ["Chicago"]


def test_sensitive_conflict():
    f = lambda label: {"label": label}  # noqa: E731
    assert _sensitive_conflict(f("Will you now or in the future require sponsorship?"), "No") is None
    assert _sensitive_conflict(f("Will you now or in the future require sponsorship?"), "Yes") is not None
    assert _sensitive_conflict(f("Are you legally authorized to work in the United States?"), "No") is not None
    assert _sensitive_conflict(f("Are you a U.S. citizen?"), "Yes") is not None
    assert _sensitive_conflict(f("Are you a U.S. citizen?"), "No") is None
    # compound / negated questions are left to the planner
    assert _sensitive_conflict(f("Are you authorized to work and will you not require sponsorship?"), "Yes") is None


def test_deterministic_identity_and_files():
    row = {"resume": "SDE"}
    mk = lambda kind, label, **kw: {"id": "x", "kind": kind, "label": label, **kw}  # noqa: E731
    assert _deterministic(mk("text", "First Name"), PROFILE, row).value == "Alex"
    assert _deterministic(mk("text", "Legal Last Name"), PROFILE, row).value == "Rivera"
    assert _deterministic(mk("text", "Name"), PROFILE, row).value == "Alex Rivera"
    assert _deterministic(mk("email", "Email"), PROFILE, row).value == "a@b.com"
    assert _deterministic(mk("text", "LinkedIn Profile"), PROFILE, row).value == "https://linkedin.com/in/x"
    assert _deterministic(mk("file", "Resume/CV"), PROFILE, row).value == "resume"
    assert _deterministic(mk("file", "Cover Letter"), PROFILE, row).value == ""
    assert _deterministic(mk("file", "Transcript", required=False), PROFILE, row).value == ""
    assert _deterministic(mk("file", "Transcript", required=True), PROFILE, row).value == "transcript"
    assert _deterministic(mk("text", "Why do you want to work here?"), PROFILE, row) is None


def test_phone_country_is_united_states():
    # Samsung (2026-10-31): the LLM picked "China +86" for the phone's country code from the citizenship
    row = {"resume": "SDE"}
    dial = ["China +86", "United States Minor Outlying Islands +1", "Canada +1", "United States +1"]
    assert _deterministic({"id": "x", "kind": "select", "label": "Country", "options": dial}, PROFILE, row).value == \
        "United States +1"
    assert _deterministic({"id": "x", "kind": "combobox", "label": "Phone Country Code", "options": ["+86 China", "+1 United States"]},
                          PROFILE, row).value == "+1 United States"
    assert _deterministic({"id": "x", "kind": "select", "label": "Country", "options": ["China", "United States", "Canada"]},
                          PROFILE, row).value == "United States"
    assert _deterministic({"id": "x", "kind": "combobox", "label": "Country"}, PROFILE, row).value == "United States"
    assert _deterministic({"id": "x", "kind": "select", "label": "Country of citizenship", "options": ["China"]}, PROFILE, row) is None


def test_missing_required():
    fields = [
        {"id": "a", "label": "First", "required": True, "value": "Alex"},
        {"id": "b", "label": "Auth", "required": True, "value": []},
        {"id": "c", "label": "Optional", "required": False, "value": ""},
    ]
    assert missing_required(fields) == ["Auth"]


def test_read_credentials(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("招聘网站注册账号\nemail: me@x.com\npassword: s3cret!\n", encoding="utf-8")
    assert read_credentials(p) == ("me@x.com", "s3cret!")
    assert read_credentials(tmp_path / "missing.txt") is None


def test_greenhouse_code_regex():
    import re
    pat = re.compile(r"(?i)(?:application|code)\s*:\s*([A-Za-z0-9]{8})\b")
    body = ("Security code for your application. Copy and paste this code into the security code field on your "
            "application: Ab3dE9xZ After you enter the code, resubmit your application.")
    assert pat.search(body).group(1) == "Ab3dE9xZ"


def test_workday_field_rules():
    row = {"resume": "SDE"}
    mk = lambda kind, label: {"id": "x", "kind": kind, "label": label}  # noqa: E731
    assert _deterministic(mk("text", "Phone Number"), PROFILE, row).value == "(555) 010-0199"
    assert _deterministic(mk("text", "Phone Extension"), PROFILE, row) is None
    assert _deterministic(mk("checkbox_single", "I have a preferred name"), PROFILE, row).value is False


def test_dispatcher_never_overshoots_and_keeps_companies_apart():
    from jobagent.apply.runner import _Dispatcher
    rows = [{"company": c, "id": str(i)} for i, c in enumerate(["A", "A", "B", "C", "D"])]
    d = _Dispatcher(rows, target=2, max_attempts=10, low_water=1)
    first, second = d.take(), d.take()
    assert {first["company"], second["company"]} == {"A", "B"}   # the second "A" waits while A is busy
    d.done(first, success=False)                                  # a failure frees a slot...
    third = d.take()
    assert third["company"] in {"A", "C"}
    d.done(second, success=True)
    d.done(third, success=True)                                   # ...and two successes reach the target
    assert d.take() is None and d.succeeded == 2


def test_dispatcher_stops_when_exhausted():
    from jobagent.apply.runner import _Dispatcher
    d = _Dispatcher([{"company": "A", "id": "1"}], target=5, max_attempts=10, low_water=1)
    r = d.take()
    d.done(r, success=False)
    d.no_more()
    assert d.take() is None


def test_dispatcher_requeues_transient_failures():
    from jobagent.apply.runner import _Dispatcher
    d = _Dispatcher([{"company": "A", "id": "1"}], target=1, max_attempts=1, low_water=1)
    d.no_more()
    r = d.take()
    assert d.may_retry(r)
    d.done(r, success=False, requeue=True)     # rate-limited: goes back, attempt not used up
    again = d.take()
    assert again is not None and again["id"] == "1"
    d.done(again, success=True)
    assert d.take() is None


def test_runner_tops_up_from_background_screening(tmp_path, monkeypatch):
    """While older jobs are screened in the background, jobs it passes join the queue after today's."""
    import threading
    import time
    from collections import Counter
    from contextlib import nullcontext
    from jobagent import config, pipeline
    from jobagent.apply import runner
    from jobagent.db import DB
    from jobagent.models import Posting
    from jobagent.rules import job_id
    monkeypatch.setattr(config, "APPLICATIONS_CSV", tmp_path / "applications.csv")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "SCREEN_POLL_SECONDS", 0)
    monkeypatch.setattr(runner, "BrowserSession", lambda *a, **k: nullcontext(object()))
    monkeypatch.setattr(runner, "read_credentials", lambda path: {})
    monkeypatch.setattr(runner.Mailbox, "from_env", staticmethod(lambda email: None))
    monkeypatch.setattr(runner, "_record", lambda db, row, out, mode: None)
    monkeypatch.setattr(pipeline, "score_fit", lambda db, llm=None: 0)
    monkeypatch.setattr(pipeline, "screen_pending", lambda db, *a, **k: pipeline.ScreenStats())
    applied = []
    monkeypatch.setattr(runner, "apply_job", lambda session, row, ctx: applied.append(row["company"])
                        or runner.Outcome("dry_run", "ok"))
    path = tmp_path / "jobs.db"
    db = DB(path)
    now = int(time.time())
    for name in ("Today", "Older"):
        db.upsert(Posting("Simplify", name, "SWE Intern", f"https://x.com/{name}", ["Chicago, IL"], "Software", now))
    db.update(job_id("https://x.com/Today"), stage="eligible")
    db.commit()

    def screen_older():  # the background screen passes "Older" a moment after applying has started
        time.sleep(0.5)
        other = DB(path)
        other.update(job_id("https://x.com/Older"), stage="eligible")
        other.commit()
        other.close()
        time.sleep(0.5)
    screening = threading.Thread(target=screen_older)
    screening.start()
    llm = type("FakeLLM", (), {"usage": Counter()})()
    results = runner._run_apply(db, "dry_run", 5, None, True, False, 1, llm, screening)
    assert applied == ["Today", "Older"]
    assert [o.status for _, o in results] == ["dry_run", "dry_run"]


def test_workday_activation_link_pattern():
    import re
    host = "zillow.wd5.myworkdayjobs.com"
    pat = re.compile(rf"(https://{re.escape(host)}/[^\s\"'<>]*?/activate/[^\s\"'<>]+)")
    body = ("Click this link https://zillow.wd5.myworkdayjobs.com/Zillow_Group_External/activate/abc123/?redirect=%2Fapply "
            "or visit https://other.wd1.myworkdayjobs.com/x/activate/zzz/")
    assert pat.search(body).group(1).startswith("https://zillow.wd5.myworkdayjobs.com/Zillow_Group_External/activate/abc123/")


def test_date_parts():
    from jobagent.apply.forms import date_parts
    assert date_parts("07/2026", "MM/YYYY") == ["07", "2026"]
    assert date_parts("Jul 2026", "MM/YYYY") == ["07", "2026"]
    assert date_parts("10/28/2026", "MM/DD/YYYY") == ["10", "28", "2026"]
    assert date_parts("2026-07-15", "MM/DD/YYYY") == ["07", "15", "2026"]
    assert date_parts("Sep 2022", "YYYY") == ["2022"]
    assert date_parts("Present", "MM/YYYY") is None
    assert date_parts("13/2025", "MM/YYYY") is None


def test_answer_cache_reuses_llm_answers(tmp_path, monkeypatch):
    from jobagent import config
    from jobagent.apply import answers
    from jobagent.apply.answers import plan
    monkeypatch.setattr(answers, "_SHARED_PATH", tmp_path / "answer_cache.json")   # never touch data/
    monkeypatch.setattr(answers, "_shared", None)

    class FakeLLM:
        calls = 0

        def json_chat(self, system, user, max_tokens):
            FakeLLM.calls += 1
            return {"answers": {"a1": {"value": "No", "basis": "fact"}, "a2": {"value": "Engineer", "basis": "resume"},
                                "a3": {"value": "Analyst", "basis": "resume"}}}

    profile = config.profile()
    resume = config.ROOT / profile["resumes"]["SDE"]["pdf"]
    row = {"company": "X", "title": "Intern", "resume": "SDE"}
    q = {"id": "a1", "kind": "radio", "label": "Previously worked here?", "required": True,
         "options": ["Yes", "No"], "option_ids": ["o1", "o2"], "value": [], "nth": 0}
    t1 = {"id": "a2", "kind": "text", "label": "Job Title", "required": True, "value": "", "nth": 0}
    t2 = {"id": "a3", "kind": "text", "label": "Job Title", "required": True, "value": "", "nth": 1}
    cache: dict = {}
    first = plan([q, t1, t2], FakeLLM(), profile, row, resume, cache)
    assert FakeLLM.calls == 1 and [it.value for it in first.items] == ["No", "Engineer", "Analyst"]
    redrawn = [dict(q, id="b1"), dict(t1, id="b2"), dict(t2, id="b3")]   # same page drawn again: new ids
    again = plan(redrawn, FakeLLM(), profile, row, resume, cache)
    assert FakeLLM.calls == 1 and [it.value for it in again.items] == ["No", "Engineer", "Analyst"]


def test_rate_limiter_backs_off_and_recovers():
    from jobagent.llm import _RateLimiter
    lim = _RateLimiter(0.5)
    lim.slow_down()
    lim.slow_down()
    assert lim.interval > 2.0
    for _ in range(50):
        lim.speed_up()
    assert lim.interval == 2.0


def test_sensitive_conflict_ignores_other_countries():
    f = {"label": "Do you hold Legal Permanent Resident Status in a country outside the US?"}
    assert _sensitive_conflict(f, "Yes") is None
    assert _sensitive_conflict({"label": "Are you a U.S. permanent resident?"}, "Yes") is not None


def test_shared_cache_serves_other_jobs(tmp_path, monkeypatch):
    from jobagent import config
    from jobagent.apply import answers
    monkeypatch.setattr(answers, "_SHARED_PATH", tmp_path / "answer_cache.json")
    monkeypatch.setattr(answers, "_shared", None)

    class FakeLLM:
        calls = 0

        def json_chat(self, system, user, max_tokens):
            FakeLLM.calls += 1
            return {"answers": {"g1": {"value": "Male", "basis": "fact"}, "t1": {"value": "N/A", "basis": "default"}}}

    profile = config.profile()
    resume = config.ROOT / profile["resumes"]["SDE"]["pdf"]
    gender = {"id": "g1", "kind": "select", "label": "Gender", "required": True, "options": ["Male", "Female"], "value": "", "nth": 0}
    other = {"id": "t1", "kind": "select", "label": "If you selected Other above, specify", "required": False,
             "options": ["N/A", "Other"], "value": "", "nth": 0}
    answers.plan([gender, other], FakeLLM(), profile, {"company": "A", "title": "x"}, resume, {})
    assert FakeLLM.calls == 1
    answers.plan([dict(gender, id="g9")], FakeLLM(), profile, {"company": "B", "title": "y"}, resume, {})
    assert FakeLLM.calls == 1                                    # another job: gender comes from the shared cache
    answers.plan([dict(other, id="t9")], FakeLLM(), profile, {"company": "B", "title": "y"}, resume, {})
    assert FakeLLM.calls == 2                                    # follow-up questions are never shared


def test_other_followup_gets_na():
    from jobagent import config
    from jobagent.apply import answers

    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            return {"answers": {"x": {"value": "__UNKNOWN__", "basis": "unknown"}}}

    profile = config.profile()
    f = {"id": "x", "kind": "text", "label": "If you selected Other, please specify below:", "required": True, "value": "", "nth": 0}
    p = answers.plan([f], FakeLLM(), profile, {"company": "A", "title": "x"}, config.ROOT / profile["resumes"]["SDE"]["pdf"], {})
    assert not p.unknown and p.items[0].value == "N/A"


def _fake_llm(monkeypatch, behaviour):
    """An LLM whose slots call `behaviour(model, key_no)` instead of the network."""
    from types import SimpleNamespace
    from jobagent import config, llm as llm_mod
    monkeypatch.setattr(config, "llm_credentials", lambda: (["k1", "k2"], "http://x", "primary"))
    monkeypatch.setattr(config, "settings", lambda: {"llm": {"fallback_models": ["backup"]}})
    client = llm_mod.LLM(rps=1000)
    for s in client.slots:
        def create(s=s, **kw):
            behaviour(s.model, s.key_no, kw)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": "%s"}' % s.model))])
        s.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client


def _api_error(cls, message: str):
    e = cls.__new__(cls)          # skip the SDK constructor (it wants a real HTTP response)
    e.args = (message,)
    return e


def _rate_limit():
    from openai import RateLimitError
    return _api_error(RateLimitError, "inference exceeds tpm/rpm limit")


def test_llm_prefers_primary_model(monkeypatch):
    client = _fake_llm(monkeypatch, lambda m, k, kw: None)
    assert client.json_chat("s", "u") == {"ok": "primary"}


def test_llm_falls_back_when_primary_is_rate_limited(monkeypatch):
    def behaviour(model, key, kw):
        if model == "primary":
            raise _rate_limit()
    client = _fake_llm(monkeypatch, behaviour)
    assert client.json_chat("s", "u") == {"ok": "backup"}
    assert client.usage == {"backup": 1}
    assert all(s.cool_until > 0 for s in client.slots if s.model == "primary")   # both primary keys cool down


def test_llm_retries_with_temperature_one(monkeypatch):
    from openai import BadRequestError

    def behaviour(model, key, kw):
        if kw["temperature"] != 1.0:
            raise _api_error(BadRequestError, "field Temperature invalid, only 1 is allowed for this model")
    client = _fake_llm(monkeypatch, behaviour)
    assert client.json_chat("s", "u") == {"ok": "primary"}


def test_veteran_claims_are_conflicts():
    f = {"label": "Please select the veteran status which most accurately describes how you identify yourself"}
    assert _sensitive_conflict(f, "I identify as a veteran, just not a protected veteran")
    assert _sensitive_conflict(f, "I identify as one or more of the classifications of protected veteran")
    assert _sensitive_conflict(f, "I am not a veteran") is None
    assert _sensitive_conflict(f, "I am not a protected veteran") is None


def test_already_applied_detection():
    from jobagent.apply.adapters import Adapter
    from jobagent.apply.forms import Snapshot
    snap = lambda t: Snapshot([], [], [], False, "", "", t, [])   # noqa: E731
    a = Adapter()
    assert a.already_applied(snap("Summer 2027 Intern You applied for this job on October 27, 2026. View Application")) == "October 27, 2026"
    assert a.already_applied(snap("Innovation Intern You've already applied for this job. View My Applications")) == ""
    assert a.already_applied(snap("Apply now to join our team")) is None
    assert a.already_applied(snap("If you have already applied for this position during the current recruitment season "
                                  "and were not selected, you may reapply")) is None   # IMC job description


def test_llm_retries_empty_reply_on_another_model(monkeypatch):
    client = _fake_llm(monkeypatch, lambda m, k, kw: None)
    from types import SimpleNamespace
    for s in client.slots:
        if s.model == "primary":
            s.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(model="primary", choices=[SimpleNamespace(
                    finish_reason="length", message=SimpleNamespace(content=""))]))))
    assert client.json_chat("s", "u") == {"ok": "backup"}


def test_permanent_authorization_expects_no():
    f = {"label": "Are you legally authorized to work permanently in the United States?"}
    assert _sensitive_conflict(f, "No") is None
    assert _sensitive_conflict(f, "Yes") is not None
    assert _sensitive_conflict({"label": "Are you legally authorized to work in the United States?"}, "Yes") is None


def test_school_email_is_deterministic():
    from jobagent import config
    p = config.profile()
    it = _deterministic({"id": "e", "kind": "text", "label": "Please provide your university email address."}, p, None)
    assert it is not None and it.value == p["contact"]["school_email"]
    it = _deterministic({"id": "e", "kind": "email", "label": "Email"}, p, None)
    assert it.value == p["contact"]["email"]


def test_daily_report_lists_manual_links(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from jobagent import daily
    from jobagent.apply.runner import Outcome
    from jobagent.pipeline import FetchStats, ScreenStats
    monkeypatch.setattr(daily, "REPORT_DIR", tmp_path)

    class FakeDB:
        def rows(self, where, **kw):
            return []
    job = lambda i, c, t: {"id": i, "company": c, "title": t, "url": f"https://jobs.example/{i}"}  # noqa: E731
    results = [
        (job("a1", "Acme Robotics", "ML Intern"), Outcome("submitted", "提交成功")),
        (job("b2", "Beta Corp", "AI Intern"), Outcome("blocked", "有必填题答不了：Do you have a driver's license?（可在 config/answers.yaml 补充答案后重新排队）")),
        (job("c3", "Gamma", "SWE Intern"), Outcome("failed", "页面上没有找到申请表")),
        (job("d4", "Delta", "Research Intern"), Outcome("skip", "需要开放式回答：Why us?")),
        (job("e5", "Gamma", "Data Intern"), Outcome("failed", "点了下一步但一直停在同一页")),
    ]
    emails = [(datetime.now(timezone.utc), "gamma@myworkday.com", "Thank you for applying to Gamma")]
    path = daily.write_report(FakeDB(), FetchStats(per_source={"Simplify": 10}, new=3), ScreenStats(attempted=3),
                              results, emails, datetime.now(timezone.utc), None)
    text = path.read_text()
    assert "[Beta Corp — AI Intern](https://jobs.example/b2)" in text
    assert "Do you have a driver's license?（Beta Corp）" in text
    assert "邮箱里有这家公司的确认信" in text and "[Gamma — SWE Intern]" in text   # failed, but Gamma emailed
    assert "[Delta — Research Intern](https://jobs.example/d4)" in text             # essay list
    assert "- Acme Robotics — ML Intern" in text


def test_other_country_and_dhs_questions_are_not_conflicts():
    hpe = {"label": "For any of the countries listed below: Are you a citizen or have a passport, or permanent residency?"}
    assert _sensitive_conflict(hpe, "YES") is None
    toyota = {"label": "Are you required to be authorized by the Department of Homeland Security (DHS), e.g. an EAD, to work?"}
    assert _sensitive_conflict(toyota, "Yes") is None
    assert _sensitive_conflict({"label": "Are you a U.S. citizen or permanent resident?"}, "Yes") is not None


def _resume_data():
    return {
        "experience": [
            {"company": "Acme Cloud", "title": "AI Engineer Intern", "location": "Seattle", "start": "2026-07", "end": "2026-08",
             "bullets": ["Built an agent.", "Shipped a skill."]},
            {"company": "Globex", "title": "Technology Consultant Trainee", "location": "Toronto", "start": "2026-01",
             "end": "present", "bullets": ["Co-developed a framework."]},
        ],
        "education": [
            {"school": "The University of Michigan", "degree": "M.S. in Computer Science", "level": "master",
             "field": "Computer Science", "start": "2026-09", "end": "2028-03", "gpa": ""},
        ],
        "skills": {"AI & ML": ["PyTorch", "LangChain"], "Languages": ["Python", "Java", "TypeScript", "C"]},
    }


def test_resume_fill_workday_experience_page():
    from jobagent.apply.resume_fill import resolve
    f = lambda i, kind, label, nth=0, **kw: {"id": i, "kind": kind, "label": label, "nth": nth, "value": "", **kw}  # noqa: E731
    fields = [
        f("t1", "text", "Job Title"), f("c1", "text", "Company"), f("l1", "text", "Location"),
        f("cur1", "checkbox_single", "I currently work here"),
        f("d1", "date_parts", "Work Experience 1 — From", format="MM/YYYY"),
        f("e1", "date_parts", "Work Experience 1 — To (Actual or Expected)", format="MM/YYYY"),
        f("r1", "textarea", "Role Description"),
        f("t2", "text", "Job Title", 1), f("c2", "text", "Company", 1),
        f("cur2", "checkbox_single", "I currently work here", 1),
        f("e2", "date_parts", "Work Experience 2 — To", format="MM/YYYY"),
        f("sk", "combobox", "Type to Add Skills", prompt=True, options=[]),
        f("q", "radio", "Are you 18 or older?", options=["Yes", "No"]),
    ]
    got = resolve(fields, _resume_data())
    assert got["t1"] == "AI Engineer Intern" and got["c1"] == "Acme Cloud" and got["l1"] == "Seattle"
    assert got["cur1"] is False and got["d1"] == "2026-07" and got["e1"] == "2026-08"
    assert got["r1"] == "• Built an agent.\n• Shipped a skill."
    assert got["t2"] == "Technology Consultant Trainee" and got["c2"] == "Globex" and got["cur2"] is True
    assert "e2" not in got                       # still working there: no end date
    assert got["sk"] == "Python, Java, TypeScript, PyTorch, LangChain"
    assert "q" not in got                        # not a resume question: left to the LLM


def test_resume_fill_education_and_degree_options():
    from jobagent.apply.resume_fill import resolve
    fields = [
        {"id": "s", "kind": "combobox", "label": "School or University", "nth": 0, "value": "", "prompt": True, "options": []},
        {"id": "dg", "kind": "select", "label": "Degree", "nth": 0, "value": "",
         "options": ["High School", "Bachelor's Degree", "Master's Degree", "Doctorate"]},
        {"id": "fs", "kind": "combobox", "label": "Field of Study", "nth": 0, "value": "", "prompt": True, "options": []},
        {"id": "y", "kind": "date_parts", "label": "Education 1 — To (Actual or Expected)", "nth": 0, "value": "", "format": "YYYY"},
        {"id": "gpa", "kind": "text", "label": "Overall Result (GPA)", "nth": 0, "value": ""},
    ]
    got = resolve(fields, _resume_data())
    assert got["s"] == "The University of Michigan" and got["dg"] == "Master's Degree"
    assert got["fs"] == "Computer Science" and got["y"] == "2028-03"
    assert "gpa" not in got                      # no GPA on the resume: the LLM uses profile.yaml instead


def test_structured_resume_goes_stale_when_file_changes(tmp_path, monkeypatch):
    from jobagent import resume
    src = tmp_path / "r.pdf"
    src.write_bytes(b"v1")
    monkeypatch.setattr(resume, "DIR", tmp_path)
    monkeypatch.setattr(resume, "source", lambda kind, profile=None: src)
    monkeypatch.setattr(resume.config, "ROOT", tmp_path)
    monkeypatch.setattr(resume, "_cache", {})           # restored afterwards: later tests see the real resumes
    (tmp_path / "AIE.yaml").write_text(f"source: r.pdf\nsource_sha1: {resume._sha1(src)}\nexperience: []\n")
    resume._cache.clear()
    assert resume.structured("AIE") is not None
    src.write_bytes(b"v2")                       # resume edited after parsing
    resume._cache.clear()
    assert resume.structured("AIE") is None


def test_fuzzy_match_never_swaps_degree_levels():
    assert best_option("Master of Science", ["Bachelor of Science"]) is None
    assert best_option("Master of Science", ["Bachelor of Science", "Master's Degree"]) is None or \
        best_option("Master of Science", ["Bachelor of Science", "Master's Degree"]) == "Master's Degree"
    assert best_option("I am not a protected veteran", ["I am a protected veteran"]) is None
    assert best_option("Colour", ["Color"]) == "Color"           # ordinary typos still match


def test_how_did_you_hear_falls_back_to_first_option():
    from jobagent import config
    from jobagent.apply import answers

    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            return {"answers": {"h": {"value": "__UNKNOWN__", "basis": "unknown"}}}

    profile = config.profile()
    f = {"id": "h", "kind": "combobox", "label": "How Did You Hear About Us?", "required": True, "value": "", "nth": 0,
         "options": ["Select One", "Agency / Search Firm", "Current Employee", "GE Vernova Recruiter"]}
    p = answers.plan([f], FakeLLM(), profile, {"company": "GE", "title": "x"}, config.ROOT / profile["resumes"]["SDE"]["pdf"], {})
    assert not p.unknown and p.items[0].value == "Agency / Search Firm"


def test_skill_match_is_strict():
    from jobagent.apply.forms import skill_match
    assert skill_match("Python", ["Python IDLE", "Python (Programming Language)"]) == "Python (Programming Language)"
    assert skill_match("C", ["C NMR", "C (Programming Language)"]) == "C (Programming Language)"
    assert skill_match("tool calling", ["Cold Calling"]) is None
    assert skill_match("MCP", ["Unisys MCP"]) is None
    assert skill_match("PostgreSQL", ["PostgreSQL"]) == "PostgreSQL"


GH_FORM = """<form id="application-form"><div id="edu">
 <div class="block"><label for="school--0">School</label><input id="school--0" role="combobox" aria-autocomplete="list">
  <label for="degree--0">Degree</label><select id="degree--0"><option></option><option>Master's Degree</option><option>Bachelor's Degree</option></select>
  <label for="start-year--0">Start date year</label><input id="start-year--0" type="number"></div>
 <button type="button" id="add" onclick="
   const b = document.querySelector('.block').cloneNode(true);
   b.querySelectorAll('[id]').forEach(e => e.id = e.id.replace('--0', '--1'));
   b.querySelectorAll('[data-ja-id]').forEach(e => e.removeAttribute('data-ja-id'));
   b.querySelectorAll('label').forEach(l => l.htmlFor = l.htmlFor.replace('--0', '--1'));
   this.before(b);">Add another</button></div>
 <label for="q1">Why us?</label><textarea id="q1"></textarea></form>"""


def test_greenhouse_education_blocks():
    from playwright.sync_api import sync_playwright
    from jobagent.apply import forms
    from jobagent.apply.adapters import _GH_BLOCKS_JS
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(GH_FORM)
        labels = [f["label"] for f in forms.snapshot(page, "#application-form").fields]
        assert "Education 1 — School" in labels and "Education 1 — Degree" in labels and "Why us?" in labels
        assert page.evaluate(_GH_BLOCKS_JS, None) == {"edu": 1, "work": 0}
        assert page.evaluate(_GH_BLOCKS_JS, "edu") is True
        assert page.evaluate(_GH_BLOCKS_JS, None) == {"edu": 2, "work": 0}
        labels = [f["label"] for f in forms.snapshot(page, "#application-form").fields]
        assert "Education 2 — School" in labels and "Education 2 — Start date year" in labels
        b.close()


GH_EMPLOYMENT = """<form id="application-form"><div id="emp">
 <div class="job"><label for="company-name-0">Company name</label><input id="company-name-0">
  <label for="title-0">Title</label><input id="title-0"></div>
 <button type="button" onclick="
   const n = document.querySelectorAll('.job').length, b = document.querySelector('.job').cloneNode(true);
   b.querySelectorAll('[id]').forEach(e => e.id = e.id.replace(/-0$/, '-' + n));
   b.querySelectorAll('label').forEach(l => l.htmlFor = l.htmlFor.replace(/-0$/, '-' + n));
   this.before(b);">Add another</button></div>
 <div><label for="school--0">School</label><input id="school--0"><button type="button">Add another</button></div></form>"""


def test_greenhouse_employment_blocks_with_single_dash_ids():
    # Samsung Semiconductor's board: "company-name-0", not "company-name--0" — the block was never counted or added
    from playwright.sync_api import sync_playwright
    from jobagent.apply.adapters import _GH_BLOCKS_JS
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(GH_EMPLOYMENT)
        assert page.evaluate(_GH_BLOCKS_JS, None) == {"edu": 1, "work": 1}
        assert page.evaluate(_GH_BLOCKS_JS, "work") is True and page.evaluate(_GH_BLOCKS_JS, "work") is True
        assert page.evaluate(_GH_BLOCKS_JS, None) == {"edu": 1, "work": 3}
        b.close()


def test_entry_problems_match_by_content():
    from types import SimpleNamespace
    from jobagent.apply.adapters import entry_problems
    from jobagent.apply.forms import Snapshot
    ctx = SimpleNamespace(row={"resume": "AIE"})
    f = lambda label, value: {"id": label, "kind": "combobox", "label": label, "value": value}  # noqa: E731
    ok = Snapshot([f("Education 1 — School", "University of Toronto"), f("Education 1 — Degree", "Bachelor's"),
                   f("Education 2 — School", "The University of Michigan"), f("Education 2 — Degree", "Master's")],
                  [], [], False, "", "", "", [])
    assert entry_problems(ok, ctx) == []                       # any order is fine
    other = Snapshot([f("Education 1 — School", "University of Michigan"), f("Education 2 — School", "Other")],
                     [], [], False, "", "", "", [])
    assert entry_problems(other, ctx) == []                    # unlisted school → "Other" is accepted
    wrong = Snapshot([f("Education 1 — School", "University of Michigan"),
                      f("Education 2 — School", "Yo San University of Traditional Chinese Medicine")],
                     [], [], False, "", "", "", [])
    assert any("University of Toronto" in p for p in entry_problems(wrong, ctx))
    empty = Snapshot([f("Education 1 — School", "University of Michigan"), f("Education 1 — Degree", "")],
                     [], [], False, "", "", "", [])
    assert any("空" in p for p in entry_problems(empty, ctx))


def test_confirmation_subjects():
    from jobagent.inbox import _CONFIRM, _NOT_CONFIRM
    is_conf = lambda s: bool(_CONFIRM.search(s) and not _NOT_CONFIRM.search(s))  # noqa: E731
    for s in ["Thank you for applying to Scale AI", "Got it! Application received for: REQ380492 Summer 2027",
              "Your application with CVS Health has been received!", "You've applied to a position at Analog Devices - DevOps Co-Op",
              "Thank you for your interest in NVIDIA", "Application Received for Software Engineering Co-op"]:
        assert is_conf(s), s
    for s in ["A Task Awaits You: Campus Job Submission", "Complete your application with Hewlett Packard Enterprise",
              "Update on Your Application – CARIAD, Inc.", "Security code for your application to Waymo",
              "Congratulations! You are invited to our screening process", "Your feedback matters - Cox Automotive Careers"]:
        assert not is_conf(s), s


def test_inbox_store_dedup_and_autoclose(tmp_path):
    from jobagent.db import DB
    from jobagent import inbox
    db = DB(tmp_path / "t.db")
    m = lambda i, day, subj: {"msg_id": i, "received": f"2026-10-{day}T09:00:00-05:00", "sender": "hpe@myworkday.com",  # noqa: E731
                              "subject": subj, "text": "", "links": []}
    v = lambda kind, company="Hewlett Packard Enterprise", action=True: {  # noqa: E731
        "kind": kind, "company": company, "action": action, "deadline": "", "link": "https://x/assess", "summary": "完成测评"}
    mails = [m("a", 27, "Complete your application with HPE"), m("b", 28, "Complete your application with HPE"),
             m("c", 29, "BCG X: Coffee Chat Invitation"), m("d", 29, "Your IBM Application Status")]
    inbox.store(db, mails, {"a": v("task"), "b": v("task"), "c": v("interview", "BCG X"), "d": v("rejection", "IBM", False)})
    items = inbox.open_items(db)
    assert [i["kind"] for i in items] == ["interview", "task"]          # interviews first; IBM rejection isn't open
    assert next(i for i in items if i["kind"] == "task")["count"] == 2  # the two HPE reminders collapse into one
    inbox.store(db, [m("e", 30, "Thank you for completing your HPE assessment")], {"e": v("completed", action=False)})
    assert [i["kind"] for i in inbox.open_items(db)] == ["interview"]    # HPE closed by the "completed" email
    bcg = inbox.open_items(db)[0]["id"]
    assert inbox.mark(db, bcg) and inbox.open_items(db) == []


def test_confirmations_search_a_day_back_across_utc_midnight(monkeypatch):
    """A 19:30 CST run is 01:30 UTC the next day; its confirmations must still be found."""
    from datetime import datetime, timezone
    from jobagent.apply import mail
    searches = []

    class FakeIMAP:
        def __init__(self, host): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def select(self, *a, **k): pass

        def search(self, charset, query):
            searches.append(query)
            return "OK", [b"1 2"]

        def fetch(self, num, what):
            date, subject = {b"1": ("Mon, 2 Nov 2026 01:00:00 +0000", "Daily digest"),
                             b"2": ("Mon, 2 Nov 2026 01:41:36 +0000", "Thank You For Applying!")}[num]
            return "OK", [(b"", f"From: x@myworkday.com\r\nSubject: {subject}\r\nDate: {date}\r\n\r\n".encode())]
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    since = datetime(2026, 11, 2, 1, 33, tzinfo=timezone.utc)
    found = mail.Mailbox("me@example.com", "pw").confirmations(since)
    assert searches == ['(SINCE "01-Nov-2026")']
    assert [subj for _, _, subj in found] == ["Thank You For Applying!"]
