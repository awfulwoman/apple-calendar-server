#!/bin/bash
set -euo pipefail

# Create the long-lived self-signed code-signing identity that gives this
# service's Python interpreter a STABLE code identity.
#
# Why: macOS TCC grants Calendar access to a *code identity*. An unsigned /
# ad-hoc-signed interpreter is identified only by its cdhash, which changes on
# every `uv sync`, venv rebuild, or Python patch bump — so the grant silently
# reverts to "denied" and the headless LaunchAgent can never re-prompt. Signing
# with a fixed certificate + identifier makes TCC match on the Designated
# Requirement instead, so the grant survives rebuilds. See sign_runtime.sh.
#
# Run this ONCE on the machine that runs the service (Malcolm), as the user the
# LaunchAgent runs as (so the key lands in that user's login keychain). The
# private key never leaves the keychain. Idempotent: a no-op if it already exists.
#
# Automation (infra): pass KEYCHAIN_PASSWORD (e.g. from Ansible vault) so the
# key partition list is set non-interactively. Without it, the first codesign
# use will show a one-time keychain prompt — click "Always Allow".

IDENTITY_CN="awfulwoman-apple-calendar-server-signing"
KEYCHAIN="${KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"
VALID_DAYS="${VALID_DAYS:-3650}"

if security find-certificate -c "$IDENTITY_CN" "$KEYCHAIN" >/dev/null 2>&1; then
    echo "Signing identity '$IDENTITY_CN' already present in $KEYCHAIN — nothing to do."
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/openssl.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions    = v3
prompt             = no
[dn]
CN = $IDENTITY_CN
[v3]
basicConstraints   = critical,CA:false
keyUsage           = critical,digitalSignature
extendedKeyUsage   = critical,codeSigning
CNF

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$tmp/key.pem" -out "$tmp/cert.pem" \
    -days "$VALID_DAYS" -config "$tmp/openssl.cnf" 2>/dev/null

openssl pkcs12 -export -inkey "$tmp/key.pem" -in "$tmp/cert.pem" \
    -out "$tmp/identity.p12" -passout pass:

# -T /usr/bin/codesign: let codesign use this key.
security import "$tmp/identity.p12" -k "$KEYCHAIN" -P "" -T /usr/bin/codesign

# Let codesign reach the private key without an interactive prompt each run.
if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security set-key-partition-list -S apple-tool:,apple:,codesign: \
        -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
else
    echo "NOTE: KEYCHAIN_PASSWORD not set — skipped set-key-partition-list."
    echo "      The first codesign use will prompt once; click 'Always Allow'."
fi

echo "Created self-signed code-signing identity '$IDENTITY_CN' (valid ${VALID_DAYS} days)."
echo "Next: run scripts/sign_runtime.sh (install_service.sh does this for you)."
