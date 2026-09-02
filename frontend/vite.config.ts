import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Tailwind v4 is configured through its Vite plugin and a CSS-first `@import`,
// so there is deliberately no tailwind.config.js or postcss.config.js.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  // The dev server proxies `/api` to the backend, so the browser sees one
  // origin and there is no cross-origin request to permit. That is deliberately
  // instead of CORS middleware on FastAPI: an origin allowlist is a permanent
  // piece of production configuration, added to a frozen application, to solve
  // a problem that only exists while two dev servers run side by side.
  //
  // It follows that the frontend must only ever use relative `/api` paths. A
  // hard-coded `http://localhost:8000` would bypass the proxy, hit the
  // cross-origin case this avoids, and fail in the browser.
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },
});
