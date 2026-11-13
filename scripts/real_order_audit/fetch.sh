#!/bin/bash
# usage: fetch.sh <bot_dir_name> <out_file>      (READ-ONLY on the host)
# Runs extract.ps1 on the trading PC over SSH and saves its evidence bundle.
# Runs under Git Bash on Windows (needs cygpath, iconv, base64, ssh).
# Both settings come from the environment; neither has a default:
#   export REAL_AUDIT_SSH="-F /dev/null -i ~/.ssh/<key> <user>@<host>"
#   export REAL_AUDIT_ROOT='<drive>:\<install>\tai_backtest\data\live'   # the dashboard's --base-dir
# (see the tailscale-ssh skill's hosts.local.yaml; never commit the values).
set -e
: "${REAL_AUDIT_SSH:?set REAL_AUDIT_SSH (see header)}"
: "${REAL_AUDIT_ROOT:?set REAL_AUDIT_ROOT (see header)}"
H="$(cd "$(dirname "$0")" && pwd)"
python - "$(cygpath -w "$H/extract.ps1")" "$(cygpath -w "$2.ps1")" "$1" "$REAL_AUDIT_ROOT" <<'PY'
import sys
src, dst, bot, root = sys.argv[1:5]
t = open(src, encoding="utf-8").read().replace("__BOT__", bot).replace("__ROOT__", root)
open(dst, "w", encoding="utf-8").write(t)
PY
ENC=$(iconv -f UTF-8 -t UTF-16LE "$2.ps1" | base64 -w0)
# shellcheck disable=SC2086
ssh $REAL_AUDIT_SSH -o BatchMode=yes -o ConnectTimeout=10 "powershell -NoProfile -EncodedCommand $ENC" 2>/dev/null \
  | grep -v -E '^#< CLIXML|^<Objs' > "$2"
rm -f "$2.ps1"; wc -l "$2"
