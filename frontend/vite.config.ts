import { cpSync, createReadStream, existsSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";

// CesiumJS loads its web workers, textures and widget CSS at runtime from CESIUM_BASE_URL.
// Serve them from node_modules in dev and copy them into dist/cesium on build.
const CESIUM_BUILD = resolve(__dirname, "node_modules/cesium/Build/Cesium");
const TYPES: Record<string, string> = {
  ".js": "text/javascript", ".json": "application/json", ".css": "text/css", ".png": "image/png",
  ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".wasm": "application/wasm", ".ktx2": "image/ktx2",
};

function cesiumStatic(): Plugin {
  return {
    name: "cesium-static",
    configureServer(server) {
      server.middlewares.use("/cesium", (req, res, next) => {
        const file = join(CESIUM_BUILD, decodeURIComponent((req.url ?? "/").split("?")[0]));
        if (!file.startsWith(CESIUM_BUILD) || !existsSync(file) || !statSync(file).isFile()) return next();
        res.setHeader("Content-Type", TYPES[extname(file)] ?? "application/octet-stream");
        createReadStream(file).pipe(res);
      });
    },
    closeBundle() {
      for (const dir of ["Workers", "Assets", "ThirdParty", "Widgets"]) {
        cpSync(join(CESIUM_BUILD, dir), resolve(__dirname, "dist/cesium", dir), { recursive: true });
      }
    },
  };
}

// The frontend calls /api/*; Vite forwards it to FastAPI on :8000 (no CORS fuss in dev).
export default defineConfig({
  plugins: [cesiumStatic()],
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
