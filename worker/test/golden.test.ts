// Golden set (SPEC section 8): the 10 questions in golden/golden_questions.md and the 20-fund hand-check
// replay (golden/handcheck_replay.csv), run against real data in a local D1.
//
// Default data: test/fixtures/golden_seed.sql, the slice of build/fund.db these calls read (committed, so CI
// needs no fund.db). With GOLDEN_SQL_DIR=../build/sql (etl.export_sql output) the same assertions run against
// a local D1 holding all of fund.db. With GOLDEN_URL=https://…workers.dev they run over HTTP against the
// deployed Worker instead (MCP tools/call, one request at a time, ≥ 250 ms apart). That mode only runs when
// GOLDEN_URL is set by hand, never in CI: `GOLDEN_URL=https://… npx vitest run test/golden.test.ts`.
import { readdirSync, readFileSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Db } from "../src/lib/db";
import { MAX_QUERIES, MAX_ROWS_READ } from "../src/lib/db";
import { DISCLAIMER } from "../src/lib/envelope";
import { fundHistory } from "../src/tools/fund_history";
import { listFunds } from "../src/tools/list_funds";
import { newLaunches } from "../src/tools/new_launches";
import { resolveManager } from "../src/tools/resolve_manager";
import type { ToolDef } from "../src/tools/types";
import { start, type Ctx } from "./harness";

const fullDir = process.env.GOLDEN_SQL_DIR;
const liveUrl = process.env.GOLDEN_URL?.replace(/\/+$/, "");
const AS_OF = { form_d: "2026-09-30", form_adv: "2026-10-31" };
const LIVE_GAP_MS = 250; // at most 4 requests per second against production

function seed(): string[] {
  if (!fullDir) return [readFileSync(new URL("./fixtures/golden_seed.sql", import.meta.url), "utf8")];
  const dir = new URL(`${fullDir.replace(/\/?$/, "/")}`, `file://${process.cwd()}/`);
  return readdirSync(dir)
    .filter((f) => f.endsWith(".sql"))
    .sort()
    .map((f) => readFileSync(new URL(f, dir), "utf8"));
}

let ctx: Ctx;
const budget = { maxRowsRead: 0, calls: 0 };
beforeAll(async () => {
  if (!liveUrl) ctx = await start(seed());
}, 600_000);
afterAll(async () => {
  const src = liveUrl ? `production ${liveUrl}` : fullDir ? `full fund.db from ${fullDir}` : "golden_seed.sql";
  console.log(`golden (${src}): ${budget.calls} tool calls${liveUrl ? "" : `, largest rows_read ${budget.maxRowsRead}`}`);
  await ctx?.server.close();
});

let lastLive = 0;
/** tools/call on the deployed Worker; returns the parsed envelope from the text content. */
async function callLive(name: string, input: unknown): Promise<any> {
  const wait = lastLive + LIVE_GAP_MS - Date.now();
  if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  lastLive = Date.now();
  const res = await fetch(`${liveUrl}/mcp`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: budget.calls + 1, method: "tools/call", params: { name, arguments: input } }),
  });
  const body = await res.text();
  expect(res.status, body.slice(0, 300)).toBe(200);
  // The stateless handler answers as JSON or as one SSE `data:` event.
  const json = body.trimStart().startsWith("{")
    ? body
    : body.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join("");
  const msg = JSON.parse(json);
  expect(msg.error, JSON.stringify(msg.error)).toBeUndefined();
  expect(msg.result.isError, msg.result.content?.[0]?.text).toBeFalsy();
  return JSON.parse(msg.result.content[0].text);
}

/** Run a tool like the MCP SDK does, enforcing the envelope and the per-query read budget. */
async function call(tool: ToolDef<any>, input: unknown): Promise<any> {
  if (liveUrl) {
    const out = await callLive(tool.name, input);
    budget.calls++;
    expect(out.disclaimer).toBe(DISCLAIMER);
    expect(out.as_of.form_adv).toBe(AS_OF.form_adv);
    // The nightly job moves form_d forward; answers that differ only because of later filings are reported, not failed.
    expect(out.as_of.form_d >= AS_OF.form_d, out.as_of.form_d).toBe(true);
    return out;
  }
  const db: Db = ctx.db();
  const out = await tool.run(db, tool.inputSchema.parse(input));
  budget.calls++;
  expect(db.stats.length).toBeLessThanOrEqual(MAX_QUERIES);
  for (const s of db.stats) {
    expect(s.rows_read, s.sql).toBeLessThanOrEqual(MAX_ROWS_READ);
    budget.maxRowsRead = Math.max(budget.maxRowsRead, s.rows_read);
  }
  expect(out.disclaimer).toBe(DISCLAIMER);
  expect(out.as_of).toEqual(AS_OF);
  return out;
}

