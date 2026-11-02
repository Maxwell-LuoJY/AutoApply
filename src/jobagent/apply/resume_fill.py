"""Fill resume-copy fields (work experience, education, skills) straight from the structured resume (jobagent.resume).

Entries are matched by position: the n-th "Job Title" box on the page is the n-th job on the resume, and a picker
labelled "Work Experience 2 — From" is the second job's start date. Anything this can't place stays with the LLM.
"""
from __future__ import annotations

import re

from .forms import CHOICE_KINDS, best_option, norm

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]
_ENTRY = re.compile(r"^(work experience|experience|employment|education)\s*(\d+)\s*(.*)$")

_WORK = {
    "title": re.compile(r"^(job )?title$|^position( title)?$|^role$"),
    "company": re.compile(r"^company( name)?$|^employer( name)?$|^organization$"),
    "location": re.compile(r"^location$|^city$"),
    "current": re.compile(r"^i currently work here$|^current(ly)? (employer|job|position)$"),
    "description": re.compile(r"^(role )?description$|^responsibilities$|^summary$"),
    "start": re.compile(r"^(from|start date|start)$"),
    "end": re.compile(r"^(to|end date|end)( actual or expected)?$"),
}
_EDU = {
    "school": re.compile(r"^(school|university|school or university|institution|college)( name)?$"),
    "degree": re.compile(r"^degree( type| level| earned)?$"),
    "field": re.compile(r"^(field of study|major|discipline|area of study|program)$"),
    "gpa": re.compile(r"^(overall result( gpa)?|gpa|cumulative gpa|grade point average)$"),
    "start": re.compile(r"^(from|start date|start|start year)$"),
    "end": re.compile(r"^(to|end date|end|end year|graduation date)( actual or expected)?$"),
    "start_month": re.compile(r"^start date month$"),
    "start_year": re.compile(r"^start date year$"),
    "end_month": re.compile(r"^end date month$"),
    "end_year": re.compile(r"^end date year$"),
}
_SKILLS = re.compile(r"skill")
_DEGREE_WORDS = {
    "master": ["Master of Science", "Master's Degree", "Master's", "Masters", "Master", "M.S.", "MS"],
    "bachelor": ["Bachelor of Engineering", "Bachelor's Degree", "Bachelor's", "Bachelors", "Bachelor", "B.Eng.", "B.E.",
                 "B.S.", "BS"],   # some lists use abbreviations only (Wawa: A.A. / B.S. / M.S.)
    "phd": ["Doctor of Philosophy", "Doctorate", "PhD", "Ph.D."],
}


# a degree of the right level that names another field: not the B.Eng. / M.S. (Jain Global: "Bachelor of Arts (BA)";
# Expedia: "MBA")
OTHER_FIELD = re.compile(r"\b(arts?|ba|mba|ma|business|administration|medicine|surgery|laws?|llb|llm|education|fine|"
                         r"music|nursing|divinity|social work|public health)\b")


def _ym(v: str) -> tuple[int, int] | None:
    m = re.match(r"^(\d{4})-(\d{2})$", str(v))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _choice(f: dict, candidates: list[str]) -> str | None:
    """First candidate that one of the field's options matches; for option-less boxes, the first candidate."""
    opts = f.get("options") or []
    if f["kind"] in CHOICE_KINDS and opts:
        # a close match for any candidate beats a loose one for an earlier candidate: "Masters" is in the list, so
        # "Master of Science" must not first fall through to a fuzzy pick (Expedia: MBA)
        return next((o for o in (best_option(c, opts, fuzzy=False) for c in candidates) if o), None) or \
            next((o for o in (best_option(c, opts) for c in candidates) if o), None)
    return candidates[0] if candidates else None


def top_skills(data: dict, n: int = 5) -> list[str]:
    groups = list((data.get("skills") or {}).items())
    groups.sort(key=lambda kv: "language" not in kv[0].lower())  # programming languages first
    out: list[str] = []
    for i, (_, items) in enumerate(groups):
        for s in (items or [])[: 3 if i == 0 else 2]:
            s = re.sub(r"\s*\(.*?\)", "", str(s)).strip()
            if s and s not in out:
                out.append(s)
    return out[:n]


