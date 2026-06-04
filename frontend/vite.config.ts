import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During dev, the FastAPI backend runs on :8000 and Vite on :5173.
// Proxy /api and /ws so the frontend code can use relative URLs.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws":  { target: "ws://127.0.0.1:8000",   ws: true, changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
