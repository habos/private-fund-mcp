import { z } from "zod";
import { MAX_ROWS, placeholders } from "../lib/db";
import { decodeCursor, encodeCursor, InputError } from "../lib/cursor";
import { envelope, formAdvSource, formDSource, readAsOf, type Source } from "../lib/envelope";
import { normFileNo, normPfid } from "../lib/ids";
import { defineTool } from "./types";

interface FundRow {
  pfid: string;
  name: string;
  fund_type: string;
  form_d_file_no: string | null;
  adviser_crd: string;
  adviser_name: string;
  adv_filing_date: string;
}

interface OfferingRow {
  accession_no: string;
  file_no: string;
  cik: string;
  issuer_name: string;
  is_amendment: number;
  filing_date: string;
  first_sale_date: string | null;
  total_offering_usd: number | null;
  offering_indefinite: number;
  total_sold_usd: number | null;
  investors_count: number | null;
  min_investment_usd: number | null;
}

const FUND_COLS = `f.pfid, f.name, f.fund_type, f.form_d_file_no, f.adviser_crd, a.name AS adviser_name, f.adv_filing_date`;
const MAX_FILE_NOS = 20;

const isCursor = (v: unknown): v is { o: number } =>
  typeof v === "object" && v !== null && Number.isSafeInteger((v as { o: unknown }).o) && (v as { o: number }).o >= 0;

