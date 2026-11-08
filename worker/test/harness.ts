// Shared test setup: runs the real Worker locally (wrangler test harness, local D1 only, never remote)
// and loads schema.sql + the fixture rows into its D1.
import { readFileSync } from "node:fs";
import { createTestHarness, type TestHarness } from "wrangler";
import { Db } from "../src/lib/db";

const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");

function statements(sql: string): string[] {
  return sql
    .split("\n")
    .filter((l) => !l.trim().startsWith("--"))
    .join("\n")
    .split(/;\s*\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export interface Ctx {
  server: TestHarness;
  d1: D1Database;
  /** A fresh query wrapper; check .stats for rows_read after a tool call. */
  db(): Db;
}

/** Start the Worker with schema.sql plus `seed` (SQL texts; default: the tool-test fixtures) in its local D1. */
export async function start(seed?: string[]): Promise<Ctx> {
  const server = createTestHarness({
    workers: [{ configPath: new URL("../wrangler.jsonc", import.meta.url) }],
  });
  await server.listen();
  const env = await server.getWorker<{ DB: D1Database }>().getEnv();
  const d1 = env.DB;
  const texts = [read("../schema.sql"), ...(seed ?? [read("./fixtures/seed_etl.sql"), read("./fixtures/synthetic.sql")])];
  // Batches of 1,000 statements, so a full fund.db export (230k rows) loads too.
  const sql = texts.flatMap(statements);
  for (let i = 0; i < sql.length; i += 1000) await d1.batch(sql.slice(i, i + 1000).map((s) => d1.prepare(s)));
  return { server, d1, db: () => new Db(d1) };
}
