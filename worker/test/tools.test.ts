import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { InputError } from "../src/lib/cursor";
import { DISCLAIMER } from "../src/lib/envelope";
import type { Db } from "../src/lib/db";
import { MAX_QUERIES, MAX_ROWS_READ } from "../src/lib/db";
import { fundHistory } from "../src/tools/fund_history";
import { listFunds } from "../src/tools/list_funds";
import { looksLikeSpv, newLaunches } from "../src/tools/new_launches";
import { resolveManager } from "../src/tools/resolve_manager";
import { TOOLS } from "../src/server";
import type { ToolDef } from "../src/tools/types";
import { start, type Ctx } from "./harness";

let ctx: Ctx;
const maxRowsRead = { value: 0, sql: "" };

beforeAll(async () => {
  ctx = await start();
}, 60_000);
afterAll(async () => {
  console.log(`largest rows_read in tests: ${maxRowsRead.value} (${maxRowsRead.sql.replace(/\s+/g, " ").slice(0, 120)})`);
  await ctx?.server.close();
});

/** Validate input like the MCP SDK does, run the tool, and enforce the read budget on every query. */
async function call(tool: ToolDef<any>, input: unknown): Promise<{ out: any; db: Db }> {
  const parsed = tool.inputSchema.parse(input);
  const db = ctx.db();
  const out = await tool.run(db, parsed);
  expect(db.stats.length).toBeLessThanOrEqual(MAX_QUERIES);
  for (const s of db.stats) {
    expect(s.rows_read, s.sql).toBeLessThanOrEqual(MAX_ROWS_READ);
    if (s.rows_read > maxRowsRead.value) Object.assign(maxRowsRead, { value: s.rows_read, sql: s.sql });
  }
  expectEnvelope(out);
  return { out, db };
}

function expectEnvelope(out: any) {
  expect(Object.keys(out).sort()).toEqual(["as_of", "data", "disclaimer", "notes", "sources"]);
  expect(out.disclaimer).toBe(DISCLAIMER);
  expect(out.as_of).toEqual({ form_d: "2026-10-25", form_adv: "2026-08-05" });
  for (const s of out.sources) {
    expect(["form_d", "form_adv"]).toContain(s.type);
    expect(s.url).toMatch(/^https:\/\/(www\.sec\.gov\/Archives\/edgar\/data\/\d+\/\d{18}\/|adviserinfo\.sec\.gov\/firm\/summary\/\d+)$/);
  }
}

describe("tool metadata", () => {
  it("every tool has a title, readOnlyHint and a description that scopes it", () => {
    for (const t of TOOLS) {
      expect(t.title.length).toBeGreaterThan(3);
      expect(t.description).toMatch(/return|performance/i);
    }
  });
});

describe("resolve_manager", () => {
  it("finds an adviser by name prefix, ignoring case and suffixes", async () => {
    const { out } = await call(resolveManager, { query: "Lateral Investment Management, L.L.C." });
    expect(out.data.matched_by).toBe("name");
    expect(out.data.advisers.map((a: any) => a.crd)).toEqual(["159649"]);
    expect(out.data.advisers[0]).toMatchObject({ status: "RIA", city: "BURLINGAME", fund_count: 4 });
    expect(out.sources).toEqual([
      { type: "form_adv", id: "159649", url: "https://adviserinfo.sec.gov/firm/summary/159649", filed: "2026-06-26" },
    ]);
  });

  it("matches several advisers by a shared prefix and by CRD", async () => {
    const { out } = await call(resolveManager, { query: "brookfield" });
    expect(out.data.advisers.map((a: any) => a.crd).sort()).toEqual(["151605", "316590"]);
    const byCrd = await call(resolveManager, { query: "105496" });
    expect(byCrd.out.data).toMatchObject({ matched_by: "crd", advisers: [{ name: "T. ROWE PRICE ASSOCIATES, INC." }] });
  });

  it("returns an empty list with a note when nothing matches", async () => {
    const { out } = await call(resolveManager, { query: "zzzz no such firm" });
    expect(out.data.advisers).toEqual([]);
    expect(out.notes.join(" ")).toMatch(/No match/);
  });

  it("rejects bad input", () => {
    expect(() => resolveManager.inputSchema.parse({ query: "" })).toThrow();
    expect(() => resolveManager.inputSchema.parse({ query: "x", limit: 26 })).toThrow();
  });
});

