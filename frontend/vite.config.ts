import react from "@vitejs/plugin-react";
// From vitest/config rather than vite: same function, plus the `test` key below.
import { defineConfig } from "vitest/config";

// The dev server proxies the API rather than the backend allowing cross-origin
// requests. In development there is then no CORS configuration to get wrong,
// and in production the static site and the API sit behind one origin on
// DigitalOcean App Platform — so the same-origin assumption holds in both.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/demo": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
  // jsdom so the page itself can be mounted and driven in tests, not only its
  // pure helpers. `globals` keeps the test files free of vitest imports for
  // describe/it/expect, matching how the Python suite reads.
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
