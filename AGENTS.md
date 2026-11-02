# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Gemini CLI, …) working in this repository.

AutoApply (`jobagent` CLI) is a local agent with four stages:

1. It fetches U.S. internship postings from public GitHub lists.
2. It filters them by rule, then has an LLM screen each one.
3. It fills in and submits applications with Playwright.
4. It reads Gmail for verification codes and confirmations.

User-facing CLI output and reports are in Simplified Chinese. Talk to the user in the language they use with you.

## Hard rules (always)

- **Never get past a CAPTCHA, bot check, 403, "access restricted" page or rate limit.** That means no header or user-agent spoofing, stealth plugins, solver services or proxy rotation. Mark the job for manual applying instead. This is a design rule of the project, not a missing feature.
- **Never submit a real application without the user's explicit go-ahead in this conversation.** Use dry runs (`apply --dry-run`, `daily --dry-run`) by default. `jobagent daily` and `jobagent apply --submit` submit for real.
- **Never guess work-authorization, citizenship, sponsorship, self-identification (EEO) or other legal answers.** They are statements the user makes to employers. Ask the user, and write down only what they confirm.
- **Keep personal data out of git and out of chat output.** The following are git-ignored on purpose; never commit them or `git add -f` them:
  - `.env`, `credentials.txt`
  - `config/profile.yaml`, `config/answers.yaml`, `config/settings.yaml`, `config/resumes/`
  - `materials/`, `data/`
  - `applications.csv` and every report folder

  Don't print secrets back to the user.
- **Check the inbox before re-queuing a job a run reported as "failed".** A submission can go through even when the page errored afterwards; run `uv run jobagent brief`, or look for the confirmation email.
- Before and after changing code, run `uv run pytest -q`. Tests use a fictional applicant (`tests/conftest.py`). Never point them at real data.

## Setup playbook

Use this when the user asks to install, set up or configure AutoApply. Work through it in order, and tell the user briefly what each step is for. If `uv run jobagent check` already passes, skip to step 9 and fix only what it reports.

