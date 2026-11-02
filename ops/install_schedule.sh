#!/bin/zsh
# Install (or reinstall) the daily launchd job. Undo with ops/uninstall_schedule.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
DEST="$HOME/Library/LaunchAgents/com.jobagent.daily.plist"

mkdir -p "$ROOT/data" "$HOME/Library/LaunchAgents"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__UV__|$UV|g" "$ROOT/ops/com.jobagent.daily.plist" > "$DEST"
launchctl bootout "gui/$(id -u)/com.jobagent.daily" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "已安装：每天 9:00 运行 jobagent daily（会真实提交，最多 daily.max_submissions 个）/ installed: jobagent daily at 9:00, submits for real"
echo "日志：$ROOT/data/launchd.log"
