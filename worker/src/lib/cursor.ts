// Opaque paging cursors: base64 of a small JSON object. Bad cursors are an input error, never a crash.
export function encodeCursor(v: unknown): string {
  return btoa(JSON.stringify(v));
}

export function decodeCursor<T>(cursor: string | undefined, check: (v: unknown) => v is T): T | null {
  if (!cursor) return null;
  let v: unknown;
  try {
    v = JSON.parse(atob(cursor));
  } catch {
    throw new InputError("cursor is not valid; pass the next_cursor value from the previous page");
  }
  if (!check(v)) throw new InputError("cursor is not valid; pass the next_cursor value from the previous page");
  return v;
}

export class InputError extends Error {}
