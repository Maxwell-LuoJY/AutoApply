"""Pre-submit check: read back what the page shows, compare with the facts, correct what can be corrected."""
from types import SimpleNamespace

from jobagent import config
from jobagent.apply import audit
from jobagent.apply.adapters import Adapter, Failed, Greenhouse, sections_on_page
from jobagent.apply.forms import Snapshot

ROW = {"company": "Samsung Semiconductor", "title": "Intern", "resume": "SDE"}


def _f(i, kind, label, value, **kw):
    return {"id": f"f{i}", "kind": kind, "label": label, "value": value, "required": True, "nth": 0, "options": [], **kw}


def test_rules_catch_the_samsung_phone():
    profile = config.profile()
    dial = ["China +86", "United States +1", "Canada +1"]
    bad = [_f(0, "select", "Country", "China +86", options=dial), _f(1, "tel", "Phone", "0555 010 0199"),
           _f(2, "email", "Email", "someone@else.com"), _f(3, "text", "First Name", "Alex")]
    problems = audit.rule_problems(bad, profile, ROW)
    assert [(p.item, p.fix) for p in problems] == [(0, "United States +1"), (1, profile["contact"]["phone"]),
                                                   (2, profile["contact"]["email"])]
    good = [_f(0, "select", "Country", "United States +1", options=dial), _f(1, "tel", "Phone", "+1 555-010-0199"),
            _f(2, "combobox", "Country", "+1"), _f(3, "email", "Email", profile["contact"]["email"])]
    assert audit.rule_problems(good, profile, ROW) == []
    assert audit.rule_problems([_f(0, "combobox", "Country", "Canada +1")], profile, ROW)[0].fix == "United States"


def test_judge_maps_problems_to_fields():
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            assert "[1] Education 1 — End date year (text, required) = 2027" in user and "Applicant facts" in system
            return {"ok": False, "problems": [{"item": 1, "field": "Education 1 — End date year", "shown": "2027",
                                               "expected": "2028", "fix": "2028"},
                                              {"item": None, "field": "Work Experience 1 — From", "shown": "07/2025",
                                               "expected": "07/2026", "fix": "07/2026"}]}
    items = [_f(0, "text", "Education 1 — School", "University of Michigan"), _f(1, "text", "Education 1 — End date year", "2027")]
    got = audit.judge(FakeLLM(), config.profile(), ROW, items, "", [])
    assert [(p.item, p.field, p.fix) for p in got] == [(1, "Education 1 — End date year", "2028"),
                                                       (None, "Work Experience 1 — From", "07/2026")]


def test_corrections_are_used_on_refill():
    ctx = SimpleNamespace(corrections={})
    audit.remember(ctx, [audit.Problem("Work Experience 1 — From", "07/2025", "07/2026", "07/2026"),
                         audit.Problem("Start date month", "May", "July", "July", item=3, nth=2)])
    assert audit.correction(ctx, {"label": "Work Experience 1 — From", "nth": 0}) == "07/2026"
    assert audit.correction(ctx, {"label": "Start date month", "nth": 2}) == "July"
    assert audit.correction(ctx, {"label": "Start date month", "nth": 0}) is None   # another entry's month


PAGE = """<form id="f"><label for="c">Country</label>
<select id="c"><option></option><option>China +86</option><option>United States +1</option></select>
<label for="p">Phone</label><input id="p" type="tel" value="(555) 010-0199">
<label for="s">Will you now or in the future require sponsorship?</label>
<select id="s"><option></option><option>Yes</option><option selected>No</option></select></form>"""


def test_final_audit_fixes_the_page_then_passes():
    from playwright.sync_api import sync_playwright
    from jobagent.apply.runner import _final_audit

    class FakeLLM:
        calls = 0

        def json_chat(self, system, user, max_tokens):
            FakeLLM.calls += 1
            assert "United States +1" in user   # the judge only runs once the rules are satisfied
            return {"ok": True, "problems": []}

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(PAGE)
        page.select_option("#c", label="China +86")
        adapter, qa, shots = Adapter(), [], []
        adapter.root = "#f"
        ctx = SimpleNamespace(llm=FakeLLM(), profile=config.profile(), row=ROW, corrections={}, audit_rounds=0,
                              audit_settled={})
        _final_audit(page, adapter, ctx, qa, shots.append)   # returns: passed after the fix
        assert page.eval_on_selector("#c", "e => e.options[e.selectedIndex].text") == "United States +1"
        assert ctx.corrections and FakeLLM.calls == 1 and qa[-1]["value"] == "通过" and not shots

        page.select_option("#c", label="China +86")   # keeps coming back wrong: give up, never submit
        ctx.audit_rounds = audit.MAX_ROUNDS
        try:
            _final_audit(page, adapter, ctx, qa, shots.append)
            raise AssertionError("should not pass")
        except Failed as e:
            assert "提交前总核对没通过" in str(e) and shots == ["before-submit"]
        b.close()


