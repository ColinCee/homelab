# ADR-009: Capped Review Cycle in the Implement Lifecycle

**Date:** 2026-04-12
**Status:** Accepted (updated from single-cycle to 2-round cap)

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
  front-load all findings. Eliminates the contradictory-feedback failure mode.
- Cons: Real bugs found in the fix round won't be caught by a re-review. The
  fix itself could introduce regressions. One round often isn't enough — the
  reviewer catches real bugs that only surface after the first fix.
- **Verdict:** Rejected (initially accepted, then revised). One round turned out
  to be too aggressive — fix-round regressions shipped unchecked.

### C. Advisory-only review (no fix round)

- Pros: Simplest. Fastest.
- Cons: Throws away the value of rounds 1–4 from PR #76 — the reviewer did
  catch real bugs that the fix round addressed.
- **Verdict:** Rejected. One fix round is worth the cost. Zero is wasteful of
  good feedback.

### D. Capped at 2 rounds: implement → review → fix → review → fix → merge

- Pros: Catches fix-round regressions without unlimited looping. The second
  review has full memory (session resume + prior threads). 2 rounds hit the
  sweet spot from PR #76 data: rounds 1–4 found real bugs, divergence started
  at round 5. With session resume providing memory, 2 rounds should capture
  most value without the contradiction spiral.
- Cons: Costs 1–2 more premium requests than single-cycle. Still possible for
  the reviewer to contradict itself, but the cap prevents runaway loops.
- **Verdict:** Accepted. 2 rounds capture the value of re-reviewing fixes
  without the unbounded divergence of the original multi-round loop.

## Decision

The implement lifecycle runs up to 2 review/fix cycles:

1. **Implement** — Copilot CLI writes the code
2. **Review round 1** — catches bugs, security issues, breaking changes
3. **Fix round 1** — if the review requested changes
4. **Review round 2** — catches regressions from the fix (if round 1 had fixes)
5. **Fix round 2** — if round 2 requests changes (final fix, no re-review)
6. **Merge** — auto-merge via GitHub API

If any review approves or errors out, the loop breaks early and proceeds to
merge. If a fix produces no changes, the loop breaks. The cap prevents the
contradiction spiral observed in PR #76 rounds 5–7.

The review is mandatory but advisory in the sense that review failure doesn't
block merge. If the review errors out (API failure, timeout), the lifecycle
proceeds to merge. If the review requests changes but provides no inline
findings or the session can't be resumed, the lifecycle skips the fix and
merges anyway.

Both the reviewer and implementer sessions resume across rounds — the reviewer
sees its own prior comments (via session resume + unresolved threads), and the
implementer has full conversation history for context-aware fixes.

## Known Risks

- **Round 2 contradiction.** The reviewer may still contradict round 1 findings
  in round 2. Mitigated by: session resume giving the reviewer memory of what
  it said, and the hard cap preventing a spiral.
- **Cost increase.** 2 rounds costs 1–2 more premium requests than single-cycle.
  Acceptable given the bug-catching value demonstrated by PR #76 rounds 1–4.
- **Reviewer may not front-load.** Despite the skill saying "at most 2 rounds,"
  the model may still hold back findings. This is a model behaviour issue, not
  an architecture issue — monitor and adjust the skill prompt if needed.

## References

- PR #76: 8-round review cycle that motivated the original single-cycle decision
- PR #78: Implementation of capped review cycle
- `stacks/agents/app/implement.py`: Lifecycle orchestrator (MAX_REVIEW_ROUNDS)
- `.github/skills/bot-review/SKILL.md`: Review skill with 2-round language
- `.github/skills/bot-implement/SKILL.md`: Implement skill with capped-cycle contract
