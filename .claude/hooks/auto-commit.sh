#!/bin/bash
# Stop hook: commit whatever is still uncommitted when a Claude turn ends. A safety net only: Claude commits each
# change itself with a descriptive message. .gitignore keeps secrets, data/ and per-run outputs out.
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
gitdir=$(git rev-parse --git-dir 2>/dev/null) || exit 0
if [ -e "$gitdir/MERGE_HEAD" ] || [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ]; then
  exit 0  # never commit in the middle of a merge or rebase
fi
git add -A || exit 0
git diff --cached --quiet && exit 0
# never auto-commit something that looks like a secret, even if .gitignore missed it
if git diff --cached --name-only | grep -qiE '(^|/)(\.env[^/]*|credentials[^/]*|[^/]*\.(pem|key|p12))$'; then
  git reset -q
  echo '{"systemMessage": "⚠ 自动提交已跳过：有像密钥的文件（.env / credentials / 证书）待提交，请先加进 .gitignore"}'
  exit 0
fi
files=$(git diff --cached --name-only)
n=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
git commit -q -m "自动提交：本轮结束时还没提交的改动（$n 个文件）" -m "$(printf '%s\n' "$files" | head -30)" || exit 0
echo "{\"systemMessage\": \"已自动提交 $n 个还没提交的文件（$(git rev-parse --short HEAD)）\"}"
