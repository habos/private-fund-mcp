import { z } from "zod";
import { MAX_ROWS, placeholders } from "../lib/db";
import { decodeCursor, encodeCursor, InputError } from "../lib/cursor";
import { envelope, formAdvSource, formDSource, readAsOf, type Source } from "../lib/envelope";
import { defineTool } from "./types";

interface LaunchRow {
  rid: number;
  accession_no: string;
  file_no: string | null;
  cik: string;
  issuer_name: string;
  fund_type: string | null;
  filing_date: string;
  first_sale_date: string | null;
  total_offering_usd: number | null;
  offering_indefinite: number;
  total_sold_usd: number | null;
  state: string | null;
  related_persons: string | null;
}

interface ManagerRow {
  file_no: string;
  pfid: string;
  fund_name: string;
  crd: string;
  adviser_name: string;
  adv_filing_date: string | null;
}

const SCAN = 200; // rows read per page when a state filter is set (state has no index)
const MAX_PEOPLE = 10;
const DAY_MS = 86_400_000;
const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, "use YYYY-MM-DD");

const isCursor = (v: unknown): v is { d: string; r: number } =>
  typeof v === "object" &&
  v !== null &&
  typeof (v as { d: unknown }).d === "string" &&
  /^\d{4}-\d{2}-\d{2}$/.test((v as { d: string }).d) &&
  Number.isSafeInteger((v as { r: unknown }).r);

/** SPEC section 5: names with "SPV", "a series of", or "Series" + number. */
export function looksLikeSpv(name: string): boolean {
  return /\bSPV\b/i.test(name) || /\ba series of\b/i.test(name) || /\bseries\s+\d+/i.test(name);
}

