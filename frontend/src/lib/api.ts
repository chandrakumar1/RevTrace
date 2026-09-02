/**
 * The only network code in the frontend.
 *
 * Two calls, both to the demo endpoint, both through a **relative** `/api`
 * path. Relative is load-bearing: the Vite dev server proxies `/api` to the
 * backend so the browser sees a single origin, and an absolute
 * `http://localhost:8000` would bypass the proxy, become a cross-origin request
 * and be refused — the backend has no CORS middleware, deliberately.
 *
 * The fixture pages remain what they were: committed JSON read at build time.
 * Nothing here touches them.
 */

import type { DemoRun, DemoStatus } from "@/types/demo";

const BASE = "/api/v1/demo";

/** A backend refusal, carrying the message the backend chose to show. */
export class DemoApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "DemoApiError";
    this.status = status;
  }
}

/**
 * The backend's `detail`, when it sent one.
 *
 * FastAPI puts its message under `detail`; anything else is not a message this
 * app should show, so a generic sentence is used instead of guessing.
 */
async function detailOf(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string" && detail) {
        return detail;
      }
    }
  } catch {
    // A non-JSON error body tells us nothing worth showing.
  }
  return `The demo endpoint returned ${response.status}.`;
}

/** Whether the demo is available, and why not when it is not. */
export async function fetchDemoStatus(): Promise<DemoStatus> {
  const response = await fetch(`${BASE}/status`);
  if (!response.ok) {
    throw new DemoApiError(await detailOf(response), response.status);
  }
  return (await response.json()) as DemoStatus;
}

/**
 * Run the demo.
 *
 * There is no argument, because the endpoint takes none — in particular there
 * is no way to ask it to keep its rows. The run is always rolled back.
 */
export async function runDemo(): Promise<DemoRun> {
  const response = await fetch(`${BASE}/run`, { method: "POST" });
  if (!response.ok) {
    throw new DemoApiError(await detailOf(response), response.status);
  }
  return (await response.json()) as DemoRun;
}
