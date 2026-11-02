#!/bin/zsh
# Remove the daily launchd job.
set -euo pipefail
launchctl bootout "gui/$(id -u)/com.jobagent.daily" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.jobagent.daily.plist"
echo "已移除每日定时任务"
