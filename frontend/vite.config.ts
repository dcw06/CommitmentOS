import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The compiled dashboard is served by the FastAPI service. Assets live under
// the absolute /app/ prefix so the same bundle renders at /app (live, session
// behind the API) and /demo (seeded judge mode) without duplication.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8080",
      "/auth": "http://localhost:8080",
      "/demo": "http://localhost:8080",
    },
  },
});
