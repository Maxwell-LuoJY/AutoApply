# AutoApply

**English** | [简体中文](README.zh-CN.md)

A job-application agent that runs on your own computer. It pulls new North American internship postings from public GitHub lists. An LLM reads each job description against your profile. Then it fills in and submits applications in a real browser and checks your inbox for the results.

It answers only from facts you give it, it stops before Submit until you allow real submissions, and it never tries to get past a CAPTCHA or a bot wall.

> The CLI messages, reports and logs are currently in **Simplified Chinese**. The setup commands (`init`, `check`) print both languages.

---

## Contents

- [What it does](#what-it-does)
- [Supported application systems](#supported-application-systems)
- [Requirements](#requirements)
- [Setup, option A: let an agent set it up](#setup-option-a-let-an-agent-set-it-up)
- [Setup, option B: manual install](#setup-option-b-manual-install)
- [Configuration files](#configuration-files)
- [Daily use](#daily-use)
- [Running it every day](#running-it-every-day)
- [Output](#output)
- [How it works](#how-it-works)
- [Limits](#limits)
- [Responsible use](#responsible-use)
- [Development](#development)
- [License](#license)

## What it does

| Step | What happens |
|---|---|
| **Fetch** | Pulls the [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships) and [SpeedyApply](https://github.com/speedyapply/2027-SWE-College-Jobs) internship lists and merges postings that appear in both. |
| **Rule filter** | Drops postings that are closed, outside the U.S., for another term, too old, or already applied to. Also drops postings that require citizenship (unless you are a citizen), are closed to your degree level, or come from companies on your blocklist. |
| **LLM screen** | Reads every job description against your profile: term, location, citizenship or clearance, degree level, graduation date and sponsorship. It returns *apply*, *skip* or *unsure*, plus a 0–100 fit score. |
| **Apply** | Opens the application in Playwright and reads every field. Name, contact details, links and files come straight from your profile. The LLM answers the remaining questions, using only your profile, `answers.yaml` and your resume. |
| **Pre-submit audit** | Before Submit, it reads back what the page shows (on Workday, the whole Review page). It checks identity fields and work-authorization answers by rule, then has an LLM judge compare everything with your facts. If something can't be fixed, it doesn't submit. |
| **Inbox** | Optional: reads Gmail over IMAP. It picks up verification codes and activation links, counts confirmation emails, and lists assessments, interviews and to-dos. It never opens those links. |
| **Report** | Writes a daily report: what was submitted, what needs you (links and reasons), questions it couldn't answer, and essays to write. |

## Supported application systems

| System | Status |
|---|---|
| Greenhouse | ✅ Submits end to end. Emailed security codes are read from Gmail. |
| Ashby | ✅ Submits end to end. |
| Workday | ✅ Submits end to end. Signs in or registers with `credentials.txt`, opens the activation email, and goes through the multi-step forms, including date pickers and the self-identification pages. |
| Lever | ⚠️ Shows an hCaptcha on every submit, which the program never solves. Unattended runs list these jobs for manual applying. With `--headed`, you solve the hCaptcha in the browser window and the program waits for you. |
| Other company sites | Tries a generic form fill. Sites that need a login are listed for manual applying. |
| Oracle, iCIMS, SmartRecruiters, SuccessFactors, Amazon, Google, TikTok | Listed for manual applying, because they need an account or emailed code, or refuse automation. |

## Requirements

- macOS or Linux. The built-in scheduler (`ops/`) uses macOS launchd; on Linux, use cron.
- [uv](https://docs.astral.sh/uv/), which also installs Python 3.11+.
- An **OpenAI-compatible LLM endpoint**: OpenAI, DeepSeek, OpenRouter, a local vLLM or Ollama server, and so on. The job reads many long job descriptions, so a fast, cheap model works well.
- Your resume as a PDF, or a `.docx` until you have a PDF.
- Optional: a **Gmail** address with an [app password](https://support.google.com/accounts/answer/185833), for verification codes and confirmation emails.
- Optional: an email and password to use for **Workday** accounts.

## Setup, option A: let an agent set it up

The repo includes [`AGENTS.md`](AGENTS.md), a step-by-step setup guide written for coding agents. Claude Code reads it through `CLAUDE.md`. Codex, Cursor and other agents that follow the `AGENTS.md` convention read it directly. For any other agent, point it at the file.

```bash
git clone https://github.com/Maxwell-LuoJY/AutoApply.git
cd AutoApply
claude        # or: codex, cursor, gemini, ...
```

Then tell the agent something like:

> Set up AutoApply for me by following AGENTS.md. My resume is at ~/Documents/Resume.pdf.

The agent will:

1. Install the dependencies and the Playwright browser, and create your config files with `jobagent init`.
2. Copy your resume into `materials/` and pre-fill `config/profile.yaml` from it.
3. Ask you for everything a resume doesn't say: your address, work authorization and sponsorship, self-identification answers, salary expectation, and companies you've already applied to.
4. Go through `answers.yaml` and `settings.yaml` with you.
5. Have you put your LLM key (and, optionally, Gmail and Workday credentials) into `.env` and `credentials.txt`. You can type secrets into those files yourself instead of pasting them into the chat.
6. Run `jobagent check --llm` until nothing is missing, then parse your resume.
7. Do a first fetch, screen and **dry run** on one or two jobs, and walk you through what it filled and the screenshots.

The agent keeps `applying.mode: dry_run`. Real submissions and the daily schedule start only when you explicitly ask for them.

## Setup, option B: manual install

```bash
git clone https://github.com/Maxwell-LuoJY/AutoApply.git
cd AutoApply
uv sync --extra dev                  # dependencies
uv run playwright install chromium   # browser used for applying
uv run jobagent init                 # creates config/*.yaml, .env, credentials.txt from the examples
```

Then:

1. **Resume.** Put your resume in `materials/`. Set the paths under `resumes:` in `config/profile.yaml`.
   - `SDE` is used for software roles.
   - `AIE` is used for AI, ML, data, analyst and consulting roles.
   - If you have only one resume, delete the `AIE` entry.
2. **Profile.** Fill in `config/profile.yaml`: name, contact details, education (most recent first, with `level`), availability, work authorization, self-identification answers and defaults. Every comment explains how the field is used.
3. **Answers.** Go through `config/answers.yaml`. It holds regex → answer pairs for common extra questions. Its work-authorization part is an example for one particular case, so change it to match your own status.
4. **Settings.** Check `config/settings.yaml`:
   - `term`, and the source URLs for the year you are applying in;
   - the daily caps;
   - `blocked_companies`, and `hold_companies` (companies you already applied to by hand);
   - `timezone`.
5. **LLM.** In `.env`, set `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL`. You can add more keys for the same endpoint as `LLM_API_KEY_2` … `LLM_API_KEY_9`; rate limits are counted per key.
6. **Optional.**
   - Gmail: set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in `.env`.
   - Workday: put the email and password to use for Workday accounts in `credentials.txt`.
7. **Check:**
   ```bash
   uv run jobagent check --llm --mail   # ✅ / ⚠️ / ❌ for every part of the setup
   uv run jobagent resume parse         # optional: resume → config/resumes/*.yaml (review it)
   ```
8. **Dry run.** Fill a couple of applications without submitting:
   ```bash
   uv run jobagent run --limit 10            # fetch + screen 10 JDs + shortlist
   uv run jobagent apply --dry-run --target 2
   ```
   Open `reviews/<date>.md` to see every field it filled and why. Screenshots are in `data/screenshots/<date>/`.

## Configuration files

| File | In git? | What it holds |
|---|---|---|
| `config/profile.yaml` | no | Your facts. Every answer comes from here, `answers.yaml` and your resume. |
| `config/answers.yaml` | no | Fixed answers to common extra questions (regex → answer). |
| `config/settings.yaml` | no | Term, sources, caps, workers, blocklist, mode (`dry_run` / `submit`). |
| `config/resumes/*.yaml` | no | Your resume parsed into a structured form (`jobagent resume parse`), used when `structured_resume: true`. |
| `.env` | no | LLM endpoint and keys; Gmail app password. |
| `credentials.txt` | no | Account email and password for employer career sites (Workday). |
| `materials/` | no | Resume and transcript files. |
| `config/*.example.yaml`, `.env.example`, `credentials.example.txt` | yes | Templates copied by `jobagent init`. |

Everything personal is git-ignored. Keep it that way if you fork the repo.

## Daily use

```bash
uv run jobagent run                     # fetch + screen + shortlist (no browser)
uv run jobagent run --apply             # ... then apply (mode from settings.yaml)
uv run jobagent apply --dry-run         # fill forms, screenshot, stop before Submit
uv run jobagent apply --submit          # really submit, up to applying.batch_size
uv run jobagent apply --id <ID> --headed   # one job, with the browser window visible
uv run jobagent daily                   # the full daily job; it SUBMITS (add --dry-run to test it)

uv run jobagent stats                   # jobs per stage
uv run jobagent show <ID>               # everything stored about one job
uv run jobagent mark <ID> apply|next|skip|applied   # decide by hand (next = first in the next run)
uv run jobagent brief                   # scan Gmail; today's confirmations and open to-dos
uv run jobagent inbox list | done <ID>  # assessments, interviews and tasks from your inbox
```

- **Batches.** `apply --submit` keeps going until it has submitted `applying.batch_size` applications. It runs `applying.workers` browsers in parallel and never applies to two jobs at the same company at the same time. A failed job is skipped and the next one is tried. When the queue runs dry, it screens more postings. Adjust a single run with `--target N` and `--workers N`.
- **Questions it can't answer.** Required questions it can't answer are listed in the report and in `manual_apply.md`. Add the pattern and your answer to `answers.yaml`, then run `jobagent mark <ID> apply` to queue the job again.
- **Applying by hand.** After you apply to a job yourself, run `jobagent mark <ID> applied`.
- **Companies you applied to outside the agent.** Put them in `hold_companies`. Their new postings are listed for you to decide on, not applied to.

## Running it every day

`uv run jobagent daily` runs these steps in order:

1. Scans your inbox.
2. Fetches new postings.
3. Screens the last 24 hours first.
4. Applies (at most `daily.max_submissions`) while older postings are screened in the background.
5. Scans the inbox again.
6. Writes `daily_reports/YYYY-MM-DD.md`.

The report puts the inbox briefing first. It also lists jobs that are recorded as failed but for which a confirmation email arrived, so you can check before re-queuing them.

There are two ways to run it on a schedule:

- **macOS launchd.** `ops/install_schedule.sh` runs it at 9:00 every day; `ops/uninstall_schedule.sh` removes the schedule. The log is written to `data/launchd.log`.
- **A scheduled task in your agent harness.** For example, a daily task in the Claude Code desktop app runs `uv run jobagent daily` and posts the report's manual-apply links in a new chat.

Two apply runs never overlap (`data/apply.lock`).

## Output

| Path | Content |
|---|---|
| `shortlists/YYYY-MM-DD.md` | Jobs ready to apply to, jobs that need your decision, and jobs whose JD couldn't be read. |
| `reviews/YYYY-MM-DD.md` | Every application or dry run: each field, the value filled, where it came from, and the audit result. |
| `run_logs/YYYY-MM-DD.md` | Run summary and what needs you. |
| `daily_reports/` | Daily report and inbox briefing. |
| `manual_apply.md` | Every job the agent could not finish, with a link and the reason. |
| `applications.csv` | Append-only record of submissions, failures and skips. |
| `data/` | SQLite database, screenshots, browser profiles (your career-site sign-ins), logs. |

## How it works

```
sources/ (Simplify, SpeedyApply) ─▶ rules.py (hard filters) ─▶ screen.py (LLM: eligibility + fit)
        ─▶ apply/runner.py ─▶ adapters.py (Greenhouse / Ashby / Workday / Lever / generic)
                            ├─ forms.py + extract.js   read and fill every field
                            ├─ answers.py              profile facts → rules → LLM, with safety checks
                            ├─ audit.py                read back the page, rules + LLM judge, before Submit
                            └─ mail.py                 Gmail codes, activation links, confirmations
        ─▶ report.py / daily.py (shortlists, reviews, manual list, daily report)   inbox.py (recruiting emails)
```

- **Applications stop without submitting when:**
  - a cover letter or a no-AI essay is required;
  - a required question can't be answered from your facts;
  - a work-authorization answer contradicts your profile;
  - the page shows a CAPTCHA or an access-denied page;
  - the site needs a login that isn't supported.
- **Only confirmed submissions count.** A job is recorded as submitted only after a success page. Anything less is "unconfirmed" and is never retried automatically, so nothing is submitted twice.
- **The LLM endpoint is the bottleneck.** Every (key, model) pair is paced separately. A key that gets a 429 cools down while the next key, then the next fallback model, takes over.

## Limits

- It is tuned for **U.S. internships** (Summer 2027 lists by default). For another season or year, change `term` and the two source URLs in `settings.yaml`.
- It assumes a U.S. address and phone number. Its degree rules cover Bachelor's, Master's and PhD students.
- It handles up to two resume tracks (`SDE` and `AIE`), chosen by job title.
- Lever (hCaptcha) and account-only systems still need you.
- The LLM can be wrong. The audit catches most mistakes, but read `reviews/` in your first runs.
- CLI output and reports are in Chinese for now.

## Responsible use

Every application this tool submits is sent in your name, and you are responsible for it. Before switching to `submit`:

- Check your profile and answers carefully.
- Read the dry-run reviews.
- Keep the daily caps reasonable.
- Respect each site's terms.

The program deliberately never bypasses CAPTCHAs, bot checks or access restrictions. Please don't add that.

## Development

```bash
uv run pytest -q
```

Tests run against a fictional applicant in a temporary `JOBAGENT_HOME` (`tests/conftest.py`), so they never touch your config or data. Code lives in `src/jobagent/`; see the module map in [`AGENTS.md`](AGENTS.md).

## License

[MIT](LICENSE) © 2026 Maxwell Luo
