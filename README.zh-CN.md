# AutoApply

[English](README.md) | **简体中文**

在你自己电脑上运行的求职申请 agent。它从公开的 GitHub 岗位列表拉取北美新发布的实习岗位，用 LLM 对照你的资料逐个阅读 JD，在真实浏览器里填写并提交申请，再通过邮箱核对结果。

它只用你提供的事实回答问题；在你允许真实提交之前，一律停在提交前；遇到人机验证或拦截页面，从不尝试绕过。

> 目前命令行输出、报告和日志都是**简体中文**；`init`、`check` 这两个配置命令同时输出中英文。

---

## 目录

- [功能](#功能)
- [支持的招聘系统](#支持的招聘系统)
- [环境要求](#环境要求)
- [配置方式一：让 agent 帮你配置](#配置方式一让-agent-帮你配置)
- [配置方式二：手动安装](#配置方式二手动安装)
- [配置文件](#配置文件)
- [日常使用](#日常使用)
- [每天自动运行](#每天自动运行)
- [输出](#输出)
- [工作原理](#工作原理)
- [局限](#局限)
- [负责任地使用](#负责任地使用)
- [开发](#开发)

## 功能

| 步骤 | 做什么 |
|---|---|
| **拉取** | 读取 [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships) 和 [SpeedyApply](https://github.com/speedyapply/2027-SWE-College-Jobs) 的实习列表，两边重复的岗位合并。 |
| **规则过滤** | 去掉已关闭、非美国、其他学期、发布太久、已经投过的岗位；也去掉要求公民身份（除非你是公民）、不招你这个学位、或在你禁用列表里的公司的岗位。 |
| **LLM 筛选** | 对照你的资料逐个阅读 JD，判断学期、地点、公民身份 / 安全审查、学位、毕业时间和 sponsorship，给出 可投 / 跳过 / 需确认，以及 0–100 的匹配分。 |
| **投递** | 用 Playwright 打开申请页，读出每个字段。姓名、联系方式、链接和文件直接取自你的资料；其余题目交给 LLM 回答，只能用你的资料、`answers.yaml` 和简历。 |
| **提交前总核对** | 点 Submit 之前，读回页面实际显示的内容（Workday 读整个 Review 页）。先按规则核对身份信息和工作授权答案，再由 LLM 把所有内容和你的资料逐项比对。有改不对的就不提交。 |
| **邮箱** | 可选：通过 IMAP 读取 Gmail。读取验证码和激活链接，统计申请确认信，列出测评、面试和待办。它从不打开这些链接。 |
| **报告** | 每天写一份报告：投了哪些、哪些需要你处理（链接 + 原因）、答不了的问题、要写短文的岗位。 |

## 支持的招聘系统

| 系统 | 情况 |
|---|---|
| Greenhouse | ✅ 可以完整提交；需要邮箱安全码时从 Gmail 读取。 |
| Ashby | ✅ 可以完整提交。 |
| Workday | ✅ 可以完整提交：用 `credentials.txt` 登录或注册，打开激活邮件，走完多步表单，包括日期选择框和自愿披露页。 |
| Lever | ⚠️ 每次提交都会弹 hCaptcha，程序从不代做。无人值守时这些岗位列为手动投递；用 `--headed` 运行时你在浏览器窗口里完成验证，程序会等你。 |
| 其他公司官网 | 尝试通用填表；需要登录的列为手动投递。 |
| Oracle、iCIMS、SmartRecruiters、SuccessFactors、Amazon、Google、TikTok | 列为手动投递，因为它们需要账号或邮箱验证码，或者明确拒绝自动化访问。 |

## 环境要求

- macOS 或 Linux。自带的定时脚本（`ops/`）用的是 macOS launchd；Linux 上用 cron。
- [uv](https://docs.astral.sh/uv/)，它也会顺带安装 Python 3.11+。
- 一个 **OpenAI 兼容的 LLM 接口**：OpenAI、DeepSeek、OpenRouter、本地 vLLM 或 Ollama 等。要读大量很长的 JD，快且便宜的模型就很合适。
- PDF 版简历；还没有 PDF 时可以先用 `.docx`。
- 可选：一个 **Gmail** 邮箱和[应用专用密码](https://support.google.com/accounts/answer/185833)，用于读验证码和确认信。
- 可选：一组用来注册 **Workday** 账号的邮箱和密码。

## 配置方式一：让 agent 帮你配置

仓库里有 [`AGENTS.md`](AGENTS.md)，是专门写给编程 agent 的分步配置指南。Claude Code 通过 `CLAUDE.md` 读取它；Codex、Cursor 等遵循 `AGENTS.md` 约定的 agent 会直接读取；其他 agent 告诉它这个文件就行。

```bash
git clone https://github.com/Maxwell-LuoJY/AutoApply.git
cd AutoApply
claude        # 或者：codex、cursor、gemini……
```

然后对 agent 说：

> 按 AGENTS.md 帮我配置 AutoApply。我的简历在 ~/Documents/Resume.pdf。

agent 会：

1. 安装依赖和 Playwright 浏览器，用 `jobagent init` 生成配置文件。
2. 把简历复制进 `materials/`，并根据简历预填 `config/profile.yaml`。
3. 问你简历上没有的信息：地址、工作授权和 sponsorship、自愿披露答案、期望薪资、已经手动投过的公司。
4. 和你一起过一遍 `answers.yaml` 和 `settings.yaml`。
5. 让你把 LLM key（以及可选的 Gmail、Workday 账号）写进 `.env` 和 `credentials.txt`。这些密钥你可以自己打开文件填写，不必贴进对话。
6. 反复运行 `jobagent check --llm`，直到没有缺项，然后解析简历。
7. 第一次拉取、筛选，并对一两个岗位做**试运行**，带你看它填了什么、截图是什么样。

agent 会保持 `applying.mode: dry_run`。只有你明确要求时，才会开始真实提交或每日定时任务。

## 配置方式二：手动安装

```bash
git clone https://github.com/Maxwell-LuoJY/AutoApply.git
cd AutoApply
uv sync --extra dev                  # 安装依赖
uv run playwright install chromium   # 安装投递用的浏览器
uv run jobagent init                 # 从模板生成 config/*.yaml、.env、credentials.txt
```

然后：

1. **简历。** 把简历放进 `materials/`，在 `config/profile.yaml` 的 `resumes:` 里写好路径。
   - `SDE` 用于软件类岗位。
   - `AIE` 用于 AI、ML、数据、分析和咨询类岗位。
   - 只有一份简历就删掉 `AIE` 那一项。
2. **个人资料。** 填写 `config/profile.yaml`：姓名、联系方式、教育经历（最近的在前，写上 `level`）、可入职时间、工作授权、自愿披露答案和默认答案。每一项的注释都写了它怎么用。
3. **固定答案。** 逐条核对 `config/answers.yaml`，里面是常见附加问题的「正则 → 答案」。其中工作授权部分只是某一种情况的示例，要按你自己的身份改。
4. **运行参数。** 检查 `config/settings.yaml`：
   - `term`，以及对应申请年份的两个岗位列表地址；
   - 每日上限；
   - `blocked_companies`，以及 `hold_companies`（你已经手动投过的公司）；
   - `timezone`。
5. **LLM。** 在 `.env` 里填 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`。同一接口的更多 key 可以写成 `LLM_API_KEY_2` … `LLM_API_KEY_9`，限流按 key 分别计算。
6. **可选。**
   - Gmail：在 `.env` 里填 `GMAIL_ADDRESS` 和 `GMAIL_APP_PASSWORD`。
   - Workday：在 `credentials.txt` 里写上用来注册 Workday 账号的邮箱和密码。
7. **检查：**
   ```bash
   uv run jobagent check --llm --mail   # 每一项给出 ✅ / ⚠️ / ❌
   uv run jobagent resume parse         # 可选：把简历解析成 config/resumes/*.yaml（请核对）
   ```
8. **试运行。** 填几份申请，但不提交：
   ```bash
   uv run jobagent run --limit 10            # 拉取 + 筛选 10 个 JD + 生成待投清单
   uv run jobagent apply --dry-run --target 2
   ```
   打开 `reviews/<日期>.md`，可以看到每个字段填了什么、依据是什么。截图在 `data/screenshots/<日期>/`。

## 配置文件

| 文件 | 进 git？ | 内容 |
|---|---|---|
| `config/profile.yaml` | 否 | 你的个人资料。所有答案都只来自这里、`answers.yaml` 和简历。 |
| `config/answers.yaml` | 否 | 常见附加问题的固定答案（正则 → 答案）。 |
| `config/settings.yaml` | 否 | 学期、岗位来源、上限、并发数、禁用列表、模式（`dry_run` / `submit`）。 |
| `config/resumes/*.yaml` | 否 | 简历的结构化解析（`jobagent resume parse`），`structured_resume: true` 时使用。 |
| `.env` | 否 | LLM 接口和密钥；Gmail 应用专用密码。 |
| `credentials.txt` | 否 | 招聘网站（Workday）账号的邮箱和密码。 |
| `materials/` | 否 | 简历、成绩单文件。 |
| `config/*.example.yaml`、`.env.example`、`credentials.example.txt` | 是 | `jobagent init` 复制用的模板。 |

所有个人文件都在 `.gitignore` 里。fork 之后也请保持这样。

## 日常使用

```bash
uv run jobagent run                     # 拉取 + 筛选 + 生成待投清单（不开浏览器）
uv run jobagent run --apply             # ……再加上投递（模式见 settings.yaml）
uv run jobagent apply --dry-run         # 填表、截图，停在提交前
uv run jobagent apply --submit          # 真的提交，最多 applying.batch_size 个
uv run jobagent apply --id <ID> --headed   # 只投某个岗位，并显示浏览器窗口
uv run jobagent daily                   # 完整的每日任务，会真实提交（测试时加 --dry-run）

uv run jobagent stats                   # 各阶段岗位数
uv run jobagent show <ID>               # 某个岗位的全部记录
uv run jobagent mark <ID> apply|next|skip|applied   # 手动决定（next = 下次最先投）
uv run jobagent brief                   # 扫描 Gmail：今天的确认信和未完成的待办
uv run jobagent inbox list | done <ID>  # 邮箱里的测评、面试和待办
```

- **批量投递。** `apply --submit` 一直投到提交满 `applying.batch_size` 个为止。它同时开 `applying.workers` 个浏览器，同一家公司不会同时投两个岗位。投失败的跳过，换下一个；待投岗位不够时会自动再筛一批。单次运行可以用 `--target N`、`--workers N` 调整。
- **答不了的问题。** 程序答不了的必填题会列在报告和 `manual_apply.md` 里。把关键词和答案写进 `answers.yaml`，再运行 `jobagent mark <ID> apply` 重新排队。
- **手动投递。** 你自己投完一个岗位后，运行 `jobagent mark <ID> applied`。
- **在 agent 之外投过的公司。** 写进 `hold_companies`。这些公司的新岗位会列出来让你决定，不会自动投。

## 每天自动运行

`uv run jobagent daily` 依次做这几件事：

1. 扫描邮箱。
2. 拉取新岗位。
3. 先筛选过去 24 小时内发布的岗位。
4. 投递（最多 `daily.max_submissions` 个），同时在后台筛选更早发布的岗位。
5. 再扫一遍邮箱。
6. 写出 `daily_reports/YYYY-MM-DD.md`。

报告最前面是邮箱简报。报告还会单独列出「记录为失败、但邮箱里已经收到确认信」的岗位，方便你重新排队前先核对。

有两种定时方式：

- **macOS launchd。** `ops/install_schedule.sh` 设为每天 9:00 运行，`ops/uninstall_schedule.sh` 取消。日志写在 `data/launchd.log`。
- **agent 工具里的计划任务。** 例如在 Claude Code 桌面应用里建一个每日任务：运行 `uv run jobagent daily`，然后在新对话里贴出报告中的手动投递链接。

两个投递任务不会同时运行（`data/apply.lock`）。

## 输出

| 路径 | 内容 |
|---|---|
| `shortlists/YYYY-MM-DD.md` | 可投的岗位、需要你决定的岗位、读不到 JD 的岗位。 |
| `reviews/YYYY-MM-DD.md` | 每次投递或试运行：每个字段、填了什么、依据是什么，以及总核对结果。 |
| `run_logs/YYYY-MM-DD.md` | 运行总结和需要你处理的事项。 |
| `daily_reports/` | 每日报告和邮箱简报。 |
| `manual_apply.md` | agent 没能完成的所有岗位（链接 + 原因）。 |
| `applications.csv` | 只追加的记录：已提交、失败、跳过。 |
| `data/` | SQLite 数据库、截图、浏览器配置目录（招聘网站的登录状态）、日志。 |

## 工作原理

```
sources/（Simplify、SpeedyApply）─▶ rules.py（硬性过滤）─▶ screen.py（LLM：资格 + 匹配分）
        ─▶ apply/runner.py ─▶ adapters.py（Greenhouse / Ashby / Workday / Lever / 通用）
                            ├─ forms.py + extract.js   读取并填写每个字段
                            ├─ answers.py              资料 → 规则 → LLM，附带安全检查
                            ├─ audit.py                提交前读回页面，规则 + LLM 总核对
                            └─ mail.py                 Gmail 验证码、激活链接、确认信
        ─▶ report.py / daily.py（待投清单、投递记录、手动清单、每日报告）   inbox.py（招聘邮件）
```

- **以下情况停下、不提交：**
  - 必须写 cover letter，或者要求不用 AI 写的短文；
  - 有必填题无法从你的资料回答；
  - 工作授权类答案和资料矛盾；
  - 页面出现人机验证或拒绝访问；
  - 网站需要的登录方式不支持。
- **只认确认过的提交。** 看到成功页面才记为「已提交」，否则记为「未确认」，并且从不自动重试，避免重复投递。
- **瓶颈在 LLM 接口。** 每个（key, 模型）组合单独限速；某个 key 遇到 429 就暂停一会儿，先换下一个 key，再换下一个备用模型。

## 局限

- 针对**美国实习**调校（默认是 Summer 2027 的列表）。换学期或年份，要改 `settings.yaml` 里的 `term` 和两个岗位列表地址。
- 默认美国地址和美国电话号码。学位规则覆盖本科、硕士和 PhD。
- 最多两条简历线（`SDE` 和 `AIE`），按岗位名称自动选择。
- Lever（hCaptcha）和只能用账号登录的系统仍然需要你自己处理。
- LLM 会出错。总核对能拦住大部分错误，但头几次运行请认真看 `reviews/`。
- 命令行输出和报告目前是中文。

## 负责任地使用

这个工具提交的每一份申请都以你的名义发出，由你负责。切换到 `submit` 之前：

- 仔细核对资料和答案。
- 看过试运行的记录。
- 把每日上限设在合理范围内。
- 遵守各网站的使用条款。

程序刻意从不绕过人机验证、机器人检测或访问限制，也请不要加上这类功能。

## 开发

```bash
uv run pytest -q
```

测试在临时的 `JOBAGENT_HOME` 里用一个虚构的申请人运行（`tests/conftest.py`），不会碰你的配置和数据。代码在 `src/jobagent/`，模块说明见 [`AGENTS.md`](AGENTS.md)。
