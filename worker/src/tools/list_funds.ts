import { z } from "zod";
import { MAX_ROWS, MAX_ROWS_READ, placeholders } from "../lib/db";
import { decodeCursor, encodeCursor, InputError } from "../lib/cursor";
import { envelope, formAdvSource, readAsOf } from "../lib/envelope";
import { normCrd } from "../lib/ids";
import { defineTool } from "./types";

interface FundRow {
  rid: number;
  pfid: string;
  name: string;
  fund_type: string;
  gross_asset_value_usd: number | null;
  beneficial_owners: number | null;
  form_d_file_no: string | null;
  auditor: string | null;
  is_feeder: number;
  master_pfid: string | null;
}

// With a fund_type filter we read up to SCAN funds per page and keep the matches, so one call never
// walks a 20,000-fund adviser. Without a filter every row read is returned.
const SCAN = 200;

const isCursor = (v: unknown): v is { r: number } =>
  typeof v === "object" && v !== null && Number.isSafeInteger((v as { r: unknown }).r);

export const listFunds = defineTool({
  name: "list_funds",
  title: "List a manager's funds",
  description:
    "List the venture capital and private equity funds an adviser reports in Form ADV Schedule D 7.B.(1), " +
    "25 per page. Needs the adviser's CRD (get it from resolve_manager). For each fund: name, private fund ID (PFID), " +
    "type, gross asset value and number of beneficial owners as of the adviser's latest Form ADV, auditor, " +
    "Form D file number(s) and whether it is a feeder fund. The first page also gives totals that exclude feeders, " +
    "so master and feeder assets are not counted twice. Funds an adviser only co-advises (reported in full by another " +
    "adviser) are not listed. Hedge, real estate and other fund types are out of scope. " +
    "Values are as filed, not current, and there are no returns or performance figures.",
  inputSchema: z.object({
    crd: z
      .union([z.string().trim().min(1).max(12), z.number().int().nonnegative()])
      .describe("Adviser CRD number, from resolve_manager"),
    fund_type: z.enum(["venture_capital", "private_equity"]).optional().describe("Only this fund type"),
    cursor: z.string().max(200).optional().describe("next_cursor from the previous page"),
  }),
  async run(db, { crd: rawCrd, fund_type, cursor }) {
    const crd = normCrd(String(rawCrd));
    if (!crd) throw new InputError("crd must be the adviser's CRD number (digits only)");
    const after = decodeCursor(cursor, isCursor)?.r ?? 0;
    const asOf = await readAsOf(db);
    const notes: string[] = [];

    const adviser = await db.first<{ crd: string; name: string; adv_filing_date: string | null; fund_count: number | null }>(
      "SELECT crd, name, adv_filing_date, fund_count FROM advisers WHERE crd = ? LIMIT 1",
      crd,
    );
    if (!adviser) {
      notes.push(
        `No in-scope adviser has CRD ${crd}. Only advisers whose latest Form ADV lists a VC or PE fund are in this data; ` +
          "use resolve_manager to search by name.",
      );
      return envelope({ adviser: null, funds: [], next_cursor: null }, [], asOf, notes);
    }

    // idx_funds_adviser holds (adviser_crd, rowid), so this is an index range in rowid order, no sort.
    const scan = fund_type ? SCAN : MAX_ROWS + 1;
    const rows = await db.all<FundRow>(
      `SELECT rowid AS rid, pfid, name, fund_type, gross_asset_value_usd, beneficial_owners, form_d_file_no, auditor,
              is_feeder, master_pfid
       FROM funds WHERE adviser_crd = ? AND rowid > ? ORDER BY rowid LIMIT ?`,
      crd,
      after,
      scan,
    );
    const page: FundRow[] = [];
    let lastRid = after;
    let more = false;
    for (const r of rows) {
      if (page.length === MAX_ROWS) {
        more = true;
        break;
      }
      lastRid = r.rid;
      if (!fund_type || r.fund_type === fund_type) page.push(r);
    }
    if (!more && rows.length === scan) more = true; // the scan window was full; there may be more funds
    if (fund_type && more && page.length < MAX_ROWS) {
      notes.push(`This page checked ${rows.length} funds for type ${fund_type}; follow next_cursor for the rest.`);
    }

    const extra = page.length
      ? await db.all<{ pfid: string; file_no: string }>(
          `SELECT pfid, file_no FROM fund_file_nos WHERE pfid IN (${placeholders(page.length)}) LIMIT 100`,
          ...page.map((r) => r.pfid),
        )
      : [];

    let totals = null;
    if (!cursor) {
      const n = adviser.fund_count ?? 0;
      if (n <= MAX_ROWS_READ) {
        const t = await db.first<{ funds: number; feeders: number; gav: number | null; owners: number | null }>(
          `SELECT count(*) AS funds, coalesce(sum(is_feeder), 0) AS feeders,
                  sum(CASE WHEN is_feeder = 0 THEN gross_asset_value_usd END) AS gav,
                  sum(CASE WHEN is_feeder = 0 THEN beneficial_owners END) AS owners
           FROM funds WHERE adviser_crd = ? AND (? IS NULL OR fund_type = ?) LIMIT 1`,
          crd,
          fund_type ?? null,
          fund_type ?? null,
        );
        if (t) {
          totals = {
            funds: t.funds,
            feeder_funds: t.feeders,
            gross_asset_value_usd_excl_feeders: t.gav,
            beneficial_owners_excl_feeders: t.owners,
          };
          if (t.feeders) notes.push(`${t.feeders} feeder fund(s) excluded from the totals so master and feeder assets are not counted twice.`);
        }
      } else {
        notes.push(`Totals skipped: this adviser reports ${n} funds, more than this server sums in one request.`);
      }
    }
    notes.push(`Gross asset values and owner counts are as reported in the adviser's Form ADV filed ${adviser.adv_filing_date ?? "(date unknown)"}.`);

    const funds = page.map((r) => ({
      pfid: r.pfid,
      name: r.name,
      fund_type: r.fund_type,
      gross_asset_value_usd: r.gross_asset_value_usd,
      beneficial_owners: r.beneficial_owners,
      auditor: r.auditor,
      form_d_file_nos: [r.form_d_file_no, ...extra.filter((e) => e.pfid === r.pfid).map((e) => e.file_no)].filter(
        (x): x is string => !!x,
      ),
      is_feeder: r.is_feeder === 1,
      master_pfid: r.master_pfid,
    }));
    return envelope(
      {
        adviser: { crd: adviser.crd, name: adviser.name, fund_count: adviser.fund_count },
        totals,
        funds,
        next_cursor: more ? encodeCursor({ r: lastRid }) : null,
      },
      [formAdvSource(adviser.crd, adviser.adv_filing_date)],
      asOf,
      notes,
    );
  },
});