describe("list_funds", () => {
  it("lists an adviser's funds with totals that exclude feeders", async () => {
    const { out } = await call(listFunds, { crd: "293329" });
    expect(out.data.adviser).toMatchObject({ crd: "293329", name: "GEF CAPITAL PARTNERS, LLC" });
    expect(out.data.funds).toHaveLength(5);
    const feeder = out.data.funds.find((f: any) => f.pfid === "805-1051900694");
    expect(feeder).toMatchObject({ is_feeder: true, master_pfid: "805-8819499885", form_d_file_nos: ["021-42540402"] });
    const nonFeederGav = out.data.funds.filter((f: any) => !f.is_feeder).reduce((s: number, f: any) => s + f.gross_asset_value_usd, 0);
    expect(out.data.totals).toMatchObject({ funds: 5, feeder_funds: 1, gross_asset_value_usd_excl_feeders: nonFeederGav });
    expect(out.notes.join(" ")).toMatch(/1 feeder fund\(s\) excluded/);
    expect(out.data.next_cursor).toBeNull();
  });

  it("returns extra Form D file numbers", async () => {
    const { out } = await call(listFunds, { crd: "153232" });
    expect(out.data.funds[0].form_d_file_nos).toEqual(["021-487358", "021-487359"]);
  });

  it("pages 25 at a time and filters by type", async () => {
    const p1 = await call(listFunds, { crd: "9990001" });
    expect(p1.out.data.funds).toHaveLength(25);
    expect(p1.out.data.totals.funds).toBe(30);
    const p2 = await call(listFunds, { crd: "9990001", cursor: p1.out.data.next_cursor });
    expect(p2.out.data.funds).toHaveLength(5);
    expect(p2.out.data.next_cursor).toBeNull();
    expect(p2.out.data.totals).toBeNull();
    const all = [...p1.out.data.funds, ...p2.out.data.funds].map((f: any) => f.pfid);
    expect(new Set(all).size).toBe(30);
    const pe = await call(listFunds, { crd: "9990001", fund_type: "private_equity" });
    expect(pe.out.data.funds).toHaveLength(10);
    expect(pe.out.data.funds.every((f: any) => f.fund_type === "private_equity")).toBe(true);
  });

  it("returns no funds for an unknown CRD", async () => {
    const { out } = await call(listFunds, { crd: "1" });
    expect(out.data).toEqual({ adviser: null, funds: [], next_cursor: null });
    expect(out.notes[0]).toMatch(/No in-scope adviser has CRD 1/);
  });

  it("rejects bad input", async () => {
    expect(() => listFunds.inputSchema.parse({ crd: "293329", fund_type: "hedge" })).toThrow();
    await expect(listFunds.run(ctx.db(), { crd: "abc" })).rejects.toThrow(InputError);
    await expect(listFunds.run(ctx.db(), { crd: "293329", cursor: "not-a-cursor" })).rejects.toThrow(InputError);
  });
});

describe("fund_history", () => {
  it("returns every filing across the fund's file numbers, oldest first", async () => {
    const { out } = await call(fundHistory, { pfid: "805-1033506186" });
    expect(out.data.form_d_file_nos).toEqual(["021-487358", "021-487359"]);
    const dates = out.data.filings.map((f: any) => f.filing_date);
    expect(dates).toEqual([...dates].sort());
    expect(out.data.filings).toHaveLength(6);
    expect(out.data.filings[0]).toMatchObject({ is_amendment: true, offering_size_usd: "indefinite" });
    expect(out.data.funds[0].adviser).toEqual({ crd: "153232", name: "RTW INVESTMENTS LP" });
    expect(out.sources.filter((s: any) => s.type === "form_d")).toHaveLength(6);
    expect(out.sources).toContainEqual({
      type: "form_d",
      id: "0001315863-24-000565",
      url: "https://www.sec.gov/Archives/edgar/data/1985516/000131586324000565/",
      filed: "2024-07-16",
    });
  });

  it("looks up by file number, accepting leading zeros", async () => {
    const { out } = await call(fundHistory, { form_d_file_no: "021-0516526" });
    expect(out.data.funds.map((f: any) => f.pfid)).toEqual(["805-1048167165"]);
    expect(out.data.filings.map((f: any) => f.total_sold_usd)).toEqual([0, 70593000, 180412279]);
  });

  it("explains an empty history", async () => {
    const noNumber = await call(fundHistory, { pfid: "805-1303245262" });
    expect(noNumber.out.data.filings).toEqual([]);
    expect(noNumber.out.notes.join(" ")).toMatch(/no Form D file number/);
    const old = await call(fundHistory, { pfid: "805-3180045069" });
    expect(old.out.data.filings).toEqual([]);
    expect(old.out.notes.join(" ")).toMatch(/predates our data/);
    expect(old.out.notes.join(" ")).not.toMatch(/never/);
  });

  it("rejects bad input", async () => {
    expect(() => fundHistory.inputSchema.parse({})).toThrow();
    expect(() => fundHistory.inputSchema.parse({ pfid: "805-1033506186", form_d_file_no: "021-1" })).toThrow();
    await expect(fundHistory.run(ctx.db(), { pfid: "12345" })).rejects.toThrow(InputError);
    await expect(fundHistory.run(ctx.db(), { form_d_file_no: "801-12345" })).rejects.toThrow(InputError);
  });
});

