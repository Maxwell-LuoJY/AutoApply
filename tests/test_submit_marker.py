"""The "submitting" marker: a run that dies between the submit click and recording the outcome must not let the next
run apply to the same job again. All jobs here are fictional and nothing is opened in a browser."""
import csv
import time
from collections import Counter
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from jobagent import config, pipeline, report
from jobagent.apply import forms, runner
from jobagent.apply.adapters import Failed
from jobagent.db import DB
from jobagent.llm import Throttled
from jobagent.models import Posting
from jobagent.rules import job_id

QA = [{"label": "First Name", "kind": "text", "value": "Alex", "basis": "profile", "error": None, "nth": 0}]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "APPLICATIONS_CSV", tmp_path / "applications.csv")
    return tmp_path


def _db(path, *companies):
    db = DB(path)
    for name in companies:
        db.upsert(Posting("Simplify", name, "SWE Intern", f"https://jobs.example/{name}", ["Ann Arbor, MI"], "Software",
                          int(time.time())))
        db.update(job_id(f"https://jobs.example/{name}"), stage="eligible")
    db.commit()  # the marker is written on another connection, which would wait on an open transaction
    return db


def _row(db, name):
    return db.get(job_id(f"https://jobs.example/{name}"))


def _csv(home):
    with (home / "applications.csv").open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _fake_run(monkeypatch, outcome="dry_run"):
    """_run_apply with no browser, mailbox or LLM; returns the list of companies apply_job was called for."""
    monkeypatch.setattr(runner, "BrowserSession", lambda *a, **k: nullcontext(object()))
    monkeypatch.setattr(runner, "read_credentials", lambda path: None)
    monkeypatch.setattr(runner.Mailbox, "from_env", staticmethod(lambda email: None))
    monkeypatch.setattr(pipeline, "score_fit", lambda db, llm=None: 0)
    monkeypatch.setattr(pipeline, "screen_pending", lambda db, *a, **k: pipeline.ScreenStats())
    applied = []
    monkeypatch.setattr(runner, "apply_job", lambda session, row, ctx: applied.append(row["company"])
                        or runner.Outcome(outcome, "ok"))
    return applied


def test_interrupted_submit_goes_to_the_manual_list_and_is_not_applied_again(home, monkeypatch):
    path = home / "jobs.db"
    db = _db(path, "Crashed Robotics", "Fresh Analytics")
    runner._mark_submitting(path, _row(db, "Crashed Robotics"), QA, [str(home / "x-before-submit.png")],
                            "https://jobs.example/Crashed Robotics/apply")  # ...and then the process died
    applied = _fake_run(monkeypatch)
    llm = SimpleNamespace(usage=Counter())

    results = runner._run_apply(db, "dry_run", 5, None, True, False, 1, llm, None)  # even a dry run recovers it
    assert applied == ["Fresh Analytics"]
    crashed = [(r, o) for r, o in results if r["company"] == "Crashed Robotics"]
    assert len(crashed) == 1
    o = crashed[0][1]
    assert o.status == "unconfirmed" and o.reason.startswith("点了提交但") and "jobagent brief" in o.reason
    assert o.answers == QA and o.screenshots and o.url.endswith("/apply")  # what the marker saved, for the review
    row = _row(db, "Crashed Robotics")
    assert row["stage"] == "manual" and row["reason"] == o.reason
    rows = _csv(home)
    assert [(r["公司名称"], r["状态"]) for r in rows] == [("Crashed Robotics", "失败")]  # the dry run itself: no row
    assert rows[0]["失败原因"] == o.reason

    manual = report.write_manual_list(db).read_text(encoding="utf-8")
    maybe = manual.split("## 可能已经提交")[1].split("## 待手动投递")[0]
    assert "Crashed Robotics" in maybe and "Crashed Robotics" not in manual.split("## 待手动投递")[1]
    review = runner.write_review(results, "dry_run").read_text(encoding="utf-8")
    assert "❓ 未确认" in review and "First Name" in review

    applied.clear()
    again = runner._run_apply(db, "dry_run", 5, None, True, False, 1, llm, None)  # the marker is closed now
    assert applied == ["Fresh Analytics"] and [r["company"] for r, _ in again] == ["Fresh Analytics"]
    assert len(_csv(home)) == 1


def test_interrupted_job_is_held_back_even_when_named(home, monkeypatch):
    """`jobagent apply --submit <ID>` right after a crash: the inbox has to be checked first."""
    path = home / "jobs.db"
    db = _db(path, "Crashed Robotics")
    runner._mark_submitting(path, _row(db, "Crashed Robotics"), [], [], "")
    applied = _fake_run(monkeypatch, outcome="submitted")
    results = runner._run_apply(db, "submit", None, [_row(db, "Crashed Robotics")["id"]], True, False, 1,
                                SimpleNamespace(usage=Counter()), None)
    assert applied == []
    assert [o.status for _, o in results] == ["unconfirmed"]


