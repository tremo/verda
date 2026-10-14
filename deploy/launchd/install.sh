#!/bin/zsh
# Installs the Verda services as launchd agents for the current user. Run it yourself; it changes nothing else.
# Safe to run again: each service is stopped, given a moment to go away, then loaded (retried if launchd is still busy).
set -u
here="$(cd "$(dirname "$0")" && pwd)"
domain="gui/$(id -u)"
mkdir -p "$HOME/Library/Logs/Verda" "$HOME/Library/LaunchAgents"
for label in com.verda.panel com.verda.agency-worker com.verda.jev-worker com.verda.publisher; do
  cp "$here/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "$domain/$label" 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8; do
    launchctl print "$domain/$label" >/dev/null 2>&1 || break     # gone: safe to load
    sleep 1
  done
  loaded=0
  for i in 1 2 3 4 5; do
    if launchctl bootstrap "$domain" "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null; then loaded=1; break; fi
    sleep 2
  done
  [ "$loaded" = 1 ] && echo "yüklendi: $label" || echo "YÜKLENEMEDİ: $label"
done
launchctl list | grep verda
