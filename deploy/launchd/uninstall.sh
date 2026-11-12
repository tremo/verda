#!/bin/zsh
for label in com.verda.panel com.verda.agency-worker com.verda.jev-worker com.verda.publisher com.verda.telegram; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
