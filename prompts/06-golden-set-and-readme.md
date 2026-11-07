# Prompt 6 — Golden set, README and wrap-up

Read `CLAUDE.md` and `docs/SPEC.md` (sections 8 and 9) first.

## Goal
Prove the answers are right, and make the repo presentable as a portfolio piece.

## Tasks
1. Read Harry's completed `golden/handcheck.csv`. Report the score and fix any systematic errors (rerun the transform and reload only the affected rows).
2. With Harry, write `golden/golden_questions.md`: 10 questions, each with the tool call(s) expected and the expected answer. Include at least:
   - a manager lookup by name with a near-duplicate name;
   - a firm with feeder funds (totals must exclude feeders);
   - a fund with an indefinite offering size;
   - a new-launch query for one state and one month;
   - a fund with no Form D file number.
   Add a `vitest` suite that replays them against local D1.
3. `README.md` for a reader who has never heard of Form D:
   - one-paragraph plain-English summary (adapt the "In plain English" section of the plan);
   - how to connect it in Claude (Settings → Connectors → Add custom connector, paste the `/mcp` URL) and three example questions;
   - an architecture diagram (Mermaid is fine): sources → nightly GitHub Action → D1 → Worker → Claude;
   - a costs section: $0/month and why (free tiers, budgets, guardrails);
   - data sources, as-of dates, limitations (no returns, VC/PE only, 3 years, manual Form ADV refresh) and the disclaimer;
   - local development commands.
4. Make sure `DECISIONS.md` is up to date, and that tests pass in CI on push.

## CHECKPOINT (stop here)
Report: hand-check score, golden test results, and a list of the deferred items from SPEC section 1, ready for Harry's next session.
