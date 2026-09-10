#!/bin/bash
set -euo pipefail

# Sign the Python interpreter the LaunchAgent launches with a STABLE, SHARED code
# identity, using the self-signed identity from setup_signing_cert.sh.
#
# STABLE: macOS TCC matches a grant on the code's Designated Requirement
# (identifier + signing certificate), never its cdhash. A fixed identity + cert
# therefore survives `uv sync`, venv rebuilds and Python patch bumps.
#
# SHARED: apple-reminders-server, apple-calendar-server and apple-contacts-server
# all execute the SAME uv-managed CPython build - uv hard-links identical builds,
# so every venv's python3 resolves to one file on disk. `codesign --force`
# replaces the whole signature, so if each service signed that file with its own
# identifier the last deploy would win and silently break the other two services'
# TCC grants (they only notice on their next restart). All three MUST sign with
# one identity.
#
# The shared identity keeps apple-reminders-server's original CN / bundle id
# because the live TCC grants (Reminders, Calendars, Contacts) are already pinned
# to it - renaming would force re-approving all three prompts on Malcolm's GUI.
# Override SIGNING_* only for a deliberate, coordinated migration (reset the
# grants with `tccutil reset` and re-approve).
#
# No hardened runtime on purpose: PyObjC's ad-hoc-signed extension dylibs would
# fail library validation under it, and TCC doesn't need it.

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SIGNING_IDENTITY_CN="${SIGNING_IDENTITY_CN:-awfulwoman-apple-reminders-server-signing}"
SIGNING_BUNDLE_ID="${SIGNING_BUNDLE_ID:-com.awfulwoman.apple-reminders-server}"
KEYCHAIN="${KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"

# Unlock the login keychain so codesign can reach the signing key headlessly
# (over SSH it's locked). KEYCHAIN_PASSWORD is the account login password (vault).
if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
fi

VENV_PY="$REPO_DIR/.venv/bin/python3"
if [ ! -e "$VENV_PY" ]; then
    echo "Error: $VENV_PY not found - run 'uv sync' first." >&2
    exit 1
fi

if ! security find-certificate -c "$SIGNING_IDENTITY_CN" >/dev/null 2>&1; then
    echo "Error: signing identity '$SIGNING_IDENTITY_CN' not found." >&2
    echo "Run scripts/setup_signing_cert.sh, or deploy apple-reminders-server" >&2
    echo "(its infra role provisions + trusts the shared identity)." >&2
    exit 1
fi

# The LaunchAgent execs the .venv/bin symlink (so venv resolution works), but the
# Mach-O actually loaded - and the identity TCC records - is the resolved target,
# the shared uv interpreter.
REAL_PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$VENV_PY")"

codesign --force --identifier "$SIGNING_BUNDLE_ID" --sign "$SIGNING_IDENTITY_CN" "$REAL_PY"

echo "Signed shared interpreter for TCC stability:"
echo "  launched : $VENV_PY"
echo "  real     : $REAL_PY"
echo "  identity : $SIGNING_IDENTITY_CN ($SIGNING_BUNDLE_ID)"
codesign -dv "$REAL_PY" 2>&1 | grep -E 'Identifier|Authority' || true