/** Follow next_cursor to the end (at most `max` pages) and concatenate `key` from every page. */
async function allPages(tool: ToolDef<any>, input: Record<string, unknown>, key: string, max = 20) {
  const items: any[] = [];
  const notes: string[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < max; page++) {
    const out = await call(tool, cursor ? { ...input, cursor } : input);
    items.push(...out.data[key]);
    notes.push(...out.notes);
    cursor = out.data.next_cursor ?? undefined;
    if (!cursor) return { items, notes, pages: page + 1 };
  }
  throw new Error(`more than ${max} pages`);
}

describe("golden questions (golden/golden_questions.md)", () => {
  it("Q1 near-duplicate names: 'Uncork Capital' is two advisers, not one", async () => {
    const out = await call(resolveManager, { query: "Uncork Capital" });
    expect(out.data.matched_by).toBe("name");
    expect(out.data.advisers.map((a: any) => [a.crd, a.name, a.status, a.fund_count])).toEqual([
      ["160391", "UNCORK CAPITAL, INC.", "ERA", 5],
      ["308145", "UNCORK CAPITAL MANAGEMENT LLC", "ERA", 6],
    ]);
    expect(out.sources.map((s: any) => s.url)).toEqual([
      "https://adviserinfo.sec.gov/firm/summary/160391",
      "https://adviserinfo.sec.gov/firm/summary/308145",
    ]);
  });

  it("Q2 lookup by CRD 156612", async () => {
    const out = await call(resolveManager, { query: "156612" });
    expect(out.data).toEqual({
      matched_by: "crd",
      advisers: [
        {
          crd: "156612",
          name: "WEATHERGAGE CAPITAL, LLC",
          status: "RIA",
          regulatory_aum_usd: 1362954590,
          city: "AUSTIN",
          state: "TX",
          fund_count: 8,
          form_adv_filed: "2026-03-17",
        },
      ],
    });
  });

  it("Q3 feeder funds are excluded from Ethereal Ventures' totals", async () => {
    const found = await call(resolveManager, { query: "Ethereal Ventures" });
    expect(found.data.advisers.map((a: any) => a.crd)).toEqual(["322432"]);
    const out = await call(listFunds, { crd: "322432" });
    expect(out.data.totals).toEqual({
      funds: 5,
      feeder_funds: 2,
      gross_asset_value_usd_excl_feeders: 139_114_000, // 92,277,000 + 43,404,000 + 3,433,000
      beneficial_owners_excl_feeders: 210,
    });
    const feeders = out.data.funds.filter((f: any) => f.is_feeder).map((f: any) => [f.name, f.master_pfid]);
    expect(feeders.sort()).toEqual([
      ["ETHEREAL VENTURES I FEEDER L.P.", "805-8261285761"],
      ["ETHEREAL VENTURES II FEEDER L.P.", "805-4856995645"],
    ]);
    expect(out.notes.join(" ")).toMatch(/2 feeder fund\(s\) excluded from the totals/);
  });

  it("Q4 indefinite offering size: Eagle Venture Fund IV", async () => {
    const out = await call(fundHistory, { pfid: "805-6872391500" });
    expect(out.data.funds[0]).toMatchObject({ name: "EAGLE VENTURE FUND IV, LP", adviser: { crd: "299541" } });
    expect(out.data.filings.map((f: any) => [f.filing_date, f.is_amendment, f.offering_size_usd, f.total_sold_usd, f.investors])).toEqual([
      ["2024-02-28", false, "indefinite", 0, 0],
      ["2025-03-03", true, "indefinite", 5_115_000, 20],
    ]);
    expect(out.notes.join(" ")).toMatch(/"indefinite" means the fund did not cap/);
  });

  it("Q5 new launches in Connecticut, August 2026", async () => {
    const { items, notes, pages } = await allPages(newLaunches, { since: "2026-08-01", until: "2026-08-31", state: "CT" }, "launches");
    expect(pages).toBeGreaterThan(1); // the state filter scans 200 launches per page, so Claude follows next_cursor
    expect(items.map((l: any) => [l.filing_date, l.issuer_name])).toEqual([
      ["2026-08-27", "Ares Secondaries Acquisition Vehicle 29 BigCat LP"],
      ["2026-08-26", "MIC01, a series of Get Access Master Partnership, LP"],
      ["2026-08-25", "MC Opportunistic Credit Fund LP"],
      ["2026-08-24", "2 Mile DK SPV LLC"],
      ["2026-08-21", "Zenith Equity Fund-A LP"],
      ["2026-08-21", "Zenith Equity Fund LP"],
      ["2026-08-21", "Stork 2 SPV, L.P."],
      ["2026-08-18", "Tamarack Global Impulse Space V, LP"],
      ["2026-08-17", "PB Private Credit Account (I) Investment I, L.P."],
      ["2026-08-14", "Builders-AS, a series of Get Access Master Partnership, LP"],
      ["2026-08-14", "Positive Sum Conviction Fund, LP"],
      ["2026-08-12", "Positive Sum Capital IV, LP"],
      ["2026-08-06", "Fairview Private Markets Fund VII, LP"],
      ["2026-08-06", "Eagle Point Infrastructure Credit Opportunity Fund US LP"],
      ["2026-08-04", "Integra Global Advisors VC Access Fund Global I LP"],
      ["2026-08-04", "Ironwood Capital Partners VI SBIC LP"],
    ]);
    expect(items.every((l: any) => l.state === "CT" && l.manager === null)).toBe(true);
    expect(items.filter((l: any) => l.likely_spv).map((l: any) => l.issuer_name)).toEqual([
      "MIC01, a series of Get Access Master Partnership, LP",
      "2 Mile DK SPV LLC",
      "Stork 2 SPV, L.P.",
      "Builders-AS, a series of Get Access Master Partnership, LP",
    ]);
    expect(items.find((l: any) => l.issuer_name.startsWith("Tamarack"))).toMatchObject({
      total_sold_usd: 5_595_000,
      offering_size_usd: 10_000_000,
      form_d_fund_type: "Venture Capital Fund",
    });
    expect(notes.join(" ")).toMatch(/manager: null because no Form ADV fund/);
  });

  it("Q6 fund with no Form D file number: Standard Sapphire LP", async () => {
    const out = await call(fundHistory, { pfid: "805-2376050040" });
    expect(out.data).toMatchObject({
      funds: [{ name: "STANDARD SAPPHIRE LP", adviser: { crd: "165808", name: "STANDARD CAPITAL ADVISORS L.P." } }],
      form_d_file_nos: [],
      filings: [],
    });
    expect(out.notes.join(" ")).toMatch(/lists no Form D file number in its Form ADV/);
  });

  it("Q7 from a Form D file number to the fund, its manager and its raise", async () => {
    const out = await call(fundHistory, { form_d_file_no: "021-257645" });
    expect(out.data.funds).toEqual([
      {
        pfid: "805-2341511343",
        name: "WEATHERGAGE VENTURE CAPITAL IV LP",
        fund_type: "venture_capital",
        adviser: { crd: "156612", name: "WEATHERGAGE CAPITAL, LLC" },
      },
    ]);
    expect(out.data.filings.map((f: any) => [f.accession_no, f.is_amendment, f.total_sold_usd, f.offering_size_usd, f.investors])).toEqual([
      ["0001664470-16-000001", false, 0, 200_000_000, 0],
      ["0001664470-17-000001", true, 190_325_000, 200_000_000, 49],
    ]);
    expect(out.sources).toContainEqual({
      type: "form_d",
      id: "0001664470-17-000001",
      url: "https://www.sec.gov/Archives/edgar/data/1664470/000166447017000001/",
      filed: "2017-02-17",
    });
  });

  it("Q8 fund with several Form D file numbers: Coller International Partners IX", async () => {
    const nos = ["021-468009", "021-468015", "021-512841", "021-512844"];
    const funds = await call(listFunds, { crd: "111035" });
    expect(funds.data.funds.find((f: any) => f.pfid === "805-3909445633").form_d_file_nos).toEqual(nos);
    const out = await call(fundHistory, { pfid: "805-3909445633" });
    expect(out.data.form_d_file_nos).toEqual(nos);
    // One Form ADV fund, four parallel partnerships (IX-A, -B, -E, -F) each filing its own Form D.
    expect(out.data.filings.map((f: any) => [f.issuer_name, f.filing_date, f.total_sold_usd, f.investors])).toEqual([
      ["Coller International Partners IX - A, L.P.", "2022-12-20", 0, 0],
      ["Coller International Partners IX - B, L.P.", "2022-12-20", 0, 0],
      ["Coller International Partners IX - F, L.P.", "2024-05-09", 0, 0],
      ["Coller International Partners IX - E, L.P.", "2024-05-09", 0, 0],
      ["Coller International Partners IX - A, L.P.", "2026-05-18", 11_284_206_649, 40],
      ["Coller International Partners IX - B, L.P.", "2026-05-18", 11_284_206_649, 9],
    ]);
  });

  it("Q9 only the venture capital funds of Third Lake Partners", async () => {
    const out = await call(listFunds, { crd: "299961", fund_type: "venture_capital" });
    expect(out.data.funds.map((f: any) => f.name).sort()).toEqual([
      "THIRD LAKE VC FUND I, LP",
      "THIRD LAKE VC FUND II, LP",
      "THIRD LAKE VC FUND III, LP",
    ]);
    expect(out.data.totals).toMatchObject({ funds: 3, feeder_funds: 0, gross_asset_value_usd_excl_feeders: 64_166_307 });
    expect(out.data.adviser).toEqual({ crd: "299961", name: "THIRD LAKE PARTNERS, LLC", fund_count: 9 });
  });

  it("Q10 file number listed in Form ADV but no Form D on EDGAR: Bridgescale Opportunities IV", async () => {
    const out = await call(fundHistory, { pfid: "805-4320106349" });
    expect(out.data).toMatchObject({ form_d_file_nos: ["021-84037"], filings: [] });
    expect(out.notes.join(" ")).toMatch(/No Form D filing for 021-84037 between 2008-01-01 and 2026-09-30/);
  });
});

