import type { ApiErrorShape } from "./types";

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, code = "request_failed", status = 500) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function cookie(name: string): string | undefined {
  const prefix = `${name}=`;
  return document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix))
    ?.slice(prefix.length);
}

export function requestId(): string {
  return crypto.randomUUID();
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = cookie("ra_csrf");
    if (csrf && !headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", csrf);
  }
  const response = await fetch(path, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    let body: ApiErrorShape = {};
    try {
      body = (await response.json()) as ApiErrorShape;
    } catch {
      // Keep the safe generic message below.
    }
    throw new ApiError(body.error?.message ?? "请求没有成功，请稍后再试", body.error?.code, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiAll<T>(path: string): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  let pageCount = 0;
  do {
    const separator = path.includes("?") ? "&" : "?";
    const cursorQuery: string = cursor ? `${separator}cursor=${encodeURIComponent(cursor)}` : "";
    const page: { items: T[]; next_cursor?: string | null } = await api<{ items: T[]; next_cursor?: string | null }>(`${path}${cursorQuery}`);
    items.push(...page.items);
    cursor = page.next_cursor ?? null;
    pageCount += 1;
    if (pageCount > 1000) throw new ApiError("分页结果异常，已停止继续加载", "pagination_limit", 500);
  } while (cursor);
  return items;
}
