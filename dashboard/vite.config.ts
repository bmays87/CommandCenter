import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev-time proxy to a locally running prodeo-server (PRODEO_API_PORT=8600).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8600",
        ws: true,
      },
    },
  },
});
