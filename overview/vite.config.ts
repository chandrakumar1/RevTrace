import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// This site is a static storytelling layer. It has no API, no proxy and no
// backend dependency by design — every figure it shows is compiled in from a
// local evidence module, so it renders identically whether or not the RevTrace
// application is running.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