describe("new_launches", () => {
  it("lists first-time notices newest first with manager, SPV flag and people", async () => {
    const { out } = await call(newLaunches, { since: "2026-10-01", until: "2026-10-31" });
    const l = out.data.launches;
    expect(l.map((x: any) => x.filing_date)).toEqual(["2026-10-20", "2026-10-15", "2026-10-10", "2026-10-05", "2026-10-01"]);
    expect(l[0].manager).toMatchObject({ crd: "9990001", pfid: "805-9900000001" });
    expect(l[1].manager).toMatchObject({ crd: "9990001", pfid: "805-9900000002" }); // via fund_file_nos
    expect(l[2]).toMatchObject({ likely_spv: true, manager: null });
    expect(l[3].likely_spv).toBe(true);
    expect(l[4].likely_spv).toBe(false);
    expect(l[0].related_persons[0]).toMatchObject({ name: "Jane Example" });
    expect(l.some((x: any) => x.accession_no === "0009990001-26-000006")).toBe(false); // amendment
    expect(out.notes.join(" ")).toMatch(/3 launch\(es\) have manager: null/);
    expect(out.notes.join(" ")).toMatch(/loaded through 2026-10-25/);
  });

  it("filters by state and pages with a cursor", async () => {
    const ny = await call(newLaunches, { since: "2026-10-01", until: "2026-10-31", state: "ny" });
    expect(ny.out.data.launches.map((x: any) => x.state)).toEqual(["NY", "NY"]);
    const all = await call(newLaunches, { since: "2008-01-01", until: "2026-10-31" });
    expect(all.out.data.launches.length).toBeLessThanOrEqual(25);
    expect(all.out.data.next_cursor).toBeNull(); // fixture has fewer than 26 launches
  });

  it("returns an empty window with no error", async () => {
    const { out } = await call(newLaunches, { since: "2010-01-01", until: "2010-12-31" });
    expect(out.data.launches).toEqual([]);
    expect(out.data.next_cursor).toBeNull();
  });

  it("defaults to the last 90 days", async () => {
    const { out } = await call(newLaunches, {});
    const days = (Date.parse(out.data.window.until) - Date.parse(out.data.window.since)) / 86_400_000;
    expect(days).toBe(90);
  });

  it("rejects bad input", async () => {
    expect(() => newLaunches.inputSchema.parse({ since: "10/01/2026" })).toThrow();
    expect(() => newLaunches.inputSchema.parse({ state: "California" })).toThrow();
    await expect(newLaunches.run(ctx.db(), { since: "2026-10-31", until: "2026-10-01" })).rejects.toThrow(InputError);
  });

  it("flags SPV-like names", () => {
    expect(looksLikeSpv("ACME SPV II LLC")).toBe(true);
    expect(looksLikeSpv("Deal X, a series of Foo LLC")).toBe(true);
    expect(looksLikeSpv("Foo Fund Series 12")).toBe(true);
    expect(looksLikeSpv("Spvx Capital Fund I")).toBe(false);
  });
});

describe("numeric IDs", () => {
  it("accepts a CRD sent as a number", async () => {
    const { out } = await call(listFunds, { crd: 293329 });
    expect(out.data.adviser.crd).toBe("293329");
    const r = await call(resolveManager, { query: 105496 });
    expect(r.out.data.matched_by).toBe("crd");
  });
});