def resolve(fields: list[dict], data: dict) -> dict[str, object]:
    """field id → value for every field this module can answer from the structured resume."""
    labels = [norm(f.get("label", "")) for f in fields]
    work_page = any(_WORK["title"].match(l) for l in labels) and any(_WORK["company"].match(l) for l in labels)
    edu_page = any(_EDU["school"].match(_ENTRY.sub(r"\3", l)) for l in labels)
    jobs, schools = data.get("experience") or [], data.get("education") or []
    out: dict[str, object] = {}
    for f, label in zip(fields, labels):
        section, idx, base = None, f.get("nth", 0), label
        if (m := _ENTRY.match(label)):  # "Work Experience 2 — From": the entry is in the label
            section = "edu" if m.group(1) == "education" else "work"
            idx, base = int(m.group(2)) - 1, m.group(3).strip()
        if _SKILLS.search(label) and f["kind"] == "combobox":
            if (skills := top_skills(data)):
                out[f["id"]] = ", ".join(skills)
            continue
        if section != "edu" and work_page and any(p.match(base) for p in _WORK.values()):
            if section is None and edu_page and (_WORK["start"].match(base) or _WORK["end"].match(base)):
                continue  # a bare "From" on a page with both sections: can't tell which entry it is
            if idx < len(jobs) and (v := _work_value(f, base, jobs[idx])) is not None:
                out[f["id"]] = v
        elif section != "work" and (edu_page or section == "edu") and any(p.match(base) for p in _EDU.values()):
            if idx < len(schools) and (v := _edu_value(f, base, schools[idx])) is not None:
                out[f["id"]] = v
    return out


def _work_value(f: dict, base: str, job: dict):
    if _WORK["title"].match(base):
        return job.get("title")
    if _WORK["company"].match(base):
        return job.get("company")
    if _WORK["location"].match(base):
        return job.get("location") or None
    if _WORK["current"].match(base):
        return job.get("end") == "present"
    if _WORK["description"].match(base):
        text = "\n".join(f"• {b}" for b in job.get("bullets") or [])
        return text[: f["maxlength"]] if f.get("maxlength") else text
    if f["kind"] != "date_parts":
        return None  # free-text date boxes have site-specific formats: leave them to the LLM
    if _WORK["start"].match(base):
        return job.get("start")
    if _WORK["end"].match(base):
        return None if job.get("end") == "present" else job.get("end")
    return None


def _edu_value(f: dict, base: str, school: dict):
    start, end = _ym(school.get("start", "")), _ym(school.get("end", ""))
    if _EDU["school"].match(base):
        return school.get("school")
    if _EDU["degree"].match(base):
        same_field = {**f, "options": [o for o in f.get("options") or [] if not OTHER_FIELD.search(norm(o))]}
        return _choice(same_field, _DEGREE_WORDS.get(school.get("level"), []) + [school.get("degree", "")])
    if _EDU["field"].match(base):
        return _choice(f, [school.get("field", "")]) if school.get("field") else None
    if _EDU["gpa"].match(base):
        return re.sub(r"\s*/.*$", "", school.get("gpa") or "") or None   # "3.70/4.00" → "3.70"
    if _EDU["start_month"].match(base) and start:
        return _choice(f, [_MONTHS[start[1] - 1], f"{start[1]:02d}", str(start[1])])
    if _EDU["start_year"].match(base) and start:
        return _choice(f, [str(start[0])])
    if _EDU["end_month"].match(base) and end:
        return _choice(f, [_MONTHS[end[1] - 1], f"{end[1]:02d}", str(end[1])])
    if _EDU["end_year"].match(base) and end:
        return _choice(f, [str(end[0])])
    if _EDU["start"].match(base) and start:
        return school.get("start") if f["kind"] == "date_parts" else None
    if _EDU["end"].match(base) and end:
        return school.get("end") if f["kind"] == "date_parts" else None
    return None
