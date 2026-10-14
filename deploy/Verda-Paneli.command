#!/bin/zsh
# Double-click: opens the Verda panel in your browser with a fresh one-time link (the panel runs as a background service).
cd "$(dirname "$0")/.." && .venv/bin/verda panel --open || { echo; echo "Panel açılamadı. Servisleri kurmak için: deploy/launchd/install.sh"; read -k 1 "?Kapatmak için bir tuşa bas..."; }