def test_recorded_outcome_supersedes_the_marker(home):
    path = home / "jobs.db"
    db = _db(path, "Done Dynamics", "Hand Applied Labs")
    done = _row(db, "Done Dynamics")
    runner._mark_submitting(path, done, QA, [], "")
    assert [a["job_id"] for a in db.latest_attempts(runner.SUBMITTING)] == [done["id"]]
    runner._record(db, done, runner.Outcome("submitted", "提交成功（看到确认页面）"), "submit")
    assert db.latest_attempts(runner.SUBMITTING) == []

    # the user found the confirmation email and ran `jobagent mark <ID> applied` before the next run
    by_hand = _row(db, "Hand Applied Labs")
    runner._mark_submitting(path, by_hand, [], [], "")
    pipeline.mark(db, by_hand["id"], "applied", None)
    assert runner.recover_interrupted(db) == []
    assert _row(db, "Hand Applied Labs")["stage"] == "applied"
    assert db.latest_attempts(runner.SUBMITTING) == []
    assert [r["状态"] for r in _csv(home) if r["公司名称"] == "Hand Applied Labs"] == ["已提交"]


class _Page:
    url = "https://jobs.example/apply"

    def screenshot(self, path, full_page):
        pass

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        pass


def _snap(text=""):
    return SimpleNamespace(fields=[], captcha=False, errors=[], frames=[], url=_Page.url, text=text, buttons=[])


def test_marker_is_written_before_the_click(home, monkeypatch):
    path = home / "jobs.db"
    db = _db(path, "Acme Robotics")
    row = _row(db, "Acme Robotics")
    monkeypatch.setattr(forms, "snapshot", lambda page, root: _snap())
    seen_at_click = []

    def click(page, btn):  # what another process would find if this one died right now
        other = DB(path)
        seen_at_click.append([a["job_id"] for a in other.latest_attempts(runner.SUBMITTING)])
        other.close()
    monkeypatch.setattr(runner, "click_button", click)
    adapter = SimpleNamespace(root=None, is_success=lambda snap: True, email_code_needed=lambda page, snap: False)
    ctx = SimpleNamespace(db_path=path, row=row, headed=False, submit_clicked=False)

    out = runner._submit(_Page(), adapter, {"id": "s"}, ctx, QA, [], lambda tag: None)
    assert out.status == "submitted" and seen_at_click == [[row["id"]]]

    seen_at_click.clear()
    ctx = SimpleNamespace(db_path=home, row=row, headed=False, submit_clicked=False)  # a folder: sqlite can't open it
    with pytest.raises(Failed, match="没有提交"):
        runner._submit(_Page(), adapter, {"id": "s"}, ctx, QA, [], lambda tag: None)
    assert seen_at_click == [] and not ctx.submit_clicked


class _Adapter:
    """One page with a submit button and nothing to fill. After the click the site shows "Something went wrong,
    refresh the page", which recover() would reload."""
    name, root = "greenhouse", None

    def __init__(self):
        self.clicked = False

    def open(self, page, row, ctx, snapshot):
        pass

    def recover(self, page, snap):
        return self.clicked

    def relevant(self, f):
        return True

    def already_applied(self, snap):
        return None

    def is_closed(self, snap):
        return False

    def handle_auth(self, page, snap, ctx):
        return False

    def submit_button(self, snap):
        return {"id": "submit", "text": "Submit application"}

    def next_button(self, snap):
        return None

    def prepare(self, page, ctx):
        return False

    def check(self, page, snap, ctx):
        return []

    def check_before_submit(self, page, ctx):
        return []

    def is_success(self, snap):
        return False

    def email_code_needed(self, page, snap):
        return False


@pytest.mark.parametrize("error", [PlaywrightError("Target page, context or browser has been closed"),
                                   Throttled("所有 API key 都被限流，稍后再试")])
def test_error_after_the_click_is_unconfirmed_never_retried_or_refilled(home, monkeypatch, error):
    path = home / "jobs.db"
    db = _db(path, "Acme Robotics")
    row = _row(db, "Acme Robotics")
    adapter = _Adapter()
    monkeypatch.setattr(runner, "adapter_for", lambda ats: adapter)
    monkeypatch.setattr(runner, "resolve_file", lambda *a: None)
    monkeypatch.setattr(runner, "_final_audit", lambda *a: None)
    monkeypatch.setattr(forms, "snapshot", lambda page, root: _snap())
    clicks = []

    def click(page, btn):
        clicks.append(btn["id"])
        adapter.clicked = True
        raise error
    monkeypatch.setattr(runner, "click_button", click)
    ctx = runner.ApplyContext(llm=None, profile={}, mode="submit", mailbox=None, credentials=None, shots_dir=home,
                              db_path=path)

    out = runner.apply_job(SimpleNamespace(new_page=_Page), row, ctx)
    assert clicks == ["submit"]  # not reloaded and submitted a second time
    assert out.status == "unconfirmed" and out.reason.startswith("点了提交但")  # not "retry", not "failed"
    assert [a["job_id"] for a in db.latest_attempts(runner.SUBMITTING)] == [row["id"]]
    runner._record(db, row, out, "submit")
    assert _row(db, "Acme Robotics")["stage"] == "manual" and db.latest_attempts(runner.SUBMITTING) == []
