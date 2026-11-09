import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En dev, l'API FastAPI tourne sur :8000
const api = "http://localhost:8000";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ["/extract", "/quote", "/pricing", "/health", "/samples", "/fields"].map((p) => [p, api]),
    ),
  },
  build: { outDir: "dist", emptyOutDir: true },
});
