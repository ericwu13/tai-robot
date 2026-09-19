# FROZEN — Hot-lane (Option C) implementable spec for Rex

**Status:** FROZEN for Eric approve → TY assigns Rex draft PR.  
**Research owner:** Watt. **Architect gate:** TY. **Implementer:** Rex (not Watt).  
**Repo:** `ericwu13/tai-robot`  
**Date frozen:** 2026-09-19 PT  

**Companion validation:** `MEMO_SCORE_FIRES_HOTLANE.md` (score_fires N), `MEMO_W2_HOTLANE_REPLAY.md` (reach/decoy), `CODE_SEAMS_MULTIMARKET.md`.

---

## 0. Problem / success

**Problem:** Default `nightly_vote_max_age_h[W2]=4.0` + classify ~04:58 TPE drops most US-evening W2 votes (~7h age). Writing the vote is pointless for nightly regime if it never reaches.

**Success for this PR:** When a W2 vote file is written and **admitted** within ~4h of `fired_at`, nightly classify **includes** that W2 in `vote_directions` even if wall-clock age at 04:58 &gt; 4h. Non-admitted over-age W2 still expires as today. **No quorum / ADX / alert-tier product changes.**

---

## 1. Admit window

| Rule | Value |
|---|---|
| Trigger | Successful `write_regime_vote(..., source=...)` whose stem is **W2** (see §2) |
| Window | `now_tpe - fired_at ≤ admit_max_age_h` |
| Default `admit_max_age_h` | **4.0** (same number as current W2 TTL rationale; config knob below) |
| Effect | Mark vote **admitted** for its `expires_after_session` night key |
| Non-effect | Does **not** deploy; does **not** call classify; does **not** consume other stems |

Config (additive on `NewsConfig` / YAML `news:`):

```yaml
news:
  nightly_vote_max_age_h:
    W2: 4.0          # unchanged — hard age for NON-admitted
  w2_hot_lane_admit_max_age_h: 4.0   # NEW — admit window at write time
  w2_hot_lane_enabled: true          # NEW — feature flag, default false in example until ship
```

---

## 2. Storage (pick this; do not invent a second ledger)

**Preferred (minimal):** stamp on the existing sidecar JSON `regime_vote_w2.json`:

```json
{
  "version": 1,
  "source": "W2",
  "direction": "trending-up",
  "fired_at": "2026-09-17T21:32:00+08:00",
  "expires_after_session": "2026-09-17|NIGHT",
  "admitted_at": "2026-09-17T21:32:05+08:00"
}
```

- Writer sets `admitted_at = now_tpe` **iff** hot-lane enabled **and** age at write ≤ `w2_hot_lane_admit_max_age_h` (always true at first write if write is immediate).
- If write is delayed / replayed later and age already &gt; admit window → **omit** `admitted_at` (not admitted).
- Schema v1 already ignores unknown keys on older readers; still keep `version: 1` unless repo convention requires bump — follow existing `regime_vote.py` pattern.

**Stem rule:** source must remain **`W2`** (not `W2-US`). Hyphenated `W2-US` still maps to stem `W2` via `split("-")[0]` for TTL path — do not rely on hyphen for a separate bucket in this PR.

**Dedup:** existing `vote:{us_date}` first-write wins — out of scope to change; document in PR body.

---

## 3. Classify honor rules (nightly path only)

**File:** `src/live/regime_switching_runner.py` — `_read_regime_vote` (or equivalent age-drop loop).

Pseudo:

```
for each vote in read_all_regime_votes matching session key:
    age = vote.age_sec
    limit = nightly_vote_max_age_h.get(stem)  # W2 -> 4.0
    admitted = bool(vote.admitted_at) and vote.expires_after_session == sess.key
    if limit is not None and age is not None and age > limit * 3600 and not admitted:
        log expired-by-age (unchanged message + optional "not admitted")
        # still consume/delete later as today
        continue  # do NOT add to classify list
    add to classify list
consume_all_regime_votes()  # unchanged timing: only inside nightly _read_regime_vote
```

**Hard constraints:**

1. **Do not** call `_read_regime_vote` from the 30s poll / write path just to admit. Admit happens at **write** (stamp field) or a peek-only helper.
2. **Do not** stamp `last_assessed` / run `classify_session` at admit time.
3. W3/W4 behavior unchanged.
4. Admitted but wrong `expires_after_session` → do not honor.

