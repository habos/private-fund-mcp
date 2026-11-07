// End to end: JSON-RPC over HTTP to /mcp on the real Worker (local D1 with the fixture rows).
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { start, type Ctx } from "./harness";

let ctx: Ctx;
beforeAll(async () => {
  ctx = await start();
}, 60_000);
afterAll(() => ctx?.server.close());

let nextId = 1;
async function rpc(method: string, params: unknown = {}): Promise<any> {
  const res = await ctx.server.fetch("http://localhost/mcp", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      "mcp-protocol-version": "2025-06-18",
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }),
  });
  const text = await res.text();
  expect(res.status, text).toBe(200);
  // responseMode "auto" may answer as JSON or as one SSE event.
  const json = res.headers.get("content-type")?.includes("text/event-stream")
    ? JSON.parse(text.split("\n").find((l) => l.startsWith("data: "))!.slice(6))
    : JSON.parse(text);
  return json;
}

describe("MCP endpoint", () => {
  it("initializes and lists the four read-only tools", async () => {
    const init = await rpc("initialize", {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "vitest", version: "0" },
    });
    expect(init.result.serverInfo.name).toBe("private-fund-mcp");
    const list = await rpc("tools/list");
    const tools = list.result.tools;
    expect(tools.map((t: any) => t.name).sort()).toEqual(["fund_history", "list_funds", "new_launches", "resolve_manager"]);
    for (const t of tools) {
      expect(t.title).toBeTruthy();
      expect(t.annotations.readOnlyHint).toBe(true);
      expect(t.inputSchema.type).toBe("object");
    }
  });

  it("calls a tool and returns the envelope as text", async () => {
    const r = await rpc("tools/call", { name: "resolve_manager", arguments: { query: "rtw" } });
    const out = JSON.parse(r.result.content[0].text);
    expect(out.data.advisers[0].crd).toBe("153232");
    expect(out.disclaimer).toMatch(/Not investment advice/);
  });

  it("reports a bad limit as a tool error", async () => {
    const r = await rpc("tools/call", { name: "resolve_manager", arguments: { query: "rtw", limit: 100 } });
    const failed = r.error !== undefined || r.result?.isError === true;
    expect(failed).toBe(true);
  });

  it("reports a bad cursor as a tool error, not a crash", async () => {
    const r = await rpc("tools/call", { name: "list_funds", arguments: { crd: "293329", cursor: "%%%" } });
    expect(r.result.isError).toBe(true);
    expect(r.result.content[0].text).toMatch(/cursor is not valid/);
  });
});
