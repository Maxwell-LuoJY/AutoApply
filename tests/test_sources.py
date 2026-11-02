from jobagent.history import History, append, load
from jobagent.sources import simplify, speedyapply

README = """
# 2027 Software Engineering Internship & New Grad Positions
## 2027 USA SWE Internships :books::eagle:
### FAANG+
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.amazon.com"><strong>Amazon</strong></a> | Software Development Engineer Intern - Summer 2027 | Redmond, WA | $53/hr | <a href="https://www.amazon.jobs/jobs/10587999/apply"><img src="x" alt="Apply"/></a> | 5d |
### Other
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://acme.com"><strong>Acme &amp; Co</strong></a> | SWE Intern | Remote | <a href="https://jobs.lever.co/acme/d5486403-c050-4920-b2e0-91b69b61ebb2"><img/></a> | 12d |
## 2027 USA New Grad
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://x.com"><strong>Nope</strong></a> | New Grad SWE | NYC | <a href="https://x.com/1"><img/></a> | 1d |
"""


def test_speedyapply_parse_only_intern_section():
    rows = speedyapply.parse(README, "## 2027 USA SWE Internships", now=1_000_000_000)
    assert [(r.company, r.title, r.locations) for r in rows] == [
        ("Amazon", "Software Development Engineer Intern - Summer 2027", ["Redmond, WA"]),
        ("Acme & Co", "SWE Intern", ["Remote"]),
    ]
    assert rows[0].date_posted == 1_000_000_000 - 5 * 86400
    assert rows[1].url.startswith("https://jobs.lever.co/acme/")


def test_simplify_parse_filters_term_category_visibility():
    data = [
        {"company_name": "A", "title": "SWE Intern", "url": "u1", "terms": ["Summer 2027"], "category": "Software",
         "active": True, "is_visible": True, "date_posted": 5},
        {"company_name": "B", "title": "x", "url": "u2", "terms": ["Summer 2026"], "category": "Software", "is_visible": True},
        {"company_name": "C", "title": "x", "url": "u3", "terms": ["Summer 2027"], "category": "Hardware", "is_visible": True},
        {"company_name": "D", "title": "x", "url": "u4", "terms": ["Summer 2027"], "category": "Software", "is_visible": False},
    ]
    rows = simplify.parse(data, ["Software", "AI/ML/Data"], "Summer 2027")
    assert [r.company for r in rows] == ["A"]


def test_history_roundtrip(tmp_path):
    path = tmp_path / "applications.csv"
    path.write_bytes("﻿申请时间,公司名称,岗位名称,类别,使用简历,申请链接,来源,状态,失败原因\n".encode("utf-8"))
    append(path, [
        {"公司名称": "Amgen", "岗位名称": "ML Intern", "申请链接": "https://a.com/1", "状态": "跳过"},
        {"公司名称": "Acme", "岗位名称": "SWE Intern", "申请链接": "https://b.com/2", "状态": "失败"},
    ])
    h: History = load(path)
    assert h.blocking_status("https://a.com/1?utm_source=x", "", "") == "跳过"
    assert h.blocking_status("https://other", "Amgen", "ML Intern") == "跳过"
    assert h.blocking_status("https://b.com/2", "Acme", "SWE Intern") is None  # 1 failure < 3
    assert path.read_bytes().startswith(b"\xef\xbb\xbf") and path.read_bytes().count(b"\xef\xbb\xbf") == 1


def test_history_counts_each_failure_once(tmp_path):
    path = tmp_path / "applications.csv"
    path.write_bytes("﻿申请时间,公司名称,岗位名称,类别,使用简历,申请链接,来源,状态,失败原因\n".encode("utf-8"))
    fail = {"公司名称": "Acme", "岗位名称": "SWE Intern", "申请链接": "https://b.com/2", "状态": "失败"}
    append(path, [fail, fail])
    assert load(path).blocking_status("https://b.com/2", "Acme", "SWE Intern") is None   # 2 failures, matched by link and name
    append(path, [fail])
    assert load(path).blocking_status("https://b.com/2", "Acme", "SWE Intern") == "失败 3 次"


def test_essay_skips_no_longer_block(tmp_path):
    path = tmp_path / "applications.csv"
    path.write_bytes("﻿申请时间,公司名称,岗位名称,类别,使用简历,申请链接,来源,状态,失败原因\n".encode("utf-8"))
    append(path, [{"公司名称": "Acme", "岗位名称": "SWE Intern", "申请链接": "https://b.com/2", "状态": "跳过",
                   "失败原因": "需要开放式回答：Why Acme?"},
                  {"公司名称": "Beta", "岗位名称": "ML Intern", "申请链接": "https://b.com/3", "状态": "跳过",
                   "失败原因": "需要开放式回答（题目要求不用 AI 写，需要你自己写）：Why?"}])
    h = load(path)
    assert h.blocking_status("https://b.com/2", "Acme", "SWE Intern") is None      # essay skip: may be retried
    assert h.blocking_status("https://b.com/3", "Beta", "ML Intern") == "跳过"     # "no AI" essay: still skipped
