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
# A self-signed cert must also be TRUSTED for code signing before codesign will
# use it — that requires admin rights, so it's a SEPARATE step (the infra role
# does it with `sudo security add-trusted-cert`; see the note printed at the end).
# This script persists the public cert to CERT_STORE so that step can find it.
#
# Automation (infra): pass KEYCHAIN_PASSWORD (e.g. from Ansible vault) so the
# key partition list is set non-interactively. Without it, the first codesign
# use will show a one-time keychain prompt — click "Always Allow".

IDENTITY_CN="awfulwoman-apple-calendar-server-signing"
KEYCHAIN="${KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"
CERT_STORE="${CERT_STORE:-$HOME/.local/state/apple-calendar-server/signing-cert.pem}"
VALID_DAYS="${VALID_DAYS:-3650}"

mkdir -p "$(dirname "$CERT_STORE")"

if security find-certificate -c "$IDENTITY_CN" "$KEYCHAIN" >/dev/null 2>&1; then
    # Make sure the public cert is on disk for the trust step, even on re-runs.
    [ -f "$CERT_STORE" ] || security find-certificate -c "$IDENTITY_CN" -p "$KEYCHAIN" > "$CERT_STORE"
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

# Import the cert and (unencrypted) private key straight from PEM. We deliberately
# avoid a PKCS12 bundle: OpenSSL 3 exports an empty-password .p12 whose MAC macOS's
# `security import` rejects ("MAC verification failed during PKCS12 import"). macOS
# forms a code-signing identity from the matching cert+key in one keychain.
# -T /usr/bin/codesign: let codesign use the imported key.
security import "$tmp/cert.pem" -k "$KEYCHAIN" -T /usr/bin/codesign
security import "$tmp/key.pem"  -k "$KEYCHAIN" -T /usr/bin/codesign

# Persist the PUBLIC cert so the role's admin-domain trust step can reference it.
# (The private key never leaves the keychain.)
cp "$tmp/cert.pem" "$CERT_STORE"
chmod 644 "$CERT_STORE"

# Let codesign reach the private key without an interactive prompt each run.
if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security set-key-partition-list -S apple-tool:,apple:,codesign: \
        -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
else
    echo "NOTE: KEYCHAIN_PASSWORD not set — skipped set-key-partition-list."
    echo "      The first codesign use will prompt once; click 'Always Allow'."
fi

echo "Created self-signed code-signing identity '$IDENTITY_CN' (valid ${VALID_DAYS} days)."
echo "Public cert stored at $CERT_STORE."
echo "NEXT: it must be trusted for code signing before codesign will use it —"
echo "  sudo security add-trusted-cert -d -r trustRoot -p codeSign \\"
echo "    -k /Library/Keychains/System.keychain $CERT_STORE"
echo "(the infra role does this automatically). Then run scripts/sign_runtime.sh."
