# Prompt 5 — Deploy and set up the nightly update

Read `CLAUDE.md` and `docs/SPEC.md` first.

## Goal
The Worker is live on `workers.dev`, and a GitHub Action adds new Form D filings every night within the free D1 budget.

## Tasks
1. Deploy with `npx wrangler deploy`. Confirm the `/mcp` endpoint responds, and print the URL for Harry.
2. `etl/src/etl/incremental.py`: find Form D filings filed after `meta.as_of_form_d` (EDGAR daily index; verify its layout first), download only those filings' XML via `sec_client`, apply the SPEC section 3 filters, and write only **new rows** as SQL chunks. Advance `meta.as_of_form_d`. Unit-test the parsing with one real saved filing as a fixture.
3. `.github/workflows/nightly.yml`:
   - triggers: `workflow_dispatch` and a daily `schedule` (pick a time after 00:00 UTC, e.g. 06:30 UTC);
   - standard `ubuntu-latest` runner only; set up uv and Node; cache `data/raw/` if helpful;
   - runs `incremental`, then `load --remote` with today's budget;
   - also finishes any unfinished initial-load chunks;
   - secrets: `SEC_USER_AGENT`, `CLOUDFLARE_API_TOKEN` (scoped to D1 edit + Workers), `CLOUDFLARE_ACCOUNT_ID`. List exactly which token permissions Harry must grant.
4. Form ADV refresh: leave it manual for v1 (Harry downloads a new file monthly). Document the steps in the README.

## CHECKPOINT (stop here)
Report: the live URL, the token permissions to create, the secrets to add in GitHub, and how to trigger the workflow manually. After Harry triggers it, check the run log together.
