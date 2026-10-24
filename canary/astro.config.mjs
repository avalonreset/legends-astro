import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  site: "https://canary.example.test",
  compressHTML: false,
  trailingSlash: "never",
  build: {
    format: "file",
    inlineStylesheets: "never",
  },
});
