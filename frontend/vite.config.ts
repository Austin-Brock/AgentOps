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
  // Built into ../public, and that directory is committed. Vercel detects this
  // repository as a FastAPI project, and for a backend-framework preset static
  // assets are served from `public/**` as it exists in the repository — not
  // from build output. A CLI deploy uploads the locally built directory and
  // works; a deploy triggered by a git push has no `public/` unless it is
  // committed, and then serves nothing while reporting a successful build.
  //
  // Committing build output is a real cost, paid deliberately: the alternative
  // is a deployment whose two paths to production behave differently.
  // Sourcemaps are off so the committed artifact stays ~235KB rather than 1.3MB.
  build: { outDir: "../public", emptyOutDir: true, sourcemap: false },
  // jsdom so the page itself can be mounted and driven in tests, not only its
  // pure helpers. `globals` keeps the test files free of vitest imports for
  // describe/it/expect, matching how the Python suite reads.
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
