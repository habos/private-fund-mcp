// Thin D1 wrapper: every query goes through here so we can cap queries per request and see rows_read.
export const MAX_ROWS = 25; // SPEC section 7: max 25 results per page
export const MAX_QUERIES = 6; // per tool call; keeps each request far under the 10 ms CPU limit
export const MAX_ROWS_READ = 1000; // SPEC section 6: tests fail any query that reads more

export interface QueryStat {
  sql: string;
  rows_read: number;
}

export class Db {
  readonly stats: QueryStat[] = [];
  constructor(private readonly d1: D1Database) {}

  async all<T>(sql: string, ...params: unknown[]): Promise<T[]> {
    if (this.stats.length >= MAX_QUERIES) {
      throw new Error(`query budget exceeded (${MAX_QUERIES} per request)`);
    }
    const res = await this.d1.prepare(sql).bind(...params).all<T>();
    const rowsRead = Number(res.meta?.rows_read ?? 0);
    this.stats.push({ sql, rows_read: rowsRead });
    if (rowsRead > MAX_ROWS_READ) console.warn(`rows_read ${rowsRead} > ${MAX_ROWS_READ}: ${sql}`);
    return res.results;
  }

  async first<T>(sql: string, ...params: unknown[]): Promise<T | null> {
    const rows = await this.all<T>(sql, ...params);
    return rows[0] ?? null;
  }
}

/** "?, ?, ?" for an IN list. */
export const placeholders = (n: number) => Array.from({ length: n }, () => "?").join(", ");
