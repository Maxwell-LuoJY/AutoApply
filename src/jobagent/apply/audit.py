"""Final check before Submit: read back what the page actually displays and compare it with the applicant's facts.

Two layers, both must pass:
- rules: values the program knows for certain (name, email, phone digits and its +1 country code, LinkedIn / GitHub,
  the work-authorization yes / no answers) are compared directly with what the page shows;
- an LLM judge reads every displayed value (or, on a review page, the page text) next to the facts and the resume and
  lists contradictions, each with the value that should be there.
A problem that names a field and a fix is corrected and the page is checked again; anything else stops the submission.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from . import answers, forms
from .forms import CHOICE_KINDS, best_option, norm

log = logging.getLogger("jobagent")

MAX_ROUNDS = 2        # rounds of corrections before giving up
_REVIEW_FIELDS = 3    # a final page with fewer form fields than this is a review page: judge its text instead


@dataclass
class Problem:
    field: str                 # label on this page, or the label it was filled under on an earlier page
    shown: str
    expected: str
    fix: str | None = None     # value to enter instead; None: can't be corrected automatically
    item: int | None = None    # index into this page's readback items
    nth: int | None = None
    source: str = "rule"       # rule | llm

    def text(self) -> str:
        return f"{self.field[:70]}：页面上是「{self.shown or '空'}」，应为「{self.expected}」"


def shown(f: dict) -> str:
    v = f.get("value")
    if isinstance(v, bool):
        return "checked" if v else "unchecked"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v or "").strip()


def readback(page: Page, adapter, snap: forms.Snapshot) -> tuple[list[dict], str]:
    """The form fields on this page with what they display; on a review page also the page text."""
    items = [f for f in snap.fields if adapter.relevant(f) and f["kind"] != "file"]
    text = ""
    if len(items) < _REVIEW_FIELDS:
        try:
            text = page.locator("body").inner_text()
        except PlaywrightError:
            text = snap.text
    return items, text[:16000]


# ---------------------------------------------------------------- rules

_PHONE = re.compile(r"^(mobile |cell |primary |home )?(phone|mobile|cell)( number)?$")
_DIAL = re.compile(r"\+\s?(\d{1,4})\b")


def _is_us(value: str) -> bool:
    if (m := _DIAL.search(value)):
        return m.group(1) == "1" and not re.search(r"(?i)canada|outlying|puerto|guam|virgin|samoa|marianas", value)
    return bool(re.search(r"(?i)\b(united states|usa|u\.s\.a?\.?|us)\b", value))


def rule_problems(items: list[dict], profile: dict, row) -> list[Problem]:
    out: list[Problem] = []
    phone = re.sub(r"\D", "", profile["contact"]["phone"])
    for i, f in enumerate(items):
        value, label = shown(f), norm(f.get("label", ""))
        if not value:
            continue  # blanks: required ones are caught by missing_required
        mk = lambda expected, fix: Problem(f.get("label", ""), value, expected, fix, i, f.get("nth"))  # noqa: E731
        if f["kind"] in CHOICE_KINDS and answers._COUNTRY.search(label):
            if answers.is_us(profile["contact"]["address"]["country"]) and not _is_us(value):
                want = answers._deterministic(f, profile, row)
                out.append(mk("United States +1", want.value if want else "United States +1"))
            continue
        if _PHONE.search(label):
            digits = re.sub(r"\D", "", value)
            if not digits.endswith(phone) or digits[: len(digits) - len(phone)] not in ("", "1"):
                out.append(mk(profile["contact"]["phone"], profile["contact"]["phone"]))
            continue
        if f["kind"] in answers._TEXTUAL and (want := answers._deterministic(f, profile, row)) is not None \
                and isinstance(want.value, str) and want.value and norm(want.value) != norm(value):
            out.append(mk(want.value, want.value))
            continue
        if (conflict := answers._sensitive_conflict(f, value, profile)):
            out.append(mk("与工作授权资料一致的答案", None))  # the planner never picks these: something else changed it
            log.warning("   总核对：工作授权类答案和资料不一致：%s", conflict)
    return out


# ---------------------------------------------------------------- LLM judge

JUDGE = """You are the last check before a job application is submitted on the applicant's behalf.
Compare what the application form DISPLAYS with the applicant's facts and resume, and report every value that is wrong
for this applicant.

Applicant facts:
{facts}

Fixed answers the applicant chose for common questions:
{extra}

