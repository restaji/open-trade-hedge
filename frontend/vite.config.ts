import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/** Vite adds crossorigin to script/link tags; strip it for same-origin FastAPI/Vercel serves. */
function stripCrossorigin() {
  return {
    name: "strip-crossorigin",
    transformIndexHtml(html: string) {
      return html.replace(/ crossorigin/g, "");
    },
  };
}

export default defineConfig({
  plugins: [react(), stripCrossorigin()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    // Vercel serves `public/` at the edge; web.py reads the same dir locally.
    outDir: "../public",
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
