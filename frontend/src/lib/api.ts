/**
 * The only network code in the frontend.
 *
 * Two calls, both to the demo endpoint. Where they point depends on how the app
 * was built, and the two cases are genuinely different:
 *
 * **Development** — `VITE_API_BASE_URL` is unset, so the base is empty and the
 * requests stay **relative**. The Vite dev server proxies `/api` to the backend,
 * the browser sees a single origin, and no cross-origin request is made. The
 * backend needs no CORS middleware.
 *
 * **A static deployment** — there is no dev server and therefore no proxy, so
 * the base must be the API's absolute origin, supplied at build time. Those
 * requests *are* cross-origin, and the backend must name this app's origin in
 * `FRONTEND_ORIGIN` for a browser to allow them.
 *
 * The URL is never hard-coded here. A deployment address baked into TypeScript
 * would be wrong for every other deployment, including a reviewer's.
 *
 * The fixture pages remain what they were: committed JSON read at build time,
 * rendered with no request at all. A visitor can read the whole evaluation with
 * the backend down; only the live demo needs it.
 */

import type { DemoRun, DemoStatus } from "@/types/demo";

/**
 * The API origin, or `""` in development.
 *
 * Trailing slashes are stripped rather than trusted: `https://host/` joined to
 * `/api/v1/demo` yields `https://host//api/v1/demo`, which some servers route
 * and others 404, and the failure looks nothing like its cause.
 */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");

const BASE = `${API_BASE_URL}/api/v1/demo`;

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
 * The request never reached a server — DNS, TLS, CORS, offline, or a backend
 * that is asleep.
 *
 * Kept distinct from `DemoApiError` because the two need different words. A
 * refusal means the API answered and declined; this means nothing answered, and
 * telling a visitor "the demo is unavailable" would blame the wrong thing.
 *
 * A browser deliberately hides *which* of those it was — a page cannot be
 * allowed to probe the network by reading error details — so this message must
 * not pretend to know.
 */
export class DemoUnreachableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DemoUnreachableError";
  }
}

/** The sentence to show when nothing answered. Deployment-aware. */
function unreachableMessage(): string {
  return API_BASE_URL
    ? "The demo backend did not respond. It is a free-tier deployment that sleeps " +
        "when idle and can take up to a minute to wake — try again shortly. " +
        "Everything else on this site is static and unaffected."
    : "The backend is not running. Start it, then try again — see the README for " +
        "the exact command.";
}

/**
 * `fetch`, with a network failure turned into an error that says so.
 *
 * `fetch` rejects with a bare `TypeError` for every transport failure, which
 * would otherwise surface to a visitor as "Failed to fetch".
 */
async function request(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch {
    throw new DemoUnreachableError(unreachableMessage());
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
  const response = await request(`${BASE}/status`);
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
  const response = await request(`${BASE}/run`, { method: "POST" });
  if (!response.ok) {
    throw new DemoApiError(await detailOf(response), response.status);
  }
  return (await response.json()) as DemoRun;
}
