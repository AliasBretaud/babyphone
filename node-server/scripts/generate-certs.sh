#!/usr/bin/env bash
set -euo pipefail

# Generate a local trusted certificate with mkcert for given names/IPs.
# Usage:
#   scripts/generate-certs.sh                 # default: localhost + current hostname
#   scripts/generate-certs.sh 192.168.1.23    # IP
#   scripts/generate-certs.sh my-host.local   # hostname
#   scripts/generate-certs.sh 192.168.1.23 my-host.local localhost

CERT_DIR="certs"
mkdir -p "${CERT_DIR}"

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert is required but not installed."
  echo "See https://github.com/FiloSottile/mkcert"
  exit 1
fi

# Ensure local CA is installed (once)
mkcert -install

# Collect names: localhost + hostname + any args
NAMES=("localhost")
HOSTNAME_FQDN="$(hostname -f 2>/dev/null || true)"
HOSTNAME_SHORT="$(hostname 2>/dev/null || true)"

# Append unique names
append_unique() {
  local name="$1"
  for n in "${NAMES[@]}"; do
    [[ "$n" == "$name" ]] && return 0
  done
  [[ -n "$name" ]] && NAMES+=("$name")
}

append_unique "$HOSTNAME_FQDN"
append_unique "$HOSTNAME_SHORT"

# Add user-provided args
for arg in "$@"; do
  append_unique "$arg"
done

echo "Generating cert for SANs: ${NAMES[*]}"
mkcert -key-file "${CERT_DIR}/server.key" -cert-file "${CERT_DIR}/server.crt" "${NAMES[@]}"
echo "Done. Wrote:"
echo "  - ${CERT_DIR}/server.key"
echo "  - ${CERT_DIR}/server.crt"
