import { defineConfig } from "vite";

// The frontend calls /api/*; Vite forwards it to FastAPI on :8000 (no CORS fuss in dev).
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
