import { defineConfig } from "vite";
import { resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  root: "static",
  base: "/static/",
  build: {
    outDir: "../dist/static",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, "static/index.html"),
      },
    },
  },
  server: {
    proxy: {
      "^/(events|auth|admin|me|health|llm-usage|manifest.webmanifest|sw.js)": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
