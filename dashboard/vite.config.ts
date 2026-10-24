import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build goes inside the Python package, so `openport serve` (and the Docker image) serve it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../src/openport/server/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
