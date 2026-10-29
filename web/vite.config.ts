import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the API runs on :8000 (uvicorn) and Vite proxies /api to it. Build: FastAPI serves web/dist itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
