import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Мини-приложение отдаётся бэкендом по пути /app/. В разработке /api проксируется на localhost:8000.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000", "/share": "http://localhost:8000" },
  },
  build: {
    target: "es2020",
    cssCodeSplit: true,
    assetsInlineLimit: 2048,
    chunkSizeWarningLimit: 400,
  },
});
