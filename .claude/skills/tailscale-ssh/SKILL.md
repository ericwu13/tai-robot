---
name: tailscale-ssh
description: SSH to Tailscale hosts (Steven, oreopie, other tailnet nodes). Use when an agent needs remote shell on a trading PC over Tailscale. Credentials and the filled hosts map stay local — never commit keys or passwords.
---

# Tailscale SSH

Reach Windows hosts on the Tailscale tailnet with OpenSSH.

**Never commit private keys, passwords, or `hosts.local.yaml`.**

## Live pattern (prefer this)

```powershell
ssh -i $env:USERPROFILE\.ssh\id_ed25519_steven -o BatchMode=yes -o ConnectTimeout=10 Traderbot@100.74.218.42 "<remote command>"
```

| Field | Notes |
|---|---|
| User | Windows account on the target (Steven: `Traderbot`) |
| Addr | Tailscale CGNAT `100.x` IP — MagicDNS optional |
| Key | Named per-hop under `%USERPROFILE%\.ssh\` (e.g. `id_ed25519_steven`); **not** the GitHub `id_ed25519` |
| Client | Windows OpenSSH |

`tailscale` CLI may be absent from PATH; the `100.x` IP is enough when Tailscale is up.

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
