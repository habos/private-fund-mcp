// Worker entry point. The MCP tools arrive in prompt 4; for now it only proves the D1 binding works.
export interface Env {
  DB: D1Database;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      const row = await env.DB.prepare("SELECT value FROM meta WHERE key = ? LIMIT 1")
        .bind("as_of_form_d")
        .first<{ value: string }>();
      return Response.json({ ok: true, as_of_form_d: row?.value ?? null });
    }
    return new Response("private-fund-mcp: MCP endpoint coming soon\n", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