Resume (the applicant's real history):
{resume}

Report as problems:
- a wrong name, email, phone number or phone country code (must match the applicant's country), address, LinkedIn or GitHub;
- a school, degree, major, company, job title, location or date that differs from the resume (month / year must match);
  a degree option naming another field ("Master of Science in Medicine or Surgery" for an M.S. in Computer Science) is
  wrong when a general one ("Masters Degree or Equivalent", "Master of Science") is offered;
- a work-authorization, sponsorship, citizenship, veteran, disability or other EEO answer that contradicts the facts;
- any other answer that is factually false about the applicant.
NOT problems: formatting differences (phone punctuation, date formats, "The University of X" vs "University of X", a
company with or without its department); a choice field holding the closest available option when the exact fact is not
among its options (e.g. graduation "May/June" when the exact month isn't offered, major "Computer Science" when
"Artificial Intelligence" isn't offered); any "how did you hear about us" answer; optional fields left blank; a GPA left
blank where the facts say it stays blank (intentional); "I have a preferred name" left unchecked / No (intentional: the
legal name is used); a school shown as "Other" (the employer's list doesn't have it); skills lists (not checked); an
empty optional Websites, Languages or Skills section ("No Response"); the wording of essays and free-text answers;
anything consistent with the facts.{middle}
When unsure, it is not a problem.

Reply with JSON only:
{{"ok": true or false, "problems": [{{"item": <the [number] of the field, or null>, "field": "<label exactly as listed>",
  "shown": "<what the form shows>", "expected": "<what it should be>",
  "fix": "<value to enter instead; for a choice field one of its listed options; null if you can't tell>"}}]}}"""


def _resume_summary(row) -> str:
    from ..resume import structured
    data = structured(row["resume"] if row is not None and "resume" in row.keys() else None) or {}
    lines = [f"- Experience: {e.get('company')} | {e.get('title')} | {e.get('location', '')} | {e.get('start')} to {e.get('end')}"
             for e in data.get("experience") or []]
    lines += [f"- Education: {e.get('school')} | {e.get('degree')} | major {e.get('field', '')} | {e.get('start')} to "
              f"{e.get('end')} | GPA {e.get('gpa') or '-'}" for e in data.get("education") or []]
    if data.get("skills"):
        lines.append("- Skills: " + "; ".join(f"{k}: {', '.join(map(str, v or []))}" for k, v in data["skills"].items()))
    return "\n".join(lines) or "(no structured resume)"


def _field_line(i: int, f: dict) -> str:
    req = ", required" if f.get("required") else ""
    line = f"[{i}] {f.get('label') or '(no label)'} ({f['kind']}{req}) = {shown(f) or '(blank)'}"
    opts = [o for o in f.get("options") or [] if o]
    if f["kind"] in CHOICE_KINDS and opts:
        more = f" … {len(opts) - 25} more" if len(opts) > 25 else ""
        line += "\n     options: " + " | ".join(o[:60] for o in opts[:25]) + more
    return line


def judge(llm, profile: dict, row, items: list[dict], text: str, qa: list[dict], seen: list[dict] | None = None) -> list[Problem]:
    """`qa`: what was entered; `seen`: what every field on earlier pages showed when we left it (pre-filled ones too)."""
    qa = qa + (seen or [])
    extra = "\n".join(f"- question matching /{m}/ → {a}" for m, a in answers.extra_answers()) or "(none)"
    middle = "" if profile["name"].get("middle") else \
        " The applicant has no middle name: a Middle Name field must be blank (fix \"\" to clear it)."
    system = JUDGE.format(facts=answers.facts_text(profile), extra=extra, resume=_resume_summary(row), middle=middle)
    parts = [f"Job: {row['company']} — {row['title']}" if row is not None else ""]
    if items:
        parts.append("Form fields on this page ([number] label (kind) = displayed value):\n"
                     + "\n".join(_field_line(i, f) for i, f in enumerate(items)))
    if text:
        parts.append("This is a review page. Its text shows the values entered on earlier pages:\n<page>\n" + text + "\n</page>")
        if (earlier := _earlier(qa)):
            parts.append("Fields on earlier pages, as \"label = value\". For a problem on the review page, set \"field\" "
                         "to one of these labels EXACTLY (e.g. \"Work Experience 2 — Job Title\", not \"Job Title\") and \"item\": null:\n"
                         + "\n".join(f"- {name} = {value}{_options_hint(value, opts)}"
                                     for name, (_, _, value, opts) in earlier.items()))
    # reasoning models spend most of the budget thinking: 6000 ran out on a 30-field Greenhouse form
    try:
        data = llm.json_chat(system, "\n\n".join(p for p in parts if p), max_tokens=16000)
    except ValueError:  # a reply cut off after '{"' (Dee Zee): one more try before giving up on this job
        data = llm.json_chat(system, "\n\n".join(p for p in parts if p), max_tokens=16000)
    if data.get("ok") is False and not data.get("problems"):  # a verdict with nothing to act on: ask once more
        data = llm.json_chat(system, "\n\n".join(p for p in parts if p) + "\n\nList every problem you found, or reply ok: true.",
                             max_tokens=16000)
    out, earlier = [], _earlier(qa) if text else {}
    for p in data.get("problems") or []:
        if not isinstance(p, dict) or not p.get("field") and p.get("item") is None:
            continue
        item = p.get("item") if isinstance(p.get("item"), int) and 0 <= p["item"] < len(items) else None
        label, nth = (items[item].get("label", ""), items[item].get("nth")) if item is not None else (str(p.get("field")), None)
        known = item is not None or not text or label in earlier
        if item is None and label in earlier:
            label, nth, _, _ = earlier[label]
        fix = p.get("fix")
        f = items[item] if item is not None else {"kind": _kind(qa, label), "label": label, "options": []}
        clear = fix == "" and f["kind"] in forms.TEXT_KINDS  # e.g. a middle name the site's parser invented
        fix = str(fix) if fix not in (None, "", "null") else ("" if clear else None)
        if (rule := answers._deterministic(f, profile, row)) is not None:
            # set by a fixed rule (name, contact, preferred-name box…): the rule decides, not the judge
            if _agrees(rule.value, str(p.get("shown") or "") if item is None else shown(f)):
                continue
            fix = "Yes" if rule.value is True else "No" if rule.value is False else str(rule.value)
        shown_now = str(p.get("shown") or "") if item is None else shown(f)
        if _eeo_agrees(label, shown_now, profile):
            continue  # F&G: "Man" is the profile's gender; the judge mixed it up with the transgender question
        words = {w for w in norm(fix or "").split() if len(w) > 3}
        if words and re.search(r"field of study|major|discipline", norm(label)) and words <= set(norm(shown_now).split()):
            continue  # "Artificial Intelligence and Robotics" already is the closest the list offers
        if not known:
            fix = None  # can't tell which earlier field it means: don't guess, stop instead
        elif fix is not None and not _RESUME_OR_CONTACT.search(norm(label)):
            # an application question: the planner already answered it from the same facts, so the judge disagreeing
            # is a judgement call for the user, not something to overwrite (Elanco: "Master Required" → "Bachelor's")
            fix = None
        out.append(Problem(label, str(p.get("shown") or ""), str(p.get("expected") or ""), fix, item, nth, "llm"))
    if not out and data.get("ok") is False and not data.get("problems"):  # listed nothing at all (not: all dismissed)
        log.info("   总核对 LLM 原始回复：%s", json.dumps(data, ensure_ascii=False)[:600])
        out.append(Problem("（总核对）", "", "LLM 判定不通过但没有列出具体问题", None, source="llm"))
    return out


# fields that copy the resume or the contact details: the judge's correction can be checked against them, so it is applied
_RESUME_OR_CONTACT = re.compile(
    r"^(work experience|education) \d+ |^field of study$|^(legal |first |last |middle |preferred |full )?name$|"
    r"^(first|last|middle|given|family) name|^e ?mail|phone|^address|^city$|^state|^postal|^zip|^country|^county$|linkedin|github")


def _eeo_agrees(label: str, value: str, profile: dict) -> bool:
    """Gender / race questions whose displayed answer is the profile's own: not the judge's call."""
    eeo, label, value = profile.get("eeo") or {}, norm(label), norm(value)
    if re.search(r"\bgender\b", label) and not re.search(r"transgender|non binary|these words|orientation|pronoun", label):
        return norm(str(eeo.get("gender", ""))) == "male" and value in ("male", "man", "m", "cisgender man", "cis man")
    if re.search(r"\b(race|ethnicity|racial)\b", label) and not re.search(r"hispanic|latin", label):
        return norm(str(eeo.get("race", ""))) == "asian" and "asian" in value.split() and "hispanic or latino" not in value.replace("not hispanic or latino", "")
    return False


def _agrees(want, value: str) -> bool:
    """Does what the page shows match the value a fixed rule sets?"""
    if isinstance(want, bool):
        return (norm(value) in ("yes", "checked", "true")) == want
    want, digits = str(want), re.sub(r"\D", "", str(want))
    if len(digits) >= 7:  # a phone number: same digits, at most a leading 1
        got = re.sub(r"\D", "", value)
        return got.endswith(digits) and got[: len(got) - len(digits)] in ("", "1")
    if re.search(r"(?i)^united states", want):
        return _is_us(value)
    # exact only: "Alex Rivera" is not "Alex Alex Rivera" (a parser-filled middle name, Workday Inc.)
    return norm(want) == norm(value)


def _earlier(qa: list[dict]) -> dict[str, tuple[str, int | None, str]]:
    """Fields filled on earlier pages: name shown to the judge → (label, nth, value entered). A label used by several
    fields ("Field of Study" in each education entry) is told apart as "Field of Study #1", "#2"."""
    by_label: dict[str, dict[int, str]] = {}
    options: dict[tuple, list] = {}
    for q in qa:
        if q.get("kind") not in ("file", "audit") and q.get("label"):
            by_label.setdefault(q["label"], {})[q.get("nth", 0)] = str(q.get("value"))[:120]
            if q.get("options"):
                options[(q["label"], q.get("nth", 0))] = q["options"]
    out = {}
    for label, values in by_label.items():
        for nth, value in sorted(values.items()):
            out[label if len(values) == 1 else f"{label} #{nth + 1}"] = (label, nth if len(values) > 1 else None, value,
                                                                        options.get((label, nth), []))
    return out


def _options_hint(value: str, options: list[str]) -> str:
    """A choice field's options for the judge: all of a short list, else the ones sharing a word with the value."""
    opts = [o for o in options if o and not re.match(r"(?i)^select( one)?$", o)]
    if len(opts) > 30:
        words = {w for w in norm(value).split() if len(w) >= 4}
        opts = [o for o in opts if words & set(norm(o).split())][:30]
    return f"   (options: {' | '.join(o[:60] for o in opts)})" if opts else ""


def _kind(qa: list[dict], label: str) -> str:
    return next((q["kind"] for q in reversed(qa) if q.get("label") == label), "text")


def run(page: Page, adapter, snap: forms.Snapshot, ctx, qa: list[dict]) -> tuple[list[dict], list[Problem]]:
    """(this page's readback items, problems found). Rules first: a rule problem is certain, the LLM isn't needed."""
    items, text = readback(page, adapter, snap)
    problems = rule_problems(items, ctx.profile, ctx.row)
    if not problems:
        problems = judge(ctx.llm, ctx.profile, ctx.row, items, text, qa, getattr(ctx, "seen", None))
    qa.append({"label": "提交前总核对", "kind": "audit", "basis": "audit", "error": None,
               "value": "通过" if not problems else "；".join(p.text() for p in problems)})
    return items, problems


def remember(ctx, problems: list[Problem]) -> None:
    """Corrections that later refills of the same field must use (a Workday page filled again after going back)."""
    for p in problems:
        if p.fix is not None:
            ctx.corrections[(norm(p.field), p.nth)] = p.fix


def correction(ctx, f: dict):
    c = getattr(ctx, "corrections", None) or {}
    key = norm(f.get("label", ""))
    if (key, f.get("nth")) in c:
        return c[(key, f.get("nth"))]
    return c.get((key, None))


def fix_here(page: Page, items: list[dict], problems: list[Problem]) -> bool:
    """Re-enter the fixes for problems on this page's own fields. True if every problem here could be fixed."""
    ok = True
    for p in problems:
        if p.fix is None or p.item is None:
            ok = False
            continue
        f = items[p.item]
        err = forms.fill_field(page, f, p.fix, lambda v: None)
        if f["kind"] == "combobox":
            page.keyboard.press("Escape")
        if err:
            log.info("   总核对修改失败：%s → %s（%s）", f.get("label"), p.fix, err)
            ok = False
    page.wait_for_timeout(800)
    return ok


def describe(problems: list[Problem]) -> str:
    return "；".join(p.text() for p in problems[:5]) + (f" 等 {len(problems)} 处" if len(problems) > 5 else "")


def as_json(problems: list[Problem]) -> str:
    return json.dumps([p.__dict__ for p in problems], ensure_ascii=False)
