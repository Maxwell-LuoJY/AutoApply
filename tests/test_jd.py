"""JD readers for sites whose job pages are JavaScript shells, and what happens when no reader gets the JD."""
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


def test_unread_jd_is_tried_unscreened(monkeypatch):
    from jobagent import screen as sc
    monkeypatch.setattr(sc, "fetch_detail", lambda url, ats, company: None)
    row = {"url": "https://careers-example.icims.com/jobs/1/job", "ats": "icims", "company": "Example"}
    res = sc.screen(row, llm=None, profile={}, term="Summer 2027", jd_max_chars=1000)   # the LLM is never asked
    assert res.stage == "eligible" and res.reason == sc.JD_UNREAD and res.data == {"jd_missing": True}


def test_fit_scoring_leaves_unread_jds_alone():
    import json
    from jobagent import pipeline

    class FakeDB:
        def rows(self, where="1", params=(), **kw):
            return [{"screen_json": json.dumps({"jd_missing": True}), "company": "Example", "title": "SWE Intern"}]
    assert pipeline.score_fit(FakeDB(), llm=None) == 0   # nothing fetched, no LLM built