def test_greenhouse_unrecognized_section_blocks_submission(monkeypatch):
    snap = Snapshot([_f(0, "text", "Company name", "Acme Cloud"), _f(1, "text", "Title", "AI Engineer Intern"),
                     _f(2, "text", "Why us?", "")], [], [], False, "", "", "", [])
    assert sections_on_page(snap) == {"work"}
    page = SimpleNamespace(evaluate=lambda js, arg: {"edu": 0, "work": 0})   # the counter sees no blocks at all
    ctx = SimpleNamespace(row={"resume": "SDE"})
    problems = Greenhouse().check(page, snap, ctx)
    assert any("认不出它的结构" in p for p in problems)


def test_judge_cannot_override_fixed_rules_or_guess_fields():
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            assert "Work Experience 1 — Job Title = AI Engineer Intern" in user
            return {"ok": False, "problems": [
                {"item": None, "field": "I have a preferred name", "shown": "No", "expected": "Yes", "fix": "Yes"},
                {"item": None, "field": "Phone Number", "shown": "+86 0555 010 0199", "expected": "+1", "fix": "+1"},
                {"item": None, "field": "Job Title", "shown": "AI Engineer Intern Seattle", "expected": "AI Engineer Intern",
                 "fix": "AI Engineer Intern"},
                {"item": None, "field": "Work Experience 1 — Job Title", "shown": "AI Engineer Intern Seattle",
                 "expected": "AI Engineer Intern", "fix": "AI Engineer Intern"}]}
    qa = [{"label": "I have a preferred name", "kind": "checkbox_single", "value": "不勾选"},
          {"label": "Phone Number", "kind": "tel", "value": "(555) 010-0199"},
          {"label": "Work Experience 1 — Job Title", "kind": "text", "value": "AI Engineer Intern"}]
    profile = config.profile()
    got = {p.field: p.fix for p in audit.judge(FakeLLM(), profile, ROW, [], "Review\\nMy Information ...", qa)}
    assert "I have a preferred name" not in got                 # intentionally unchecked: the judge was wrong
    assert got["Phone Number"] == profile["contact"]["phone"]    # wrong on the page: the rule's value, not the judge's
    assert got["Job Title"] is None                              # which entry? don't guess: stop instead
    assert got["Work Experience 1 — Job Title"] == "AI Engineer Intern"



def test_duplicate_labels_are_told_apart():
    # Nissan (2026-10-31): a fix for "Field of Study" landed in the master's entry instead of the bachelor's
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            assert "- Field of Study #1 = Computer Science" in user and "- Field of Study #2 = Artificial Intelligence" in user
            return {"ok": False, "problems": [
                {"item": None, "field": "Field of Study #2", "shown": "Robotics", "expected": "Artificial Intelligence",
                 "fix": "Artificial Intelligence"},
                {"item": None, "field": "Field of Study", "shown": "x", "expected": "y", "fix": "y"}]}
    qa = [{"label": "Field of Study", "kind": "combobox", "value": "Computer Science", "nth": 0},
          {"label": "Field of Study", "kind": "combobox", "value": "Artificial Intelligence", "nth": 1}]
    got = audit.judge(FakeLLM(), config.profile(), ROW, [], "Review", qa)
    assert [(p.field, p.nth, p.fix) for p in got] == [("Field of Study", 1, "Artificial Intelligence"),
                                                      ("Field of Study", None, None)]   # ambiguous: stop, don't guess


WORKDAY = """<form id="f"><h2>{title}</h2>{body}
<button type="button" onclick="go({back})">Back</button>{next}</form>"""


