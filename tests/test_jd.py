"""JD readers for sites whose job pages are JavaScript shells, and where JD-unread jobs are listed."""
from datetime import datetime, timezone

import pytest

from jobagent import http, jd

LONG = "<p>" + "Build and ship features with the team. " * 20 + "</p>"


@pytest.fixture
def fake_json(monkeypatch):
    """Answer http.get_json from a {url-substring: payload} map and record the URLs asked for."""
    asked, replies = [], {}

    def get_json(url, **kw):
        asked.append(url)
        for key, payload in replies.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected URL {url}")

    def no_page(url, **kw):  # the generic page reader must not be needed
        raise AssertionError(f"generic reader used for {url}")

    monkeypatch.setattr(http, "get_json", get_json)
    monkeypatch.setattr(http, "get_text", no_page)
    return asked, replies


def test_workable_reads_the_public_job_api(fake_json):
    asked, replies = fake_json
    replies["/api/v2/accounts/bask-health-1/jobs/E81C7F7D3B"] = {
        "description": LONG, "requirements": "<ul><li>Currently pursuing a CS degree</li></ul>", "benefits": "",
        "location": {"city": "", "region": None, "country": "United States"}, "workplace": "remote"}
    d = jd.fetch_detail("https://apply.workable.com/bask-health-1/j/E81C7F7D3B/", "other", "Bask")
    assert d is not None and d.via == "workable"
    assert "Currently pursuing a CS degree" in d.text and d.location == "Remote (United States)"
    assert asked == ["https://apply.workable.com/api/v2/accounts/bask-health-1/jobs/E81C7F7D3B"]


def test_bamboohr_reads_the_careers_detail_json(fake_json):
    asked, replies = fake_json
    replies["surepoint.bamboohr.com/careers/109/detail"] = {"result": {"jobOpening": {
        "description": LONG, "locationType": "0",
        "location": {"city": "Manhattan", "state": "Kansas", "addressCountry": "United States"}}}}
    d = jd.fetch_detail("https://surepoint.bamboohr.com/careers/109/", "other", "SurePoint")
    assert d is not None and d.via == "bamboohr" and d.location == "Manhattan, Kansas, United States"
    assert asked == ["https://surepoint.bamboohr.com/careers/109/detail"]


def test_dover_reads_the_application_portal_job(fake_json):
    asked, replies = fake_json
    jid = "a984ef07-3cd6-488b-9c66-ac95b6feb640"
    replies[f"/inbound/application-portal-job/{jid}"] = {
        "user_provided_description": LONG, "visa_support": True,
        "locations": [{"location_type": "REMOTE", "location_option": {"display_name": "United States"}},
                      {"location_type": "IN_OFFICE", "location_option": {"display_name": "Boston, MA"}}]}
    d = jd.fetch_detail(f"https://app.dover.io/apply/job/{jid}/", "other", "Rivet")
    assert d is not None and d.via == "dover"
    assert d.location == "Remote (United States); Boston, MA"
    assert "Visa sponsorship offered: yes" in d.text
    assert asked == [f"https://app.dover.com/api/v1/inbound/application-portal-job/{jid}"]


def test_other_hosts_still_use_the_page_reader(monkeypatch):
    seen = []
    monkeypatch.setattr(http, "get_text", lambda url, **kw: seen.append(url) or "<html></html>")
    assert jd.fetch_detail("https://careers.example.com/jobs/42", "other", "Example") is None
    assert seen == ["https://careers.example.com/jobs/42"]


def _missing_job(i: str, company: str) -> dict:
    return {"id": i, "company": company, "title": "Software Engineering Intern", "url": f"https://jobs.example/{i}",
            "ats": "other", "date_posted": 0, "reason": "不开浏览器读不到 JD，申请时在页面上核对", "resume": "SDE"}


class _FakeDB:
    """Only JD-unread jobs exist; every other query comes back empty."""

    def __init__(self, rows):
        self.missing = rows

    def rows(self, where="1", params=(), **kw):
        return list(self.missing) if "jd_missing" in where else []


def test_manual_list_shows_jd_unread_jobs():
    from jobagent import report
    path = report.write_manual_list(_FakeDB([_missing_job("m1", "Rivet")]))
    text = path.read_text(encoding="utf-8")
    assert "JD 没读到 1 个" in text
    assert "## JD 没读到：程序不会自动投（1 个）" in text
    assert "[Software Engineering Intern](https://jobs.example/m1)" in text and "`m1`" in text


def test_daily_report_lists_jd_unread_jobs(tmp_path, monkeypatch):
    from jobagent import daily
    from jobagent.pipeline import FetchStats, ScreenStats
    monkeypatch.setattr(daily, "REPORT_DIR", tmp_path)
    db = _FakeDB([_missing_job("m1", "Rivet"), _missing_job("m2", "Operio")])
    path = daily.write_report(db, FetchStats(per_source={"Simplify": 1}, new=2), ScreenStats(attempted=2), [],
                              [], datetime.now(timezone.utc), None)
    text = path.read_text(encoding="utf-8")
    assert "JD 没读到，程序不会自动投（过去 24 小时新增 2 个，累计 2 个）" in text
    assert "[Rivet — Software Engineering Intern](https://jobs.example/m1)" in text
    assert "mark <ID> apply" in text
