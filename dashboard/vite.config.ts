import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build goes inside the Python package, so `openport serve` (and the Docker image) serve it. `--mode static`
// builds the replay-only page instead (scripts/static_replay.py adds the recordings), for any static host.
export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  base: mode === "static" ? "./" : "/",
  build: { outDir: mode === "static" ? "../site" : "../src/openport/server/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
}));
