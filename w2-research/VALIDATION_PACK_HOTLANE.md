# Hot-lane (C) — validation pack for Eric approve

**Status:** in progress (2026-09-19 PT)  
**Goal:** draft PR + this pack ready before merge ask.

## Product decision (Eric → TY)

- US-evening W2 must be able to **reach** the decision (else vote write is pointless for nightly regime).
- Architecture: **hot-lane (C)**; not raise-TTL-only as end state.
- No quorum change in this PR.
- Related issue: #132 (leave open until approve/merge path).

## Evidence already done

| Artifact | Result |
|---|---|
| `MEMO_W2_TTL_DOMAIN.md` | 4h TTL intentional; US evening systematically expires before 04:58 |
| `MEMO_W2_MULTIMARKET_INTEGRATE.md` | Rec C; interim B/A; alert≠vote |
| `MEMO_W2_HOTLANE_REPLAY.md` | N=8 US-evening: 4h reach 2/8; hot-lane/TTL8h reach 8/8; swing ~25%; decoy ~38% |
| `#132` gold night | Hot-lane would `up:2/2` but ADX already long → **decoy** |
| `SPEC_HOTLANE_C.md` | Frozen implementable spec |
| `CODE_SEAMS_MULTIMARKET.md` | Peek vs consume_all seam |

## Pending

| Item | Owner | Status |
|---|---|---|
| `score_fires` harm/help N | Watt | in flight |
| Draft PR (hot-lane + tests) | Rex | assigned |
| Reed review | Rex requests after draft open | pending |
| Eric approve via Selina | after APPROVE | pending |

## Accept for approve (PR)

1. Implements SPEC_HOTLANE_C (admit + classify honor OR age; no early consume_all).
2. Tests cover admit keep / over-age expire / no skip-04:58.
3. No quorum / live settings changes.
4. Validation pack attached or linked in PR body (replay + score_fires when ready).

## Expect after ship (ops)

- Reach ↑ for US evenings; many nights still **decoy** when ADX already agrees.
- Discord: admit ≠ deploy.

## score_fires (Watt, 2026-09-19) — DONE

Source: `MEMO_SCORE_FIRES_HOTLANE.md`

- Method: `scripts/score_fires.py` + live rss bot bars/history; fires CSV synthesized from `monitor.log` (no alerts-sheet export).
- Overall right: **9/15**; US-evening: **6/8**.
- NEW reaches under hot-lane / TTL8h (US age>4): **help 5 / harm 1** (N=6; ~4/5 if dedupe 9/17–18 continuation).
- #132 up night: **help** on `leg_window_move` (+335) but still **decoy for deploy** (ADX already long).
- Caveat: small N; not a sheet refresh of comment ~3/10.

## Approve checklist

- [x] Spec frozen (`SPEC_HOTLANE_C.md`)
- [x] Replay QA (`MEMO_W2_HOTLANE_REPLAY.md`)
- [x] score_fires harm/help (`MEMO_SCORE_FIRES_HOTLANE.md`)
- [ ] Draft PR + tests (Rex in progress)
- [ ] Reed APPROVE
- [ ] Eric yes via Selina

## Frozen implementable spec (Watt)

- Source of truth for Rex: `SPEC_HOTLANE_C_FROZEN.md`
