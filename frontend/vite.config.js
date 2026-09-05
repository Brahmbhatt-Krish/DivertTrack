import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Backend runs on :8000 (see Makefile); the dev server proxies API and
// WebSocket calls there so the frontend can always call same-origin paths.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/health": "http://localhost:8000",
      "/transports": "http://localhost:8000",
      "/demo": "http://localhost:8000",
      "/ai": "http://localhost:8000",
      "/events": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
});
