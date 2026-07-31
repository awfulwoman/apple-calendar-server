#!/bin/bash
set -euo pipefail

# Trust the self-signed signing cert for code signing, headlessly.
#
# macOS requires interactive authorization for SecTrustSettingsSetTrustSettings
# (the admin-domain trust store), so `security add-trusted-cert -d` fails over SSH
# even as root ("authorization denied, no user interaction possible"). To do it
# without a GUI we briefly set the `com.apple.trust-settings.admin` authorization
# right to `allow`, add the trusted cert, then RESTORE the original right — the
# security relaxation lasts only for that one operation. Must run as root.

CERT="${1:?usage: trust_signing_cert.sh <cert.pem>}"
RIGHT="com.apple.trust-settings.admin"

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: must run as root (the infra role uses become)." >&2
    exit 1
fi

backup="$(mktemp)"
# Always restore the original authorization rule, even on error.
trap 'security authorizationdb write "$RIGHT" < "$backup" >/dev/null 2>&1 || true; rm -f "$backup"' EXIT

security authorizationdb read "$RIGHT" > "$backup" 2>/dev/null
security authorizationdb write "$RIGHT" allow >/dev/null 2>&1

security add-trusted-cert -d -r trustRoot -p codeSign \
    -k /Library/Keychains/System.keychain "$CERT"

echo "Trusted $CERT for code signing (authorization right restored)."
