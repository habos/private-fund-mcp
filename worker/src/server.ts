import { McpServer } from "@modelcontextprotocol/server";
import { InputError } from "./lib/cursor";
import { Db } from "./lib/db";
import { fundHistory } from "./tools/fund_history";
import { listFunds } from "./tools/list_funds";
import { newLaunches } from "./tools/new_launches";
import { resolveManager } from "./tools/resolve_manager";
import type { ToolDef } from "./tools/types";

export const TOOLS = [resolveManager, listFunds, fundHistory, newLaunches] as const;

export const SERVER_INFO = { name: "private-fund-mcp", version: "0.1.0" };

/** One server per request (stateless createMcpHandler). Tools only read D1. */
export function buildServer(d1: D1Database): McpServer {
  const server = new McpServer(SERVER_INFO);
  for (const tool of TOOLS as readonly ToolDef<any>[]) {
    server.registerTool(
      tool.name,
      {
        title: tool.title,
        description: tool.description,
        inputSchema: tool.inputSchema,
        annotations: { title: tool.title, readOnlyHint: true, openWorldHint: false },
      },
      async (input: unknown) => {
        try {
          const result = await tool.run(new Db(d1), input);
          return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
        } catch (err) {
          const msg = err instanceof InputError ? err.message : "Internal error while reading the database.";
          if (!(err instanceof InputError)) console.error(`${tool.name} failed`, err);
          return { isError: true, content: [{ type: "text" as const, text: msg }] };
        }
      },
    );
  }
  return server;
}