export const newLaunches = defineTool({
  name: "new_launches",
  title: "New fund launches",
  description:
    "First-time Form D notices from pooled investment funds (new fund raises) filed in a date window, newest first, " +
    "25 per page: issuer, filing date, Form D fund type, amount sold so far, offering size, issuer state, related " +
    "persons (GPs, officers, promoters), a likely-SPV flag, and the managing adviser (CRD and name) when a Form ADV " +
    "fund lists the same Form D file number, else null. Unmatched launches are limited to Form D fund types " +
    "Venture Capital Fund and Private Equity Fund. Amendments (D/A) are not launches; use fund_history for them. " +
    "Defaults to the last 90 days. Form D data starts in 2008 and is only as fresh as as_of.form_d. " +
    "No returns or performance data.",
  inputSchema: z.object({
    since: isoDate.optional().describe("Earliest filing date, YYYY-MM-DD (default: 90 days ago)"),
    until: isoDate.optional().describe("Latest filing date, YYYY-MM-DD (default: today)"),
    state: z
      .string()
      .trim()
      .regex(/^[A-Za-z0-9]{2}$/, "use a 2-letter state or country code, e.g. CA")
      .optional()
      .describe("Issuer state or country code from Form D, e.g. CA, NY, DE"),
    cursor: z.string().max(200).optional().describe("next_cursor from the previous page"),
  }),
  async run(db, { since: rawSince, until: rawUntil, state: rawState, cursor }) {
    const now = Date.now();
    const until = rawUntil ?? new Date(now).toISOString().slice(0, 10);
    const since = rawSince ?? new Date(now - 90 * DAY_MS).toISOString().slice(0, 10);
    if (since > until) throw new InputError("since must be on or before until");
    const state = rawState?.toUpperCase();
    const pos = decodeCursor(cursor, isCursor);

    const asOf = await readAsOf(db);
    const notes: string[] = [];

    // idx_formd_new_funds is partial (is_fund = 1 AND is_amendment = 0) on filing_date, so the WHERE must repeat
    // those terms. Keyset paging on (filing_date, rowid) walks the index backwards with no sort and no OFFSET;
    // the upper date bound moves down to the cursor's date so later pages don't re-read earlier ones.
    const scan = state ? SCAN : MAX_ROWS + 1;
    const rows = await db.all<LaunchRow>(
      `SELECT rowid AS rid, accession_no, file_no, cik, issuer_name, fund_type, filing_date, first_sale_date,
              total_offering_usd, offering_indefinite, total_sold_usd, state, related_persons
       FROM formd_offerings
       WHERE is_fund = 1 AND is_amendment = 0 AND filing_date >= ? AND filing_date <= ?
         AND (filing_date, rowid) < (?, ?)
       ORDER BY filing_date DESC, rowid DESC LIMIT ?`,
      since,
      pos && pos.d < until ? pos.d : until,
      pos?.d ?? "9999-12-31",
      pos?.r ?? Number.MAX_SAFE_INTEGER,
      scan,
    );
    const page: LaunchRow[] = [];
    let last: LaunchRow | null = null;
    let more = false;
    for (const r of rows) {
      if (page.length === MAX_ROWS) {
        more = true;
        break;
      }
      last = r;
      if (!state || r.state === state) page.push(r);
    }
    if (!more && rows.length === scan) more = true;
    if (state && more && page.length < MAX_ROWS) {
      notes.push(`This page checked ${rows.length} launches for state ${state}; follow next_cursor for the rest.`);
    }

    // Matched manager: Form ADV funds listing the same Form D file number, main or extra.
    const fileNos = [...new Set(page.map((r) => r.file_no).filter((x): x is string => !!x))];
    const managers = fileNos.length
      ? await db.all<ManagerRow>(
          `SELECT f.form_d_file_no AS file_no, f.pfid, f.name AS fund_name, a.crd, a.name AS adviser_name, a.adv_filing_date
           FROM funds f JOIN advisers a ON a.crd = f.adviser_crd WHERE f.form_d_file_no IN (${placeholders(fileNos.length)})
           UNION ALL
           SELECT x.file_no, f.pfid, f.name, a.crd, a.name, a.adv_filing_date
           FROM fund_file_nos x JOIN funds f ON f.pfid = x.pfid JOIN advisers a ON a.crd = f.adviser_crd
           WHERE x.file_no IN (${placeholders(fileNos.length)})
           LIMIT 100`,
          ...fileNos,
          ...fileNos,
        )
      : [];
    const byFileNo = new Map<string, ManagerRow>();
    for (const m of managers) if (!byFileNo.has(m.file_no)) byFileNo.set(m.file_no, m);

    const sources: Source[] = [];
    let unmatched = 0;
    let spvs = 0;
    const launches = page.map((r) => {
      const m = r.file_no ? byFileNo.get(r.file_no) : undefined;
      sources.push(formDSource(r));
      if (m) sources.push(formAdvSource(m.crd, m.adv_filing_date));
      else unmatched++;
      const spv = looksLikeSpv(r.issuer_name);
      if (spv) spvs++;
      let people: unknown[] = [];
      try {
        people = JSON.parse(r.related_persons ?? "[]");
      } catch {
        people = [];
      }
      return {
        accession_no: r.accession_no,
        issuer_name: r.issuer_name,
        form_d_file_no: r.file_no,
        filing_date: r.filing_date,
        form_d_fund_type: r.fund_type,
        first_sale_date: r.first_sale_date,
        total_sold_usd: r.total_sold_usd,
        offering_size_usd: r.offering_indefinite === 1 ? "indefinite" : r.total_offering_usd,
        state: r.state,
        related_persons: people.slice(0, MAX_PEOPLE),
        likely_spv: spv,
        manager: m ? { crd: m.crd, name: m.adviser_name, pfid: m.pfid, fund_name: m.fund_name } : null,
      };
    });

    if (unmatched) {
      notes.push(
        `${unmatched} launch(es) have manager: null because no Form ADV fund in our data lists their Form D file number ` +
          "yet; advisers often add a new fund at their next annual Form ADV update.",
      );
    }
    if (spvs) notes.push(`${spvs} launch(es) flagged likely_spv (name has "SPV", "a series of", or "Series" + number); they are kept, not dropped.`);
    if (page.some((r) => (r.total_sold_usd ?? 0) === 0)) {
      notes.push("A first Form D often reports $0 sold; later D/A amendments (see fund_history) show the raise.");
    }
    if (asOf.form_d && until > asOf.form_d) {
      notes.push(`Form D data is loaded through ${asOf.form_d}; filings after that date are not in these results yet.`);
    }
    return envelope(
      {
        window: { since, until, state: state ?? null },
        launches,
        next_cursor: more && last ? encodeCursor({ d: last.filing_date, r: last.rid }) : null,
      },
      sources,
      asOf,
      notes,
    );
  },
});