1. **Prerequisites.**
   - Check `uv --version`. If uv is missing, ask before installing it (https://docs.astral.sh/uv/getting-started/installation/).
   - Then run `uv sync --extra dev` and `uv run playwright install chromium`.
2. **Create the config files.** Run `uv run jobagent init`. It copies these templates and never overwrites existing files:
   - `config/{settings,profile,answers}.example.yaml` → `config/*.yaml`
   - `.env.example` → `.env`
   - `credentials.example.txt` → `credentials.txt`
3. **Resume.**
   - Ask for the resume file(s) and copy them into `materials/`.
   - Prefer a PDF; a `.docx` works until a PDF exists. The file name is what recruiters see.
   - Fill in `resumes:` in `config/profile.yaml`. `SDE` is used for software roles. `AIE` is used for AI, ML, data, analyst and consulting roles. With only one resume, delete the `AIE` entry.
   - Give all resume files a common name prefix (e.g. `Resume_First_Last_SDE.pdf` and `Resume_First_Last_AIE.pdf`). Workday drafts use that prefix to remove the user's older uploads.
4. **Profile (`config/profile.yaml`).**
   - Read the resume to extract its text. For a PDF:
     ```
     uv run python -c "from pypdf import PdfReader; import sys; print('\n'.join(p.extract_text() or '' for p in PdfReader(sys.argv[1]).pages))" materials/<file>.pdf
     ```
   - Pre-fill name, email, phone, links and education. List schools most recent first, each with:
     - the school name as forms should show it, with no leading "The", plus `aliases`;
     - `degree` and `level` (master | bachelor | phd | associate);
     - `start` and `end` as `YYYY-MM`;
     - `expected: true` while still enrolled;
     - `gpa`, or `null` to leave GPA boxes blank.
   - Then ask the user, in one message, for what a resume doesn't say:
     - legal vs preferred name, middle name;
     - full address, school email;
     - availability;
     - work authorization: authorized to work in the U.S.? needs sponsorship now or in the future? U.S. citizen or permanent resident? permanent / indefinite authorization? visa status? citizenship(s)?
     - extra rules for sponsorship questions, which go in `notes`;
     - EEO answers (offer "Decline to answer");
     - salary expectation, willingness to relocate.
   - Remove every value left over from the example applicant (Alex Rivera).
5. **Answers (`config/answers.yaml`).**
   - Go through the example entries with the user.
   - Rewrite the work-authorization block so it agrees with the profile.
   - Ask about entries that depend on the person: driver's license, pronouns, languages, the commute city, travel.
   - Each entry is `match` (case-insensitive regex) plus `answer`, which can be an instruction.
6. **Settings (`config/settings.yaml`).**
   - `term`, and the two source URLs for the year the user applies in.
   - `daily.max_submissions` and workers. Suggest starting low.
   - `hold_companies`: companies the user already applied to outside the agent.
   - `blocked_companies`.
   - `timezone`.
   - Leave `applying.mode: dry_run`.
7. **Secrets.**
   - `.env` needs `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` (any OpenAI-compatible endpoint). Extra keys for the same endpoint go in `LLM_API_KEY_2`…`_9`.
   - Optional: `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`. The app password needs 2-Step Verification; it is created under Google Account → Security → App passwords.
   - Optional: `credentials.txt`, the email and password used to register on Workday. Suggest a password unique to job sites.
   - Offer to let the user edit these files themselves instead of pasting secrets into chat.
8. **Verify.**
   - Run `uv run jobagent check --llm`, adding `--mail` if Gmail is set. Fix every ❌; mention the ⚠️ items.
   - Run `uv run jobagent resume parse`. Show the user `config/resumes/*.yaml` and ask them to confirm it. Only then suggest `applying.structured_resume: true`.
9. **First run, without submitting.**
   - Run `uv run jobagent run --limit 10` and summarize `shortlists/<today>.md`.
   - Then run `uv run jobagent apply --dry-run --target 2`.
   - Walk the user through `reviews/<today>.md` (each field, the value, its source) and the screenshots in `data/screenshots/<today>/`.
   - Fix wrong answers in `profile.yaml` or `answers.yaml`, not in code.
10. **Going live, only when the user asks.**
    - Real submissions: `uv run jobagent apply --submit --target 3`, or `applying.mode: submit`.
    - Daily schedule: `ops/install_schedule.sh` (macOS launchd, 9:00), or a scheduled task in the user's agent harness that runs `uv run jobagent daily` and reports `daily_reports/<date>.md`.
    - Make sure the user knows that `daily` submits for real, up to `daily.max_submissions`.

## Day-to-day help

- **Unanswered required questions.** They are listed in the daily report and `manual_apply.md`. Ask the user for the answer, add an `answers.yaml` entry, then run `uv run jobagent mark <ID> apply`.
- **Inspecting jobs.** Use `uv run jobagent stats`, `show <ID>`, and `mark <ID> apply|next|skip|applied`.
- **Inbox.** `uv run jobagent brief` lists confirmations, assessments, interviews and to-dos. The program never opens assessment or interview links.

## Code map

| Path | Role |
|---|---|
| `src/jobagent/cli.py` | CLI entry point (`jobagent …`) |
| `src/jobagent/onboard.py` | `init` / `check` |
| `src/jobagent/config.py` | paths (`JOBAGENT_HOME`), YAML config, LLM credentials, degree level |
| `src/jobagent/sources/` | SimplifyJobs and SpeedyApply list parsers |
| `src/jobagent/rules.py` | URL identity, ATS detection, hard filters, resume choice, apply order |
| `src/jobagent/screen.py` | LLM eligibility screen and fit score (rules built from the profile) |
| `src/jobagent/pipeline.py` | fetch → refilter → screen; background screening |
| `src/jobagent/apply/runner.py` | per-job apply loop, parallel workers, outcomes |
| `src/jobagent/apply/adapters.py` | per-ATS behaviour (Greenhouse, Ashby, Workday, Lever, generic) |
| `src/jobagent/apply/forms.py`, `extract.js` | reading and filling form fields, option matching |
| `src/jobagent/apply/answers.py` | what goes in each field: profile facts, rules, LLM, safety checks |
| `src/jobagent/apply/audit.py` | pre-submit read-back: rules plus LLM judge |
| `src/jobagent/apply/resume_fill.py`, `resume.py` | structured resume parsing and experience/education filling |
| `src/jobagent/apply/mail.py`, `inbox.py` | Gmail: codes, activation links, confirmations, recruiting-email tasks |
| `src/jobagent/report.py`, `daily.py` | shortlists, reviews, manual list, daily report |
| `src/jobagent/db.py`, `history.py` | SQLite job store; `applications.csv` history |

Conventions:

- Lines up to about 120 characters.
- Comments explain *why*, often naming the employer site where a case was seen.
- User-facing strings are in Chinese.
- New applicant-specific behaviour must come from `profile.yaml`, `answers.yaml` or `settings.yaml`, never from hardcoded personal facts.
