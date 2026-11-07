import type { z } from "zod";
import type { Db } from "../lib/db";
import type { Envelope } from "../lib/envelope";

export interface ToolDef<S extends z.ZodObject> {
  name: string;
  title: string;
  description: string;
  inputSchema: S;
  run(db: Db, input: z.infer<S>): Promise<Envelope<unknown>>;
}

export const defineTool = <S extends z.ZodObject>(t: ToolDef<S>) => t;
