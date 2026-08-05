#!/bin/bash
# Wöchentlicher Redeploy der Pegelstände-Cloudflare-Pages-Seite.
# Holt den aktuellen Stand aus dem GitHub-Repo (dort aktualisiert GitHub Actions
# täglich die Live-Daten) und deployed docs/ neu zu Cloudflare Pages.
#
# Erwartet eine .env-Datei im selben Verzeichnis mit:
#   CLOUDFLARE_API_TOKEN=...   (Account API Token, Permission "Cloudflare Pages: Edit")
#   CLOUDFLARE_ACCOUNT_ID=...  (nötig, da Account-Tokens keine Nutzer-Memberships haben)
#
# NODE_OPTIONS=--dns-result-order=ipv4first ist nötig, wenn der Host eine
# IPv6-Adresse hat, die nicht im Token-IP-Filter freigegeben ist (Cloudflares
# API-Client verbindet sonst per Happy-Eyeballs bevorzugt über IPv6).
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
  echo "CLOUDFLARE_API_TOKEN fehlt (siehe .env)" >&2
  exit 1
fi

git pull --quiet origin main
NODE_OPTIONS='--dns-result-order=ipv4first' wrangler pages deploy docs --project-name pegelstaende --branch main --commit-dirty=true
