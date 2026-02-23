#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: Missing .env at $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

WEBHOOK_URL="${1:-${DOCUSEAL_WEBHOOK_TARGET_URL:-}}"
if [[ -z "$WEBHOOK_URL" && -n "${GRIEVANCE_DOMAIN:-}" ]]; then
  WEBHOOK_URL="https://api.${GRIEVANCE_DOMAIN}/webhook/docuseal"
fi
if [[ -z "$WEBHOOK_URL" ]]; then
  echo "ERROR: Set DOCUSEAL_WEBHOOK_TARGET_URL in .env or pass URL as first arg" >&2
  exit 1
fi

WEBHOOK_SECRET="${DOCUSEAL_WEBHOOK_SECRET:-}"
if [[ -z "$WEBHOOK_SECRET" ]]; then
  WEBHOOK_SECRET="$(sed -nE 's/^[[:space:]]*webhook_secret:[[:space:]]*"?([^"#]+)"?.*$/\1/p' config/config.yaml | head -n1 | tr -d '[:space:]')"
fi
if [[ -z "$WEBHOOK_SECRET" || "$WEBHOOK_SECRET" == REPLACE* ]]; then
  echo "ERROR: docuseal.webhook_secret must be set in config/config.yaml or DOCUSEAL_WEBHOOK_SECRET env" >&2
  exit 1
fi

HEADER_NAME="${DOCUSEAL_WEBHOOK_HEADER_NAME:-X-Webhook-Token}"

echo "Syncing DocuSeal webhook URL to: $WEBHOOK_URL"
echo "Using webhook auth header: $HEADER_NAME"

docker compose --env-file .env up -d docuseal docuseal_db >/dev/null

docker compose --env-file .env exec -T \
  -e DOCUSEAL_WEBHOOK_URL="$WEBHOOK_URL" \
  -e DOCUSEAL_WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  -e DOCUSEAL_WEBHOOK_HEADER_NAME="$HEADER_NAME" \
  docuseal sh -lc '
    cd /app
    bundle exec rails runner "
      url = ENV.fetch(\"DOCUSEAL_WEBHOOK_URL\")
      secret = ENV.fetch(\"DOCUSEAL_WEBHOOK_SECRET\")
      header_name = ENV.fetch(\"DOCUSEAL_WEBHOOK_HEADER_NAME\", \"X-Webhook-Token\")
      events = %w[submission.completed]
      accounts = Account.order(:id)
      if accounts.none?
        puts \"No accounts found yet; nothing to update\"
        exit 0
      end
      accounts.each do |account|
        rec = WebhookUrl.find_or_initialize_by(account_id: account.id, sha1: Digest::SHA1.hexdigest(url))
        rec.url = url
        rec.events = (Array(rec.events) | events)
        secret_hash = rec.secret.to_h
        secret_hash[header_name] = secret
        rec.secret = secret_hash
        rec.save!
        puts \"account=#{account.id} webhook_id=#{rec.id} events=#{rec.events.join(\",\")}\"
      end
    "
  '

echo "DocuSeal webhook sync complete."
