#!/bin/bash
set -euo pipefail

# Create the long-lived self-signed code-signing identity that gives the Python
# interpreter a STABLE, SHARED code identity.
#
# Why: macOS TCC grants EventKit access (Reminders / Calendars / Contacts) to a
# *code identity*. An unsigned / ad-hoc-signed interpreter is identified only by
# its cdhash, which changes on every `uv sync`, venv rebuild, or Python patch
# bump - so the grant silently reverts to "denied" and the headless LaunchAgent
# can never re-prompt. Signing with a fixed certificate + identifier makes TCC
# match on the Designated Requirement instead, so the grant survives rebuilds.
#
# SHARED across apple-reminders-server, apple-calendar-server and
# apple-contacts-server: all three exec the SAME uv-managed CPython file, and
# `codesign --force` replaces the whole signature, so they must all sign it with
# ONE identity or each deploy breaks the others. apple-reminders-server owns
# provisioning + trusting this identity (its infra role runs first); the other
# two just call sign_runtime.sh. This keeps the original apple-reminders-server
# CN so the live TCC grants stay valid without re-approval.
#
# Run ONCE on the machine that runs the services, as the user the LaunchAgents
# run as (so the key lands in that user's login keychain). The private key never
# leaves the keychain. Idempotent: a complete identity is kept, never
# regenerated (a new cert would change the DR and break every grant).
#
# A self-signed cert must also be TRUSTED for code signing before codesign will
# use it - that needs admin rights, so it's a SEPARATE step (the infra role does
# it with `sudo security add-trusted-cert`). This script persists the public cert
# to SIGNING_CERT_STORE so that step can find it.
#
# KEYCHAIN_PASSWORD (the account login password, from vault) is required for a
# headless run: over SSH the login keychain is locked, so importing key material
# fails with "User interaction is not allowed" unless we unlock it first. It's
# also used for set-key-partition-list.

SIGNING_IDENTITY_CN="${SIGNING_IDENTITY_CN:-awfulwoman-apple-reminders-server-signing}"
KEYCHAIN="${KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"
SIGNING_CERT_STORE="${SIGNING_CERT_STORE:-$HOME/.local/state/apple-reminders-server/signing-cert.pem}"
VALID_DAYS="${VALID_DAYS:-3650}"

mkdir -p "$(dirname "$SIGNING_CERT_STORE")"

# Unlock first - over SSH (no GUI login) the keychain is locked and any operation
# touching private-key material would fail with "User interaction is not allowed".
if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
fi

# A COMPLETE identity (cert+key) already present? Keep it - never regenerate.
if security find-identity -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$SIGNING_IDENTITY_CN"; then
    [ -f "$SIGNING_CERT_STORE" ] || security find-certificate -c "$SIGNING_IDENTITY_CN" -p "$KEYCHAIN" > "$SIGNING_CERT_STORE"
    echo "Signing identity '$SIGNING_IDENTITY_CN' already present in $KEYCHAIN - nothing to do."
    exit 0
fi

# Clear any orphaned cert/key from a prior partial run so the import below is clean.
security delete-identity -c "$SIGNING_IDENTITY_CN" "$KEYCHAIN" >/dev/null 2>&1 || true
security delete-certificate -c "$SIGNING_IDENTITY_CN" "$KEYCHAIN" >/dev/null 2>&1 || true

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/openssl.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions    = v3
prompt             = no
[dn]
CN = $SIGNING_IDENTITY_CN
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
cp "$tmp/cert.pem" "$SIGNING_CERT_STORE"
chmod 644 "$SIGNING_CERT_STORE"

# Let codesign reach the private key without an interactive prompt each run.
if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security set-key-partition-list -S apple-tool:,apple:,codesign: \
        -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
else
    echo "NOTE: KEYCHAIN_PASSWORD not set - skipped unlock + set-key-partition-list."
    echo "      Run this in a GUI session, or codesign will prompt/fail headlessly."
fi

echo "Created self-signed code-signing identity '$SIGNING_IDENTITY_CN' (valid ${VALID_DAYS} days)."
echo "Public cert stored at $SIGNING_CERT_STORE."
echo "NEXT: it must be trusted for code signing before codesign will use it -"
echo "  sudo security add-trusted-cert -d -r trustRoot -p codeSign \\"
echo "    -k /Library/Keychains/System.keychain $SIGNING_CERT_STORE"
echo "(the infra role does this automatically). Then run scripts/sign_runtime.sh."