export const fundHistory = defineTool({
  name: "fund_history",
  title: "Fund's Form D history",
  description:
    "All Form D filings (the original notice and its D/A amendments) for one private fund, oldest first, 25 per page: " +
    "filing date, amendment flag, total amount sold, offering size (or \"indefinite\"), number of investors, " +
    "minimum investment and first-sale date. Give either the fund's private fund ID (PFID, from list_funds) or a " +
    "Form D file number (021-…). Form D covers the money a fund says it has raised, not its returns or holdings. " +
    "Form D data starts in 2008, and new filings arrive with the nightly update.",
  inputSchema: z
    .object({
      pfid: z.string().trim().max(20).optional().describe("Private fund ID, e.g. 805-1234567890"),
      form_d_file_no: z.string().trim().max(30).optional().describe("Form D file number, e.g. 021-123456"),
      cursor: z.string().max(200).optional().describe("next_cursor from the previous page"),
    })
    .refine((v) => !!v.pfid !== !!v.form_d_file_no, { message: "Give exactly one of pfid or form_d_file_no" }),
  async run(db, { pfid: rawPfid, form_d_file_no: rawFileNo, cursor }) {
    const offset = decodeCursor(cursor, isCursor)?.o ?? 0;
    let pfid: string | null = null;
    let fileNo: string | null = null;
    if (rawPfid) {
      pfid = normPfid(rawPfid);
      if (!pfid) throw new InputError("pfid must look like 805-1234567890 (805- plus 10 digits)");
    } else if (rawFileNo) {
      fileNo = normFileNo(rawFileNo);
      if (!fileNo) throw new InputError("form_d_file_no must look like 021-123456");
    }
    const asOf = await readAsOf(db);
    const notes: string[] = [];
    let funds: FundRow[] = [];
    let fileNos: string[] = [];

    if (pfid) {
      // funds by primary key, extra file numbers by the fund_file_nos primary key (pfid, file_no).
      funds = await db.all<FundRow>(
        `SELECT ${FUND_COLS} FROM funds f JOIN advisers a ON a.crd = f.adviser_crd WHERE f.pfid = ? LIMIT 1`,
        pfid,
      );
      if (!funds.length) {
        notes.push(`No in-scope fund has PFID ${pfid}. Only VC and PE funds reported in Form ADV 7.B.(1) are in this data.`);
        return envelope({ funds: [], form_d_file_nos: [], filings: [], next_cursor: null }, [], asOf, notes);
      }
      const extra = await db.all<{ file_no: string }>(
        "SELECT file_no FROM fund_file_nos WHERE pfid = ? ORDER BY file_no LIMIT ?",
        pfid,
        MAX_FILE_NOS,
      );
      fileNos = [funds[0].form_d_file_no, ...extra.map((e) => e.file_no)].filter((x): x is string => !!x);
    } else if (fileNo) {
      fileNos = [fileNo];
      // Either the fund's main number (idx_funds_file_no) or one of its extra numbers (idx_fund_file_nos_file_no).
      funds = await db.all<FundRow>(
        `SELECT ${FUND_COLS} FROM funds f JOIN advisers a ON a.crd = f.adviser_crd WHERE f.form_d_file_no = ?
         UNION
         SELECT ${FUND_COLS} FROM fund_file_nos x JOIN funds f ON f.pfid = x.pfid JOIN advisers a ON a.crd = f.adviser_crd
         WHERE x.file_no = ?
         LIMIT 5`,
        fileNo,
        fileNo,
      );
      if (!funds.length) {
        notes.push(`No Form ADV fund in this data lists file number ${fileNo}, so the manager is unknown.`);
      } else if (funds.length > 1) {
        notes.push(`${funds.length} funds list file number ${fileNo} in Form ADV (often a master and its feeders).`);
      }
    }

    let filings: OfferingRow[] = [];
    if (!fileNos.length) {
      notes.push(
        "This fund lists no Form D file number in its Form ADV, so it can't be linked to Form D filings " +
          "(funds that never sold to US investors, or that rely on other exemptions, often have none).",
      );
    } else {
      // idx_formd_file_no; a fund has at most a few dozen filings, so the sort and offset are cheap.
      filings = await db.all<OfferingRow>(
        `SELECT accession_no, file_no, cik, issuer_name, is_amendment, filing_date, first_sale_date, total_offering_usd,
                offering_indefinite, total_sold_usd, investors_count, min_investment_usd
         FROM formd_offerings WHERE file_no IN (${placeholders(fileNos.length)})
         ORDER BY filing_date, accession_no LIMIT ? OFFSET ?`,
        ...fileNos,
        MAX_ROWS + 1,
        offset,
      );
      if (!filings.length && offset === 0) {
        notes.push(
          `No Form D filing for ${fileNos.join(", ")} between ${asOf.form_d_since ?? "2008-01-01"} and ${asOf.form_d ?? "the latest data set"}. ` +
            "The fund's last Form D most likely predates our data, which starts in 2008; " +
            `a filing made after ${asOf.form_d ?? "the latest data set"} may not be loaded yet.`,
        );
      }
    }
    const more = filings.length > MAX_ROWS;
    filings = filings.slice(0, MAX_ROWS);

    const sources: Source[] = [
      ...funds.map((f) => formAdvSource(f.adviser_crd, f.adv_filing_date)),
      ...filings.map(formDSource),
    ];
    if (filings.some((f) => f.offering_indefinite === 1)) {
      notes.push('Offering size "indefinite" means the fund did not cap the amount it may raise.');
    }
    return envelope(
      {
        funds: funds.map((f) => ({
          pfid: f.pfid,
          name: f.name,
          fund_type: f.fund_type,
          adviser: { crd: f.adviser_crd, name: f.adviser_name },
        })),
        form_d_file_nos: fileNos,
        filings: filings.map((r) => ({
          accession_no: r.accession_no,
          file_no: r.file_no,
          issuer_name: r.issuer_name,
          filing_date: r.filing_date,
          is_amendment: r.is_amendment === 1,
          first_sale_date: r.first_sale_date,
          total_sold_usd: r.total_sold_usd,
          offering_size_usd: r.offering_indefinite === 1 ? "indefinite" : r.total_offering_usd,
          investors: r.investors_count,
          min_investment_usd: r.min_investment_usd,
        })),
        next_cursor: more ? encodeCursor({ o: offset + MAX_ROWS }) : null,
      },
      sources,
      asOf,
      notes,
    );
  },
});