def test_review_page_fixes_go_back_to_that_page_only():
    """Review shows a wrong job title: go back to My Experience, change only that field, Next back to Review."""
    from playwright.sync_api import sync_playwright
    from jobagent.apply.runner import _fix_on_earlier_pages
    pages = {
        0: WORKDAY.format(title="My Information", back=0, next='<button type="button" data-automation-id="pageFooterNextButton" onclick="go(1)">Save and Continue</button>',
                          body='<label for="n">Legal Name</label><input id="n" value="Alex Rivera">'),
        1: WORKDAY.format(title="My Experience", back=0, next='<button type="button" data-automation-id="pageFooterNextButton" onclick="go(2)">Save and Continue</button>',
                          body='<label for="t">Work Experience 1 — Job Title</label><input id="t" value="AI Engineer Intern Seattle">'
                               '<label for="d">Work Experience 1 — Role Description</label><input id="d" value="untouched">'),
        2: WORKDAY.format(title="Review", back=1, next='<button type="button" data-automation-id="pageFooterNextButton">Submit</button>', body="<p>Review</p>"),
    }
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content("<div id='app'></div>")
        page.evaluate("""(pages) => {
          window.store = {t: 'AI Engineer Intern Seattle'};
          window.go = (n) => { const t = document.getElementById('t'); if (t) store.t = t.value;
            document.getElementById('app').innerHTML = pages[n];
            const t2 = document.getElementById('t'); if (t2) t2.value = store.t; };
          go(2); }""", {str(k): v for k, v in pages.items()})
        from jobagent.apply.adapters import Workday
        adapter = Workday()
        adapter.root = "#f"
        fixed = _fix_on_earlier_pages(page, adapter, [audit.Problem("Work Experience 1 — Job Title", "AI Engineer Intern Seattle",
                                                                    "AI Engineer Intern", "AI Engineer Intern")])
        assert fixed and page.inner_text("h2") == "Review"
        assert page.evaluate("store.t") == "AI Engineer Intern"
        b.close()


def test_prefilled_fields_can_be_fixed_too():
    # Nissan: Workday's parser pre-filled "AI Engineer Intern Seattle"; the planner kept it, so it isn't in qa
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            assert "- Work Experience 1 — Job Title = AI Engineer Intern Seattle" in user
            return {"ok": False, "problems": [{"item": None, "field": "Work Experience 1 — Job Title",
                                               "shown": "AI Engineer Intern Seattle", "expected": "AI Engineer Intern",
                                               "fix": "AI Engineer Intern"}]}
    seen = [{"label": "Work Experience 1 — Job Title", "kind": "text", "nth": 0, "value": "AI Engineer Intern Seattle"}]
    got = audit.judge(FakeLLM(), config.profile(), ROW, [], "Review", [], seen)
    assert [(p.field, p.fix) for p in got] == [("Work Experience 1 — Job Title", "AI Engineer Intern")]


def test_workday_skips_skills_and_steps_are_told_apart():
    from playwright.sync_api import sync_playwright
    from jobagent.apply.answers import _deterministic
    from jobagent.apply.forms import page_signature
    f = {"id": "s", "kind": "combobox", "prompt": True, "label": "Type to Add Skills", "value": "", "required": False}
    assert _deterministic(f, config.profile(), {"resume": "SDE", "ats": "workday"}).value == ""      # left alone
    assert _deterministic({**f, "required": True}, config.profile(), {"resume": "SDE", "ats": "workday"}).value.count(",") == 2
    assert _deterministic(f, config.profile(), {"resume": "SDE", "ats": "greenhouse"}).value.count(",") > 5
    with sync_playwright() as p:  # same URL, same job-title header: only the step heading changes
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content('<h1 data-automation-id="pageHeaderTitleText">Intern</h1><div data-automation-id="applyFlowPage"><h2>My Experience</h2></div>')
        one = page_signature(page)
        page.evaluate("document.querySelector('h2').innerText = 'Application Questions 1 of 2'")
        assert page_signature(page) != one
        b.close()


