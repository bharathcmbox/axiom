# Phase 5 T3: the new reviewers on real PRs, Nov 14 2026

**Question:** do the Phase 5 reviewers (code review on Claude Fable 5.2, security review on Claude Sonnet 5.5,
both with the T2 checklists) still catch the planted bug, and what do they cost? **Short answer:** yes, both caught
it, every review returned a valid checklist on the first try, and the six reviews cost $1.81.

## What ran

Both roles on three real PRs on `tiny-agenticsoc`, rebuilt as the original reviewers saw them, with
`src/axiom/drills/review_compare.py` (as in the [Phase 4 comparison](2026-11-07-reviewer-comparison.md)): the same
sandbox, prompt, read-only tools and metered model proxy as a real review, writing only to a file, so no task moved.

| PR | Why it's here |
|---|---|
| #2 SOC-002, round 1 | the planted bug: `>` where the spec says "at or above", and a test that misses the boundary |
| #7 SOC-005 | the PR where Fable 5.1 had found weak test assertions, and once failed it for them |
| #10 SOC-009 | a clean QA PR, the most expensive review in Phase 4 |

## Results

| PR | Code review, Fable 5.2 | Original code review (Fable 5.1) | Security review, Sonnet 5.5 | Original security review |
|---|---|---|---|---|
| #2 SOC-002 | **FAIL** H M, $0.24, 5 turns | FAIL | **FAIL** H L, $0.04, 4 turns | (superseded, no verdict) |
| #7 SOC-005 | **FAIL** M L, $0.53, 9 turns | PASS L | PASS, $0.04, 5 turns | (superseded, no verdict) |
| #10 SOC-009 | PASS, $0.86, 14 turns | PASS | PASS, $0.09, 5 turns | PASS |

- **The planted bug:** both reviewers named `incidents.py:157` as high and the missing boundary test. Sonnet's
  security reviewer said there was no security issue as such, and still failed the PR for the correctness bug.
- **SOC-005:** Fable 5.2 failed it for the same weak assertion Fable 5.1 found (every id check passes because each
  title contains its id), rating it `medium`. Fable 5.1 rated it `low` once and `medium` once. It's a real defect
  in a test, and a borderline verdict: Phase 4 already showed that one run of one model doesn't settle those.
- **Checklists:** every review returned a complete checklist for its role; none was rejected.
- **Cost:** $1.81 for six reviews: $1.63 on Fable 5.2 (three code reviews), $0.18 on Sonnet 5.5 (three security
  reviews). Within the plan's $1–2. The original Fable 5.1 reviews of the same six cost $2.72 ([comparison](2026-11-07-reviewer-comparison.md#per-case)).

## Checks

- Every workspace and result file (818 files) was scanned for the values in `.env`: no match.
- Three earlier attempts failed before reaching the model ($0): the launcher had been started outside `make`, so
  it lacked the Docker socket's group (`DOCKER_GID`, which the Makefile works out). Restarted with it, it worked.
  Start the launcher with `make workers`, or export `DOCKER_GID` first.
- The drill's cap counts each run's reservation ($1.50), so with a $3 cap and two runs in parallel, the third case
  waited and was run on its own. No cap was exceeded.

## What changed

The live registry now has SEC-01 on `claude-sonnet-5-5` and REV-01 on `claude-fable-5-2` (`AGENT_MODEL_CHANGED`
events). Nothing else.
