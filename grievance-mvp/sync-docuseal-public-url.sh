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

if [[ -z "${DOCUSEAL_PROTOCOL:-}" || -z "${DOCUSEAL_HOST:-}" ]]; then
  echo "ERROR: DOCUSEAL_PROTOCOL and DOCUSEAL_HOST must be set in $ENV_FILE" >&2
  exit 1
fi

PUBLIC_URL="${DOCUSEAL_PROTOCOL}://${DOCUSEAL_HOST}"
echo "Syncing DocuSeal APP_URL to: $PUBLIC_URL"

docker compose up -d docuseal >/dev/null

docker compose exec -T \
  -e DOCUSEAL_PUBLIC_URL="$PUBLIC_URL" \
  docuseal sh -lc '
    cd /app
    bundle exec rails runner "
      url = ENV.fetch(\"DOCUSEAL_PUBLIC_URL\")
      accounts = Account.order(:id)
      if accounts.none?
        puts \"No accounts found yet; nothing to update\"
      else
        accounts.each do |account|
          rec = EncryptedConfig.find_or_initialize_by(account_id: account.id, key: EncryptedConfig::APP_URL_KEY)
          rec.value = url
          rec.save!
        end
        Docuseal.refresh_default_url_options!
        puts \"Updated app_url for #{accounts.count} account(s)\"
      end
      puts \"effective_default_url_options=#{Docuseal.default_url_options.inspect}\"
    "
  '