def test_glued_location_is_cut_from_job_titles():
    from jobagent.apply.answers import _deterministic
    row = {"resume": "AIE", "ats": "workday"}
    f = lambda v: {"id": "t", "kind": "text", "label": "Work Experience 2 — Job Title", "value": v}  # noqa: E731
    assert _deterministic(f("Technology Consultant Trainee Toronto"), config.profile(), row).value == "Technology Consultant Trainee"
    assert _deterministic(f("Technology Consultant Trainee"), config.profile(), row) is None   # already right
    assert _deterministic(f("Senior Engineer Toronto"), config.profile(), row) is None       # not a resume title


def test_judge_sees_options_of_earlier_dropdowns():
    degrees = ["Select One"] + [f"Certificate {i}" for i in range(40)] + ["Bachelor of Engineering", "Bachelor of Science (B.S.)"]
    hint = audit._options_hint("Bachelor of Science (B.S.)", degrees)
    assert "Bachelor of Engineering" in hint and "Certificate 3" not in hint and "Select One" not in hint
    assert audit._options_hint("Yes", ["Select One", "Yes", "No"]) == "   (options: Yes | No)"


def test_education_degree_follows_the_school_shown():
    from jobagent.apply.answers import _education_by_school
    opts = ["Select One", "Bachelor of Science (B.S.)", "Master of Science (M.S.)", "Doctor of Philosophy (Ph.D.)"]
    fields = [  # Workday listed the bachelor's school first and the planner gave it the Master's (Regeneron, 2026-10-31)
        {"id": "s1", "kind": "combobox", "label": "Education 1 — School or University", "value": "University of Toronto"},
        {"id": "d1", "kind": "combobox", "label": "Education 1 — Degree", "value": "Master of Science (M.S.)", "options": opts},
        {"id": "f1", "kind": "combobox", "prompt": True, "label": "Field of Study", "nth": 0, "value": "Computer Science"},
        {"id": "s2", "kind": "combobox", "label": "Education 2 — School or University", "value": "The University of Michigan"},
        {"id": "d2", "kind": "combobox", "label": "Education 2 — Degree", "value": "Master of Science (M.S.)", "options": opts},
        {"id": "f2", "kind": "combobox", "prompt": True, "label": "Field of Study", "nth": 1, "value": "Computer and Information Science"},
    ]
    got = {k: v.value for k, v in _education_by_school(fields, {"resume": "AIE"}).items()}
    assert got == {"d1": "Bachelor of Science (B.S.)"}   # an existing major stays; the master's entry is already right


def test_middle_name_is_cleared():
    from jobagent.apply.answers import _deterministic
    f = {"id": "m", "kind": "text", "label": "Middle Name", "value": "Alex"}
    it = _deterministic(f, config.profile(), ROW)
    assert it.value == "" and it.basis == "clear"
    assert _deterministic({**f, "value": ""}, config.profile(), ROW).basis == "default"

    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            return {"ok": False, "problems": [{"item": 0, "field": "Middle Name", "shown": "Alex", "expected": "", "fix": ""}]}
    got = audit.judge(FakeLLM(), config.profile(), ROW, [f], "", [])
    assert [(p.fix, p.item) for p in got] == [("", 0)]   # clearing is a fix, not "can't tell"


def test_rule_values_are_compared_exactly():
    assert not audit._agrees("Alex Rivera", "Alex Alex Rivera")
    assert audit._agrees("Alex Rivera", "alex  rivera")
    assert audit._agrees("United States +1", "+1") and not audit._agrees("United States +1", "China +86")
    assert audit._agrees("(555) 010-0199", "+1 555 010 0199") and not audit._agrees("(555) 010-0199", "0555 010 0199")


def test_experience_follows_the_company_shown():
    from jobagent.apply.answers import _experience_by_company
    import yaml
    jobs = yaml.safe_load(open(config.ROOT / "config/resumes/SDE.yaml"))["experience"]
    fields = [
        {"id": "c1", "kind": "text", "label": "Work Experience 1 — Company", "value": "ACME CLOUD"},
        {"id": "t1", "kind": "text", "label": "Work Experience 1 — Job Title", "value": "AI Engineer Intern"},
        {"id": "d1", "kind": "textarea", "label": "Work Experience 1 — Role Description",
         "value": "Software Development) Seattle\n• " + jobs[0]["bullets"][0]},
        {"id": "c2", "kind": "text", "label": "Work Experience 2 — Company", "value": "Globex— Advisory"},
        {"id": "t2", "kind": "text", "label": "Work Experience 2 — Job Title", "value": jobs[1]["title"]},
        {"id": "d2", "kind": "textarea", "label": "Work Experience 2 — Role Description",
         "value": "\n".join("• " + b for b in jobs[1]["bullets"])},
    ]
    got = _experience_by_company(fields, {"resume": "SDE"})
    assert set(got) == {"t1", "d1"}                            # Globex's entry is already right
    assert got["t1"].value == jobs[0]["title"] and got["d1"].value.startswith("• " + jobs[0]["bullets"][0][:20])


