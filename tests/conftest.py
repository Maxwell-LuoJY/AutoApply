"""Tests run against a fictional applicant in a temporary JOBAGENT_HOME, never against your own config/ or data/.

Set up at import time, before any test module imports jobagent (config.ROOT is read once, on import).
"""
import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="jobagent-test-home-"))

PROFILE = """
name: {first: Alex, last: Rivera, middle: "", preferred: Al}
contact:
  email: alex.rivera@example.com
  phone: "(555) 010-0199"
  school_email: arivera@umich.example.edu
  address: {street: 123 Main St, city: Ann Arbor, county: Washtenaw, state: Michigan, zip: "48104", country: United States}
links: {linkedin: https://www.linkedin.com/in/alex-rivera-example, github: https://github.com/alex-rivera-example}
education:
  - {school: University of Michigan, aliases: [UMich], degree: M.S. in Computer Science, level: master,
     start: "2026-09", end: "2028-05", expected: true, gpa: null}
  - {school: University of Toronto, aliases: [UofT], degree: B.Eng. in Computer Engineering, level: bachelor,
     start: "2022-09", end: "2026-06", expected: false, gpa: "3.70/4.00", honors: Dean's List}
availability: Summer 2027 (May/June 2027)
work_authorization:
  authorized_to_work_in_us: true
  requires_sponsorship_now_or_future: false
  us_citizen_or_permanent_resident: false
  permanent_or_indefinite: false
  visa_status: F-1 student
  green_card: none
  citizenship: China
eeo: {gender: Male, race: Decline to answer, hispanic_or_latino: Decline to answer,
      disability: Decline to answer, veteran: "I am not a veteran (have never served in the military)",
      sexual_orientation: Decline to answer, other_voluntary: Decline to answer}
defaults: {how_did_you_hear: Online job board / GitHub, willing_to_relocate: "Yes", previously_worked_here: "No",
           salary_expectation: Negotiable, cover_letter: skip_job_if_required, open_ended_required: skip_job,
           transcript: upload_only_if_required}
resumes:
  SDE: {pdf: materials/Resume_Alex_Rivera_SDE.pdf, docx: materials/Resume_Alex_Rivera_SDE.docx}
  AIE: {pdf: materials/Resume_Alex_Rivera_AIE.pdf, docx: materials/Resume_Alex_Rivera_AIE.docx}
transcript: materials/Transcript.pdf
"""

EXPERIENCE = """experience:
- company: Acme Cloud
  title: AI Engineer Intern (Software Development)
  location: Seattle
  start: 2026-07
  end: 2026-08
  bullets:
  - Built a FastAPI service that generates slide decks from templates, cutting generation latency by ~40%.
  - Wrote 150+ pytest unit and integration tests used as CI release gates.
- company: Globex — Advisory (Technology Risk)
  title: Technology Consultant Trainee
  location: Toronto
  start: 2026-01
  end: 2026-04
  bullets:
  - Co-developed an automated vulnerability assessment pipeline integrating 30+ scanning tools.
  - Security-tested a mobile banking app and documented findings in the client report.
- company: Initech
  title: Software Engineer Intern
  location: Austin, TX
  start: 2025-05
  end: 2025-08
  bullets:
  - Modelled the order lifecycle as a state machine with reconciliation jobs.
education:
- {school: University of Michigan, degree: M.S. in Computer Science, level: master, field: Computer Science,
   location: "Ann Arbor, MI", start: 2026-09, end: 2028-05, gpa: '', honors: ''}
- {school: University of Toronto, degree: B.Eng. in Computer Engineering, level: bachelor, field: Computer Engineering,
   location: Toronto, start: 2022-09, end: 2026-06, gpa: '', honors: Dean's List}
skills:
  Languages: [Python, Java, TypeScript, C++, SQL, Go]
  Backend & Systems: [FastAPI, PostgreSQL, Redis, Docker]
  AI & ML: [PyTorch, LangChain]
"""


def _docx(path: Path, lines: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')


def _build_home() -> None:
    (HOME / "config/resumes").mkdir(parents=True)
    (HOME / "materials").mkdir()
    (HOME / "config/profile.yaml").write_text(PROFILE, encoding="utf-8")
    for name in ("settings", "answers"):
        shutil.copyfile(REPO / f"config/{name}.example.yaml", HOME / f"config/{name}.yaml")
    for kind in ("SDE", "AIE"):
        doc = HOME / f"materials/Resume_Alex_Rivera_{kind}.docx"
        _docx(doc, ["Alex Rivera", "Acme Cloud  AI Engineer Intern", "University of Michigan  M.S. in Computer Science",
                    "Technical Skills", "Languages: Python, Java, TypeScript, C++, SQL, Go"])
        sha1 = hashlib.sha1(doc.read_bytes()).hexdigest()
        (HOME / f"config/resumes/{kind}.yaml").write_text(
            f"source: {doc.relative_to(HOME)}\nsource_sha1: {sha1}\n{EXPERIENCE}", encoding="utf-8")


_build_home()
os.environ["JOBAGENT_HOME"] = str(HOME)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(HOME, ignore_errors=True)
