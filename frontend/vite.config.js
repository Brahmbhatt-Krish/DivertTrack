import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Backend runs on :8000 (see Makefile); the dev server proxies API and
// WebSocket calls there so the frontend can always call same-origin paths.
//
// Targets are 127.0.0.1, never "localhost", deliberately: on Windows
// "localhost" resolves to ::1 (IPv6) first, uvicorn listens on IPv4 only,
// and every single proxied request then waits ~2s for the IPv6 attempt to
// fail before retrying on IPv4. That penalty applied to every button click
// and made the app feel completely unresponsive.
const BACKEND = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // shadcn/ui components import each other through the "@/" alias.
  resolve: {
    alias: { "@": path.resolve(path.dirname(fileURLToPath(import.meta.url)), "src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/health": BACKEND,
      "/transports": BACKEND,
      "/hospitals": BACKEND,
      "/invariant": BACKEND,
      "/policy": BACKEND,
      "/demo": BACKEND,
      "/ai": BACKEND,
      "/events": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
