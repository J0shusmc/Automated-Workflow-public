#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="automated-workflow.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

mkdir -p "${USER_SYSTEMD_DIR}"
sed "s|__PROJECT_DIR__|${PROJECT_DIR}|g" "${PROJECT_DIR}/systemd/user/${UNIT_NAME}" > "${USER_SYSTEMD_DIR}/${UNIT_NAME}"

systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAME}"

echo "Automated Workflow is managed by systemd user service: ${UNIT_NAME}"
echo "Open locally: http://127.0.0.1:8787"
echo "Open from LAN: http://<this-computer-ip>:8787"
echo "Restart: systemctl --user restart automated-workflow"
echo "Stop:    systemctl --user stop automated-workflow"
echo "Logs:    journalctl --user -u automated-workflow -f"