// --- Hand-check replay: our Worker's answers vs. the values handcheck_replay.py read from the raw ADV files and EDGAR.

function parseCsv(text: string): Record<string, string>[] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') (field += '"'), i++;
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") row.push(field), (field = "");
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field), rows.push(row), (row = []), (field = "");
    } else field += c;
  }
  if (field || row.length) row.push(field), rows.push(row);
  const [head, ...body] = rows;
  return body.filter((r) => r.length === head.length).map((r) => Object.fromEntries(head.map((h, j) => [h, r[j]])));
}

const replay = parseCsv(readFileSync(new URL("../../golden/handcheck_replay.csv", import.meta.url), "utf8"));

describe("hand-check replay (golden/handcheck_replay.csv, 20 funds)", () => {
  it("has the 20 funds", () => expect(replay).toHaveLength(20));

  it("the Worker matches the sources for at least 19 of 20 funds", async () => {
    const wrong: string[] = [];
    for (const r of replay) {
      const problems: string[] = [];
      const funds = await call(listFunds, { crd: r.adviser_crd });
      const fund = funds.data.funds.find((f: any) => f.pfid === r.pfid);
      if (!fund) problems.push("not in list_funds");
      else {
        if (String(fund.gross_asset_value_usd ?? "") !== r.src_gav_usd) problems.push(`GAV ${fund.gross_asset_value_usd} vs ${r.src_gav_usd}`);
        const src = r.src_file_nos ? r.src_file_nos.split(" ") : [];
        const main = fund.form_d_file_nos[0];
        if (main ? !src.includes(main) : src.length > 0) problems.push(`file no ${main} vs ${r.src_file_nos}`);
      }
      const hist = await call(fundHistory, { pfid: r.pfid });
      if (r.edgar_latest_accession) {
        const f = hist.data.filings.find((x: any) => x.accession_no === r.edgar_latest_accession);
        if (!f) problems.push(`EDGAR's latest filing ${r.edgar_latest_accession} missing`);
        else if (String(f.total_sold_usd) !== r.src_latest_amount_sold_usd) problems.push(`sold ${f.total_sold_usd} vs ${r.src_latest_amount_sold_usd}`);
        const main = r.our_form_d_file_no;
        const later = hist.data.filings.filter((x: any) => x.file_no === main && x.filing_date > r.edgar_latest_filed);
        if (later.length) problems.push(`filings after EDGAR's latest: ${later.map((x: any) => x.accession_no)}`);
      } else if (r.our_form_d_file_no) {
        if (hist.data.filings.some((x: any) => x.file_no === r.our_form_d_file_no)) problems.push("filings where EDGAR has none");
      } else if (hist.data.form_d_file_nos.length) problems.push("file numbers where ADV has none");
      if (problems.length) wrong.push(`${r.pfid} ${r.fund_name}: ${problems.join("; ")}`);
    }
    console.log(`hand-check replay: ${replay.length - wrong.length}/${replay.length} fully correct${wrong.length ? `\n  ${wrong.join("\n  ")}` : ""}`);
    expect(replay.length - wrong.length, wrong.join("\n")).toBeGreaterThanOrEqual(19);
  }, 120_000);
});
