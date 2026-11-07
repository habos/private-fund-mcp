import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const strip = (sql: string) =>
  sql
    .split("\n")
    .filter((l) => !l.startsWith("--"))
    .join("\n")
    .replace(/ IF NOT EXISTS/g, "")
    .trim();

describe("worker/schema.sql", () => {
  it("matches the ETL schema (SPEC section 4) apart from IF NOT EXISTS", () => {
    const worker = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");
    const etl = readFileSync(new URL("../../etl/src/etl/schema.sql", import.meta.url), "utf8");
    expect(strip(worker)).toBe(strip(etl));
  });
});
