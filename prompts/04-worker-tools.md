# Prompt 4 — Build the MCP Worker and the four tools

Read `CLAUDE.md` and `docs/SPEC.md` (section 7 especially) first.

## Goal
A stateless MCP server on a Cloudflare Worker exposing `resolve_manager`, `list_funds`, `fund_history` and `new_launches`, reading from D1.

## Tasks
1. Look up the **current** Cloudflare docs for the stateless `createMcpHandler` (Agents SDK) and the matching MCP SDK package and version. Use that; don't use `McpAgent` or Durable Objects. Create a new server instance per request, as the docs require for stateless servers. Serve at `/mcp`. Record the versions in `DECISIONS.md`.
2. Implement the four tools in `worker/src/tools/`, one file each:
   - zod input schemas; `title` and `annotations: { readOnlyHint: true }` on every tool;
   - clear tool descriptions that say what's in and out of scope (no returns, VC/PE only, last 3 years of Form D);
   - prepared statements only; each query uses an index from SPEC section 4 and `LIMIT ≤ 25`;
   - the response envelope from SPEC section 7, including sources, `as_of` (from `meta`) and the disclaimer.
3. Add a small guard in the Worker: reject `limit` values above 25 and keep each request to a few queries (stay well under the 10 ms CPU limit).
4. Tests (`vitest`, with Cloudflare's Workers test integration or local D1 and a fixture DB): one happy path per tool, one empty result, one bad input. Assert `rows_read` stays ≤ 1,000 per query.
5. Run `npx wrangler dev` and check the server with the MCP Inspector (`npx @modelcontextprotocol/inspector`) against `http://localhost:8787/mcp`: list tools, call each one.

## CHECKPOINT (stop here)
Report: test results, the Inspector output for each tool (a trimmed sample), the largest `rows_read` seen, and any tool descriptions Harry should review for wording.