Log when an over-age W2 is kept due to admit, e.g.  
`[REGIME-VOTE] hot-lane-admit (W2, %.1fh > %.1fh limit, admitted_at=...)`.

---

## 4. Alert ≠ vote (product; no code to “promote” alerts)

- Discord **alert-up/down** may still show `vote: -` when no file is written.
- This PR **must not** add “alert Discord ⇒ write W2”.
- Vote write stays: existing `_vote_direction` + `VOTE_MIN_SYMBOLS` + `--vote-out`.
- PR description must note: alert **symbols** can already contribute to vote direction when fresh; hot-lane does not change that.

---

## 5. Discord copy (bridge / n8n message text)

| When | Message should say | Must not say / imply |
|---|---|---|
| Vote file written + admitted | `W2 vote written (trending-…); admitted for tonight’s classify` | `deploy_long` / strategy name / “already live” |
| Vote written, not admitted | `W2 vote written (trending-…)` (status quo) | — |
| Classify kept via admit | optional bot debug only | Discord spam every 04:58 |

Wire copy in the W2 monitor / Discord payload path that already announces votes (`scripts/news_bridge/…`). Keep strings bilingual if that file already is — match local convention.

**Admit ≠ instant deploy.**

---

## 6. Tests Rex must add

| Test | Intent |
|---|---|
| `test_hot_lane_admit_stamp_on_write` | write with enabled flag → JSON has `admitted_at` |
| `test_hot_lane_no_admit_when_disabled` | flag false → no `admitted_at` |
| `test_classify_keeps_admitted_over_age` | age 7.4h, `admitted_at` set, limit 4h → vote **in** classify list |
| `test_classify_drops_over_age_without_admit` | age 7.4h, no admit → expired-by-age, **not** in list (status quo) |
| `test_admit_does_not_consume_other_stems` | W3 file remains until nightly consume |
| `test_hot_lane_does_not_classify_at_write` | write/admit does not set `last_assessed` / does not call classify |

Use existing freeze-time patterns from `tests/test_regime_vote.py` / runner vote tests. Fixture fire: **2026-09-17 21:32 TPE**, classify **2026-09-18 04:58 TPE**, age **7.4h** (issue #132 case study).

---

## 7. Out of scope (do not include in this PR)

- Changing `vote_quorum_up` / `vote_quorum_down` / ADX thresholds  
- Raising `nightly_vote_max_age_h[W2]` as the primary fix (TTL=8h is a **different** interim option, not this PR)  
- Infinite TTL  
- Alert-tier ⇒ forced vote file  
- Mid-session / second classify window  
- Soft age weights  
- Live `data/` / settings on oreopie  
- Merging / needs-ceo policy edits beyond feature flag default  

---

## 8. Validation pack (already done — attach in PR body)

### score_fires harm/help (method + N)

- **Tool:** `scripts/score_fires.py` on live bot dir + bars.  
- **Fires:** monitor-synthesized CSV (alerts sheet export **missing** on disk).  
- **Metric:** `leg_window_move` (`+` = right). Intra-session PnL **not** scored.  
- **Overall:** **9/15 (60%)** right.  
- **US-evening:** **6/8 (75%)**.  
- **NEW reaches if hot-lane/TTL8h (US age&gt;4):** help **5** / harm **1** (N=**6** raw; ~**4/5** if dedupe 9/17+9/18 continuation).  
- Detail: `MEMO_SCORE_FIRES_HOTLANE.md`.

### Replay (reach/decoy)

- US-evening reach under 4h: **2/8**; hot-lane/TTL8h: **8/8**.  
- Decoy proxy among hot-lane reaches ~**38%**; #132 = reach + decoy (ADX 41.3 already long).  
- Detail: `MEMO_W2_HOTLANE_REPLAY.md`.

---

## 9. PR acceptance checklist (Eric / TY)

- [ ] Feature flag off by default in `settings.example.yaml` (or documented enable path)  
- [ ] Tests in §6 green  
- [ ] No early `consume_all` / no classify-at-write  
- [ ] Discord copy matches §5  
- [ ] PR body cites score_fires N + decoy risk  
- [ ] No quorum changes  

**Watt non-actions:** no Rex assign from Watt; no live edits; TY assigns Rex after Eric approve.
