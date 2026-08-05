// Vite only processes index.html + its JS/CSS/asset graph. static/fonts,
// static/icons, and static/sw.js aren't referenced in a way Vite resolves
// (fonts/icons via absolute "/static/..." URLs it deliberately leaves alone,
// sw.js served by its own FastAPI route reading straight off disk) so they
// need to land in the build output unchanged for app/main.py to serve
// dist/static as a drop-in replacement for static/ once built. (manifest.
// webmanifest lives under static/public/ instead, which Vite's publicDir
// convention already copies into dist/static/ automatically.) Run after
// `vite build`.
import { cpSync, existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, "..");
const src = join(root, "static");
const dest = join(root, "dist", "static");

const PASSTHROUGH = ["fonts", "icons", "sw.js"];

for (const name of PASSTHROUGH) {
  const from = join(src, name);
  if (!existsSync(from)) continue;
  cpSync(from, join(dest, name), { recursive: true });
}