def test_degree_levels_match_abbreviation_only_lists():
    from jobagent.apply.forms import best_option
    wawa = ["Select One", "Certificate", "DCS", "GED", "HS", "A.A.", "B.A.", "B.S.", "M.A.", "M.S.", "MBA", "JD", "PhD", "No Degree"]
    assert best_option("Bachelor's degree", wawa) == "B.S." and best_option("Master's Degree", wawa) == "M.S."
    assert best_option("Bachelor of Engineering", wawa + ["B.E."]) == "B.E."
    assert best_option("Bachelor's degree", wawa, fuzzy=False) is None   # strict checks stay strict
    assert best_option("Bachelor's degree", ["Masters Degree", "Bachelors Degree"]) == "Bachelors Degree"
    assert best_option("Master of Science", ["Bachelor of Science"]) is None
    assert best_option("Bachelor of Engineering", ["B.S.", "B.A.", "B.E/B.Tech", "M.S."]) == "B.E/B.Tech"   # Elanco


def test_degree_rule_never_picks_another_field():
    from jobagent.apply.answers import _education_by_school
    jj = ["Select One", "Masters Degree or Equivalent", "Master of Science in Medicine or Surgery (MSc, MS, MSurg)",
          "University/Bachelors Degree or Equivalent"]
    fields = [{"id": "s1", "kind": "combobox", "label": "Education 1 — School or University", "value": "University of Michigan"},
              {"id": "d1", "kind": "combobox", "label": "Education 1 — Degree", "value": "Masters Degree or Equivalent", "options": jj}]
    assert _education_by_school(fields, {"resume": "AIE"}) == {}   # already a master's: keep it
    fields[1]["options"] = jj + ["Master of Science (M.S.)"]
    assert _education_by_school(fields, {"resume": "AIE"})["d1"].value == "Master of Science (M.S.)"


def test_judge_never_rewrites_application_questions():
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            return {"ok": False, "problems": [
                {"item": None, "field": "Master Required", "shown": "Master", "expected": "Bachelor's", "fix": "Bachelor's degree"},
                {"item": None, "field": "Education 2 — Degree", "shown": "B.S.", "expected": "B.E/B.Tech", "fix": "B.E/B.Tech"}]}
    seen = [{"label": "Master Required", "kind": "combobox", "nth": 0, "value": "Master"},
            {"label": "Education 2 — Degree", "kind": "combobox", "nth": 0, "value": "B.S."}]
    got = {p.field: p.fix for p in audit.judge(FakeLLM(), config.profile(), ROW, [], "Review", [], seen)}
    assert got == {"Master Required": None, "Education 2 — Degree": "B.E/B.Tech"}   # question: stop, don't overwrite


