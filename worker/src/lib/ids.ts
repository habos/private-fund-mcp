// Normalizers that mirror the ETL (etl/src/etl/transform.py) so user input matches stored IDs.

const NAME_SUFFIXES = new Set([
  "llc", "lp", "llp", "lllp", "inc", "incorporated", "ltd", "limited", "co", "corp",
  "corporation", "plc", "gmbh", "sa", "ag", "bv", "nv", "sarl", "pte", "pty", "lc", "pc",
]);

/** Same as etl.transform.name_norm: lowercase, punctuation removed, trailing legal suffixes removed. */
export function nameNorm(name: string): string {
  let s = name.toLowerCase().replace(/&/g, " and ");
  s = s.replace(/\b([a-z])\.(?=[a-z]\.)/g, "$1");
  s = s.replace(/\b([a-z])\.([a-z])\b\.?/g, "$1$2");
  const words = s.replace(/[^a-z0-9]+/g, " ").split(" ").filter(Boolean);
  while (words.length > 1 && NAME_SUFFIXES.has(words[words.length - 1])) words.pop();
  return words.join(" ");
}

/** Form D file number as stored (same as etl.transform.normalize_file_no, minus the placeholder check):
 * '021-' + digits without leading zeros, plus any legacy suffix such as '-3N'. */
export function normFileNo(raw: string): string | null {
  const s = raw.replace(/\s+/g, "").toUpperCase();
  const m = /^0*21-(\d+)(-[0-9A-Z]{1,4})?$/.exec(s);
  if (!m) return null;
  const n = m[1].replace(/^0+/, "");
  return n ? `021-${n}${m[2] ?? ""}` : null;
}

/** PFID as stored: '805-' + 10 digits. Accepts the bare 10 digits too. */
export function normPfid(raw: string): string | null {
  const s = raw.replace(/\s+/g, "");
  const m = /^(?:805-)?(\d{10})$/.exec(s);
  return m ? `805-${m[1]}` : null;
}

/** CRD: digits only, leading zeros dropped (CRDs are stored as plain numbers in text). */
export function normCrd(raw: string): string | null {
  const s = raw.trim();
  return /^\d{1,10}$/.test(s) ? String(Number(s)) : null;
}
