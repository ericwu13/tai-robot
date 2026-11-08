#!/bin/bash
# usage: fetch.sh <bot_dir_name> <out_file> [install]      (READ-ONLY on the host)
# Runs extract.ps1 on the trading PC over SSH and saves its evidence bundle.
# REAL_AUDIT_SSH must hold the ssh target + options, e.g.
#   export REAL_AUDIT_SSH="-F /dev/null -i ~/.ssh/<key> <user>@<tailscale-ip>"
# (see the tailscale-ssh skill's hosts.local.yaml; never commit the values).
# install defaults to tai_robot_v21916; bots live in E:\<install>\tai_backtest\data\live
set -e
: "${REAL_AUDIT_SSH:?set REAL_AUDIT_SSH (see header)}"
INST="${3:-tai_robot_v21916}"
H="$(cd "$(dirname "$0")" && pwd)"
ROOT='E:\'"$INST"'\tai_backtest\data\live'
python - "$(cygpath -w "$H/extract.ps1")" "$(cygpath -w "$2.ps1")" "$1" "$ROOT" <<'PY'
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
