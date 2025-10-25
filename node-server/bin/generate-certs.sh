#!/usr/bin/env sh
set -eu

CERT_DIR="${CERT_DIR:-/app/certs}"
HOSTS="${CERT_HOSTNAMES:-localhost 127.0.0.1}"
KEY="${CERT_DIR}/server.key"
CRT="${CERT_DIR}/server.crt"
CFG="${CERT_DIR}/openssl.cnf"

mkdir -p "${CERT_DIR}"

if [ -f "${KEY}" ] && [ -f "${CRT}" ]; then
  echo "TLS certs already present in ${CERT_DIR}. Skipping generation."
  exit 0
fi

echo "Generating self-signed cert for hosts: ${HOSTS}"

DNS_INDEX=1
IP_INDEX=1
ALT_NAMES=""
for h in $HOSTS; do
  if echo "$h" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    ALT_NAMES="${ALT_NAMES}\nIP.${IP_INDEX} = ${h}"
    IP_INDEX=$((IP_INDEX+1))
  else
    ALT_NAMES="${ALT_NAMES}\nDNS.${DNS_INDEX} = ${h}"
    DNS_INDEX=$((DNS_INDEX+1))
  fi
done

cat > "${CFG}" <<EOF
[req]
default_bits       = 2048
distinguished_name = req_distinguished_name
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
CN = BabyPhone Local

[v3_req]
subjectAltName = @alt_names

[alt_names]
$(printf "${ALT_NAMES}")
EOF

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout "${KEY}" -out "${CRT}" -config "${CFG}" >/dev/null 2>&1

chmod 600 "${KEY}"
echo "Generated: ${KEY} and ${CRT}"