def test_your_education_headings_are_entries():
    # RBC: "Your Education 1" / "Your Work Experience 2" headings
    from playwright.sync_api import sync_playwright
    from jobagent.apply import forms
    from jobagent.apply.adapters import _ENTRIES_JS
    html = """<form id="f"><h3>Your Education</h3><div><h4>Your Education 1</h4>
      <label for="s">School or University</label><input id="s" value="The University of Michigan"></div>
      <div><h4>Your Education 2</h4><label for="t">School or University</label><input id="t" value="UofT">
      <button type="button">Delete</button></div><button type="button">Add Another</button></form>"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(html)
        labels = [f["label"] for f in forms.snapshot(page, "#f").fields]
        assert labels == ["Education 1 — School or University", "Education 2 — School or University"]
        assert page.evaluate(_ENTRIES_JS, None) == {"edu": 1}   # one Delete button: entry 1 has none
        b.close()


def test_listed_countries_and_visa_status_rules():
    from jobagent.apply.answers import _deterministic
    hp = ("In order to comply with the U.S. Export Control regulations ... Please indicate if you are a citizen or permanent "
          "resident of ONE OR MORE of the countries listed below, by answering 'yes'. The countries are: Armenia, China "
          "(People's Republic of China), Cuba, Iran.")
    f = {"id": "x", "kind": "select", "label": hp, "options": ["Select One", "Yes", "No"], "value": ""}
    assert _deterministic(f, config.profile(), ROW).value == "Yes"
    f = {"id": "x", "kind": "combobox", "label": hp, "options": [], "value": "No"}   # pre-filled: options not read
    assert _deterministic(f, config.profile(), ROW).value == "Yes"
    f = {"id": "v", "kind": "select", "label": "Please indicate your current US employment visa status",
         "options": ["Select One", "F-1 OPT", "F-1 CPT", "F-1 Student", "H-1B"], "value": ""}
    assert _deterministic(f, config.profile(), ROW).value == "F-1 Student"
    f["options"] = ["Not Applicable (I am a US Citizen/Lawful Permanent Resident)", "F-1 OPT", "F-1 CPT", "H-1B"]
    assert _deterministic(f, config.profile(), ROW).value == "F-1 CPT"   # RBC: no plain F-1 → CPT (the user)
    f["options"] = ["F-1 OPT", "H-1B"]
    assert _deterministic(f, config.profile(), ROW) is None   # neither: the planner decides


def test_parser_junk_in_address_line_2_is_cleared():
    from jobagent.apply.answers import _deterministic
    it = _deterministic({"id": "a", "kind": "text", "label": "Address Line 2", "value": "0705"}, config.profile(), ROW)
    assert it.value == "" and it.basis == "clear"


def test_prefilled_workday_dropdown_keeps_its_question():
    from playwright.sync_api import sync_playwright
    from jobagent.apply import forms
    html = """<form id="f"><div><label id="q">Please indicate your current US employment visa status</label>
      <div><button type="button" aria-haspopup="listbox" aria-label="F-1 OPT Required">F-1 OPT</button></div></div>
      <div><label id="q2">Are you 18 or older?</label>
      <div><button type="button" aria-haspopup="listbox" aria-label="Select One Required">Select One</button></div></div></form>"""
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content(html)
        labels = [f["label"] for f in forms.snapshot(page, "#f").fields]
        assert labels == ["Please indicate your current US employment visa status", "Are you 18 or older?"]
        b.close()


def test_closest_major_is_not_a_problem():
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            return {"ok": False, "problems": [
                {"item": None, "field": "Field of Study #1", "shown": "Computer Science and Mathematics",
                 "expected": "Computer Science", "fix": "Computer Science"},
                {"item": None, "field": "Field of Study #2", "shown": "Mechanical Engineering",
                 "expected": "Artificial Intelligence", "fix": "Artificial Intelligence"}]}
    seen = [{"label": "Field of Study", "kind": "combobox", "nth": 0, "value": "Computer Science and Mathematics"},
            {"label": "Field of Study", "kind": "combobox", "nth": 1, "value": "Mechanical Engineering"}]
    got = audit.judge(FakeLLM(), config.profile(), ROW, [], "Review", [], seen)
    assert [(p.nth, p.fix) for p in got] == [(1, "Artificial Intelligence")]   # only the really wrong one


def test_degree_of_another_field_is_replaced():
    from jobagent.apply.answers import _education_by_school
    opts = ["Select One", "Associate of Arts (AA)", "Bachelor of Arts (BA)", "Bachelor of Science (BS)",
            "Master of Arts (MA)", "Master of Science (MS)", "MBA"]
    fields = [  # Jain Global (2026-11-01): the B.Eng. stayed "Bachelor of Arts (BA)" because it is a bachelor's
        {"id": "s1", "kind": "combobox", "label": "Education 1 — School or University", "value": "University of Toronto"},
        {"id": "d1", "kind": "combobox", "label": "Education 1 — Degree", "value": "Bachelor of Arts (BA)", "options": opts},
        {"id": "s2", "kind": "combobox", "label": "Education 2 — School or University", "value": "University of Michigan"},
        {"id": "d2", "kind": "combobox", "label": "Education 2 — Degree", "value": "MBA", "options": opts},
    ]
    got = {k: v.value for k, v in _education_by_school(fields, {"resume": "AIE"}).items()}
    assert got == {"d1": "Bachelor of Science (BS)", "d2": "Master of Science (MS)"}
    expedia = ["Select One", "Associates", "Bachelors", "MBA", "Masters", "PhD"]
    fields = [fields[2], {**fields[3], "value": "MBA", "options": expedia}]
    assert _education_by_school(fields, {"resume": "AIE"})["d2"].value == "Masters"   # not MBA again


def test_synonyms_inside_longer_options():
    from jobagent.apply.forms import best_option
    assert best_option("Mobile", ["Select One", "Personal Cell", "Personal Landline"]) == "Personal Cell"   # Nelnet
    assert best_option("United States", ["Canada", "Other", "USA"]) == "USA"                               # RBC
    assert best_option("United States of America", ["Canada", "United States", "Other"]) == "United States"


def test_judge_leaves_the_profile_gender_alone():
    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            return {"ok": False, "problems": [{"item": 0, "field": "How would you describe your gender identity?",
                                               "shown": "Man", "expected": "I don't wish to answer", "fix": None}]}
    f = {"id": "g", "kind": "select", "label": "How would you describe your gender identity?", "value": "Man",
         "options": ["Man", "Woman", "I don't wish to answer"]}
    assert audit.judge(FakeLLM(), config.profile(), ROW, [f], "", []) == []   # F&G, 2026-11-01


def test_judge_retries_a_cut_off_reply():
    calls = []

    class FakeLLM:
        def json_chat(self, system, user, max_tokens):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("LLM 没有返回合法 JSON：'{\"'")
            return {"ok": True, "problems": []}
    assert audit.judge(FakeLLM(), config.profile(), ROW, [], "", []) == [] and len(calls) == 2   # Dee Zee


def test_degree_prefers_the_general_option_of_the_field():
    from jobagent.apply.answers import _education_by_school, _degree_fit
    kla = ["Select One", "Bachelors of Arts or Science (Bachelors)", "Masters of Technology (Masters)",
           "Masters of Arts or Science or Business Administration (Masters)", "Doctorate (PhD)"]
    fields = [{"id": "s2", "kind": "combobox", "label": "Education 2 — School or University", "value": "The University of Michigan"},
              {"id": "d2", "kind": "combobox", "label": "Education 2 — Degree", "value": "Masters of Technology (Masters)",
               "options": kla}]
    got = _education_by_school(fields, {"resume": "AIE"})   # KLA, 2026-11-01: the review kept the parsed M.Tech.
    assert got["d2"].value == "Masters of Arts or Science or Business Administration (Masters)"
    assert _degree_fit("MASTER - IN PROGRESS", "master") == 3 and _degree_fit("Master of Science in Medicine or Surgery", "master") == 0
    assert _degree_fit("Bachelor of Arts (BA)", "bachelor") == 0 and _degree_fit("B.S.", "bachelor") == 3


def test_degree_lists_with_bracketed_abbreviations_and_general_options():
    from jobagent.apply.forms import best_option
    reg = ["Select One", "Certificate: Diploma (Japan)", "High School (HS)", "Associate of Arts (A.A.)",
           "Bachelor of Science (B.S.)", "Bachelor of Arts (B.A.)", "Master of Business Administration (MBA)",
           "Master of Science (M.S.)", "Master of Health Administration (MHA)"]          # Regeneron, 2026-11-01
    assert best_option("Bachelor's degree", reg) == "Bachelor of Science (B.S.)"
    assert best_option("Bachelor of Engineering", reg) == "Bachelor of Science (B.S.)"
    jj = ["Select One", "Chartered Management Accountant", "MD", "Masters Degree or Equivalent",
          "Master of Science in Medicine or Surgery (MSc, MS, MSurg)", "University/Bachelors Degree or Equivalent"]
    assert best_option("Bachelor's degree", jj) == "University/Bachelors Degree or Equivalent"   # J&J C&SP
    assert best_option("Master's Degree", jj) == "Masters Degree or Equivalent"
    assert best_option("Master of Science", jj) == "Masters Degree or Equivalent"   # never the medicine one
