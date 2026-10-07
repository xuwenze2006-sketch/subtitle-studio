import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/",
  plugins: [react()],
  build: { outDir: "dist" },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.js"],
    pool: "threads",
    maxWorkers: 1,
  },
});
