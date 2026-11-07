// Worker entry point: a stateless MCP server at /mcp (Agents SDK createMcpHandler) plus /health.
import { createMcpHandler } from "agents/mcp/server";
import { buildServer } from "./server";

export interface Env {
  DB: D1Database;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      const row = await env.DB.prepare("SELECT value FROM meta WHERE key = ? LIMIT 1")
        .bind("as_of_form_d")
        .first<{ value: string }>();
      return Response.json({ ok: true, as_of_form_d: row?.value ?? null });
    }
    if (url.pathname === "/mcp") {
      // The handler builds a fresh McpServer per request from this factory; no global server, no Durable Objects.
      return createMcpHandler(() => buildServer(env.DB), { route: "/mcp" })(request, env, ctx);
    }
    return new Response("private-fund-mcp: MCP endpoint is /mcp\n", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
