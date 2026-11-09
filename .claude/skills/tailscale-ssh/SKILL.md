---
name: tailscale-ssh
description: SSH to Tailscale hosts (Steven = the live trading PC, oreopie, other tailnet nodes). Use when an agent needs a remote shell or file on a trading PC — "ssh to Steven", "run this on Steven", "check the bots/bridge on Steven", "restart the dashboard", "read a Steven log". Credentials and the filled hosts map stay local — never commit keys or passwords.
---

# Tailscale SSH

Reach Windows hosts on the Tailscale tailnet with OpenSSH.

**Never commit private keys, passwords, or `hosts.local.yaml`.**

## Live pattern (prefer this)

```powershell
ssh -i $env:USERPROFILE\.ssh\id_ed25519_steven -o BatchMode=yes -o ConnectTimeout=10 Traderbot@100.x.x.x "<remote command>"
```

The real address is `tailscale_ip` in gitignored `hosts.local.yaml` (or `%USERPROFILE%\.ssh\tailscale-hosts.local.yaml`).

| Field | Notes |
|---|---|
| User | Windows account on the target (Steven: `Traderbot`) |
| Addr | Tailscale CGNAT `100.x` IP — MagicDNS optional |
| Key | Named per-hop under `%USERPROFILE%\.ssh\` (e.g. `id_ed25519_steven`); **not** the GitHub `id_ed25519` |
| Client | Windows OpenSSH |

`tailscale` CLI may be absent from PATH; the `100.x` IP is enough when Tailscale is up.

From git-bash / the Bash tool:

```bash
ssh -F /dev/null -i ~/.ssh/id_ed25519_steven -o UserKnownHostsFile=~/.ssh/known_hosts \
    -o BatchMode=yes -o ConnectTimeout=10 Traderbot@<Steven tailnet IP> "<cmd>"
```

## Gotchas (Steven)

- git-bash ssh rejects the user's `~/.ssh/config` (UTF-8 BOM) — pass `-F /dev/null`.
- The remote shell is **cmd**: pipes and quoting in an inline command break. For
  anything non-trivial send `powershell -NoProfile -EncodedCommand <base64 UTF-16LE>`
  and drop the trailing `#< CLIXML` noise. A piped `-Command -` script runs line by
  line — keep each statement on one line.
- Console output is cp950 unless the script sets UTF-8.
- Processes started from the ssh session die when it disconnects. Start
  long-running things via a scheduled task (or WMI `Win32_Process.Create`).
- Never stop processes by image name (`tai_backtest.exe` = every bot; the dashboard
  is a venv python pair) — stop by PID.
- Never move the HEAD of `C:\Users\Traderbot\tai-robot-src` (bridge tasks) or
  `C:\Users\Traderbot\tai-robot-dashboard` (dashboard + W3). Use a new worktree.
- What runs where on Steven → `start-here`.

## Local hosts file

1. `%USERPROFILE%\.ssh\tailscale-hosts.local.yaml`
2. `.claude/skills/tailscale-ssh/hosts.local.yaml` (this checkout)

Copy from `hosts.local.example.yaml`. Do not invent values if missing.

## Optional Host alias

`~/.ssh/config` Host stanzas are convenience only. Default invoke is explicit `-i` + IP + user.

## Safety

- Named keys per hop; never reuse the GitHub key for hosts.
- Never `git add` `hosts.local.yaml`, private keys, pfx, or Capital passwords.
- Never print private key bytes; pubkey comment/fingerprint only if needed.
