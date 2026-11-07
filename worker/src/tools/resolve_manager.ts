import { z } from "zod";
import { MAX_ROWS } from "../lib/db";
import { envelope, formAdvSource, readAsOf } from "../lib/envelope";
import { nameNorm, normCrd } from "../lib/ids";
import { defineTool } from "./types";

interface AdviserRow {
  crd: string;
  name: string;
  status: string | null;
  raum_usd: number | null;
  city: string | null;
  state: string | null;
  adv_filing_date: string | null;
  fund_count: number | null;
}

const COLS = "crd, name, status, raum_usd, city, state, adv_filing_date, fund_count";

export const resolveManager = defineTool({
  name: "resolve_manager",
  title: "Find a fund manager",
  description:
    "Find SEC-registered or exempt reporting advisers that manage venture capital or private equity funds, " +
    "by CRD number or by name. Use this first to get the CRD that list_funds needs. " +
    "Name search matches the start of the adviser's legal name, ignoring case, punctuation and suffixes " +
    "like LLC or LP (\"acme capital\" finds \"Acme Capital Partners, L.P.\"). " +
    "Only advisers whose latest Form ADV reports at least one VC or PE fund in Schedule D 7.B.(1) are included; " +
    "hedge fund, real estate and other managers are out of scope. Returns firm facts from Form ADV " +
    "(status RIA/ERA, regulatory AUM, city/state, number of in-scope funds). No performance or returns data.",
  inputSchema: z.object({
    query: z
      .union([z.string().trim().min(1).max(200), z.number().int().nonnegative()])
      .describe("Adviser name (or its start) or CRD number, e.g. \"Sequoia\" or \"105496\""),
    limit: z.number().int().min(1).max(MAX_ROWS).default(10).describe("Max advisers to return (1-25, default 10)"),
  }),
  async run(db, { query: rawQuery, limit }) {
    const query = String(rawQuery);
    const asOf = await readAsOf(db);
    const notes: string[] = [];
    let rows: AdviserRow[] = [];
    let matchedBy: "crd" | "name" = "name";

    const crd = normCrd(query);
    if (crd) {
      rows = await db.all<AdviserRow>(`SELECT ${COLS} FROM advisers WHERE crd = ? LIMIT 1`, crd);
      if (rows.length) matchedBy = "crd";
      else notes.push(`No in-scope adviser has CRD ${crd}; searched names starting with "${query}" instead.`);
    }
    if (!rows.length) {
      const prefix = nameNorm(query);
      if (!prefix) {
        notes.push("The query has no letters or digits to search for.");
      } else {
        // Range scan on idx_advisers_name_norm: every name_norm that starts with prefix.
        rows = await db.all<AdviserRow>(
          `SELECT ${COLS} FROM advisers WHERE name_norm >= ? AND name_norm < ? ORDER BY name_norm LIMIT ?`,
          prefix,
          `${prefix}￿`,
          limit,
        );
        if (rows.length === limit) notes.push(`Showing the first ${limit} matches in name order; add more of the name to narrow it.`);
      }
    }
    if (!rows.length) {
      notes.push(
        "No match. Only advisers whose latest Form ADV lists a venture capital or private equity fund are in this data. " +
          "Name search matches the start of the legal name, so try the first word of the firm's name, or its CRD.",
      );
    }
    if (rows.some((r) => r.status === "ERA")) {
      notes.push("ERA = exempt reporting adviser: files a shorter Form ADV and reports no regulatory AUM.");
    }
    const advisers = rows.map((r) => ({
      crd: r.crd,
      name: r.name,
      status: r.status,
      regulatory_aum_usd: r.raum_usd,
      city: r.city,
      state: r.state,
      fund_count: r.fund_count,
      form_adv_filed: r.adv_filing_date,
    }));
    return envelope(
      { matched_by: matchedBy, advisers },
      rows.map((r) => formAdvSource(r.crd, r.adv_filing_date)),
      asOf,
      notes,
    );
  },
});
