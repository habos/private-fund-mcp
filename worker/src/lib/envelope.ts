// SPEC section 7 response envelope, shared by every tool.
import type { Db } from "./db";

export const DISCLAIMER =
  "Data from public SEC filings as filed. Not investment advice. Fund returns are not in these filings.";

export interface Source {
  type: "form_d" | "form_adv";
  id: string;
  url: string;
  filed: string | null;
}

export interface AsOf {
  form_d: string | null;
  form_adv: string | null;
  form_d_since: string | null;
}

export interface Envelope<T> {
  data: T;
  sources: Source[];
  as_of: { form_d: string | null; form_adv: string | null };
  notes: string[];
  disclaimer: string;
}

export async function readAsOf(db: Db): Promise<AsOf> {
  const rows = await db.all<{ key: string; value: string }>(
    "SELECT key, value FROM meta WHERE key IN ('as_of_form_d', 'as_of_form_adv', 'form_d_since') LIMIT 3",
  );
  const m = Object.fromEntries(rows.map((r) => [r.key, r.value]));
  return { form_d: m.as_of_form_d ?? null, form_adv: m.as_of_form_adv ?? null, form_d_since: m.form_d_since ?? null };
}

export function envelope<T>(data: T, sources: Source[], asOf: AsOf, notes: string[]): Envelope<T> {
  const seen = new Set<string>();
  const unique = sources.filter((s) => {
    const k = `${s.type}:${s.id}`;
    return seen.has(k) ? false : (seen.add(k), true);
  });
  return {
    data,
    sources: unique,
    as_of: { form_d: asOf.form_d, form_adv: asOf.form_adv },
    notes,
    disclaimer: DISCLAIMER,
  };
}

export const formDUrl = (cik: string, accessionNo: string) =>
  `https://www.sec.gov/Archives/edgar/data/${String(Number(cik))}/${accessionNo.replace(/-/g, "")}/`;

export const formAdvUrl = (crd: string) => `https://adviserinfo.sec.gov/firm/summary/${crd}`;

export const formDSource = (r: { accession_no: string; cik: string; filing_date: string }): Source => ({
  type: "form_d",
  id: r.accession_no,
  url: formDUrl(r.cik, r.accession_no),
  filed: r.filing_date,
});

export const formAdvSource = (crd: string, filed: string | null): Source => ({
  type: "form_adv",
  id: crd,
  url: formAdvUrl(crd),
  filed,
});
