# ADR-009: Single-Cycle Review in the Implement Lifecycle

**Date:** 2026-04-12
**Status:** Accepted

## Context

The implement lifecycle originally ran a multi-round review/fix loop: after the
initial implementation, the bot reviewed the PR, requested changes, the
implementer fixed them, the bot re-reviewed, and so on up to
`MAX_FIX_ITERATIONS` (3 fix rounds, 4 total review rounds).

PR #76 exposed the failure mode. Over 8 review rounds (4 from the bot
implementer, 4 from manual `/review` triggers), the reviewer:

- **Rounds 1–4:** Found 5 genuine bugs — over-strict mergeability gating,
  premature mergeability cutoff, unpaginated check-run scan, unhandled API
  errors, and a leaked runtime artifact. Each was a real issue introduced or
  exposed by the previous fix. This was valuable.
- **Rounds 5–7:** Flip-flopped on a design question (should CI status gate
  merging?). Round 5 said "don't treat CI as authoritative," round 7 said "CI
  failure IS terminal — stop immediately." These rounds consumed tokens and time
  without converging.

The root cause is that LLM-powered reviewers are good at spotting concrete bugs
but bad at holding stable design opinions across rounds. Each fix changes
100–250 lines of code, creating new surface area for the reviewer to find new
(often contradictory) opinions about. The review/fix loop amplifies this
instability instead of damping it.

## Options Considered

### A. Keep multi-round loop (status quo)

- Pros: More chances to catch bugs; fixes can be verified by re-review.
- Cons: Doesn't converge — reviewer contradicts itself across rounds. 53-minute
  run times, 8+ premium requests per issue. Each fix introduces new code surface
  that triggers new findings. Drip-feeding despite skill instructions to
  front-load.
- **Verdict:** Rejected. The theoretical benefit of convergence doesn't hold in
  practice. The loop amplifies instability.

### B. Single cycle: implement → review → fix → merge

- Pros: Predictable cost (2–3 premium requests). Forces the reviewer to
  front-load all findings. Forces the implementer to get it right in one pass.
  Eliminates the contradictory-feedback failure mode.
- Cons: Real bugs found in the fix round won't be caught by a re-review. The
  fix itself could introduce regressions.
- **Verdict:** Accepted. The fix-round regression risk is mitigated by the
  implementer's self-review checklist and `mise run ci`. The cost of missed
  bugs is lower than the cost of non-convergent loops.

### C. Advisory-only review (no fix round)

- Pros: Simplest. Fastest.
- Cons: Throws away the value of rounds 1–4 from PR #76 — the reviewer did
  catch real bugs that the fix round addressed.
- **Verdict:** Rejected. One fix round is worth the cost. Zero is wasteful of
  good feedback.

## Decision

The implement lifecycle runs exactly one cycle:

1. **Implement** — Copilot CLI writes the code
2. **Review** — single round, catches catastrophic bugs
3. **Fix** — one pass if the review requested changes (no re-review)
4. **Merge** — auto-merge via GitHub API

The review is mandatory but advisory in the sense that review failure doesn't
block merge. If the review errors out (API failure, timeout), the lifecycle
proceeds to merge. If the review requests changes but provides no inline
findings or the session can't be resumed, the lifecycle skips the fix and
merges anyway.

Both skills (`bot-review`, `bot-implement`) are updated to make the single-cycle
contract explicit. The reviewer knows there's one shot — it must front-load
everything.

## Known Risks

- **Fix-round regressions ship uncaught.** The fix could introduce new bugs
  that a re-review would have caught. Mitigated by: the implementer's
  self-review checklist, `mise run ci`, and the fact that a human can still
  `/review` the PR manually before or after merge.
- **Reviewer may not front-load.** Despite the skill saying "one round only,"
  the model may still hold back findings. This is a model behaviour issue, not
  an architecture issue — monitor and adjust the skill prompt if needed.

## References

- PR #76: 8-round review cycle that motivated this change
- PR #78: Implementation of single-cycle flow
- `stacks/agents/app/implement.py`: Lifecycle orchestrator
- `.github/skills/bot-review/SKILL.md`: Review skill with single-round language
- `.github/skills/bot-implement/SKILL.md`: Implement skill with one-cycle contract
