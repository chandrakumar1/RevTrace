/// <reference types="vite/client" />

/**
 * Typed build-time environment.
 *
 * Only `VITE_`-prefixed variables reach the browser bundle; Vite refuses the
 * rest, which is the mechanism that keeps a server-side secret out of a static
 * build. Nothing secret belongs here regardless — everything in this interface
 * is inlined into JavaScript that anyone can read.
 */
interface ImportMetaEnv {
  /**
   * Absolute origin of the RevTrace API, for a deployed build.
   *
   * Left undefined in development, where requests stay relative and the Vite
   * dev proxy forwards `/api` to the backend. Set at build time for a static
   * deployment, where no proxy exists: e.g. `https://api.example.com`.
   *
   * Scheme and host only — no trailing slash, no path. A trailing slash is
   * tolerated and stripped by `lib/api.ts` rather than producing `//api/v1`.
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
