#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root: sudo ./scripts/install-systemd-services.sh"
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
RUN_USER="${SUDO_USER:-${USER:-nicholas-craig}}"
if ! id "${RUN_USER}" >/dev/null 2>&1; then
  echo "user '${RUN_USER}' does not exist on this host"
  exit 1
fi
RUN_GROUP="$(id -gn "${RUN_USER}")"

SERVICE_NAME="${SERVICE_NAME:-grievance-mvp}"
SYSTEMD_DIR="/etc/systemd/system"

UP_WRAPPER="/usr/local/bin/${SERVICE_NAME}-up"
DOWN_WRAPPER="/usr/local/bin/${SERVICE_NAME}-down"
WATCHDOG_WRAPPER="/usr/local/bin/${SERVICE_NAME}-watchdog"

SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}.service"
WATCHDOG_SERVICE_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}-watchdog.service"
WATCHDOG_TIMER_FILE="${SYSTEMD_DIR}/${SERVICE_NAME}-watchdog.timer"

cat >"${UP_WRAPPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${PROJECT_DIR}"
exec /usr/bin/docker compose --env-file ".env" -f "docker-compose.yml" up -d
EOF

cat >"${DOWN_WRAPPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${PROJECT_DIR}"
exec /usr/bin/docker compose --env-file ".env" -f "docker-compose.yml" down
EOF

cat >"${WATCHDOG_WRAPPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${PROJECT_DIR}"
exec "${PROJECT_DIR}/scripts/watchdog-restart.sh"
EOF

chmod 0755 "${UP_WRAPPER}" "${DOWN_WRAPPER}" "${WATCHDOG_WRAPPER}"

cat >"${SERVICE_FILE}" <<EOF
[Unit]
Description=Grievance MVP Docker Compose Stack
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=${RUN_USER}
Group=${RUN_GROUP}
ExecStart=${UP_WRAPPER}
ExecStop=${DOWN_WRAPPER}
TimeoutStartSec=0
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF

cat >"${WATCHDOG_SERVICE_FILE}" <<EOF
[Unit]
Description=Grievance MVP Health Watchdog
After=${SERVICE_NAME}.service
Requires=${SERVICE_NAME}.service

[Service]
Type=oneshot
User=${RUN_USER}
Group=${RUN_GROUP}
ExecStart=${WATCHDOG_WRAPPER}
EOF

cat >"${WATCHDOG_TIMER_FILE}" <<EOF
[Unit]
Description=Run Grievance MVP watchdog every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
RandomizedDelaySec=15s
Persistent=true
Unit=${SERVICE_NAME}-watchdog.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"
systemctl enable --now "${SERVICE_NAME}-watchdog.timer"

echo "Installed:"
echo "  - ${SERVICE_FILE}"
echo "  - ${WATCHDOG_SERVICE_FILE}"
echo "  - ${WATCHDOG_TIMER_FILE}"
echo
echo "Current status:"
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
systemctl --no-pager --full status "${SERVICE_NAME}-watchdog.timer" || true
