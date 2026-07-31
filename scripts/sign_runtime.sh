#!/bin/bash
set -euo pipefail

# Sign the Python interpreter the LaunchAgent launches with a STABLE code
# identifier, using the self-signed identity from setup_signing_cert.sh.
#
# Because TCC matches on (identifier + certificate) rather than the interpreter's
# cdhash, the Calendar grant survives `uv sync`, venv rebuilds and Python patch
# bumps. Re-run this on EVERY deploy (the infra role does) so a freshly-rebuilt
# venv is re-signed with the same identity and keeps the existing TCC grant.
#
# No hardened runtime (no --options runtime) on purpose: PyObjC's ad-hoc-signed
# extension dylibs would fail library validation under it, and TCC doesn't need
# it — a valid, stable signature is enough.

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IDENTITY_CN="awfulwoman-apple-calendar-server-signing"
BUNDLE_ID="com.awfulwoman.apple-calendar-server"

VENV_PY="$REPO_DIR/.venv/bin/python3"
if [ ! -e "$VENV_PY" ]; then
    echo "Error: $VENV_PY not found — run 'uv sync' first." >&2
    exit 1
fi

if ! security find-certificate -c "$IDENTITY_CN" >/dev/null 2>&1; then
    echo "Error: signing identity '$IDENTITY_CN' not found." >&2
    echo "Run scripts/setup_signing_cert.sh first." >&2
    exit 1
fi

# The LaunchAgent execs the .venv/bin symlink (so venv resolution works), but the
# Mach-O actually loaded — and the identity TCC records — is the resolved target.
REAL_PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$VENV_PY")"

codesign --force --identifier "$BUNDLE_ID" --sign "$IDENTITY_CN" "$REAL_PY"

echo "Signed interpreter for TCC stability:"
echo "  launched : $VENV_PY"
echo "  identity : $REAL_PY"
codesign -dv "$REAL_PY" 2>&1 | grep -E 'Identifier|Authority' || true
