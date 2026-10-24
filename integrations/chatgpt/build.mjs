#!/usr/bin/env node
// Builds the three ChatGPT / MCP Apps widgets into self-contained HTML files under apps/mcp/widgets/.
// No dependencies: it inlines shared.css and shared.js into each template in widgets/src.
//   node integrations/chatgpt/build.mjs          write the bundles
//   node integrations/chatgpt/build.mjs --check  fail when the committed bundles are stale
import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "widgets", "src");
const out = join(here, "..", "..", "apps", "mcp", "widgets");
const css = readFileSync(join(src, "shared.css"), "utf8");
const js = readFileSync(join(src, "shared.js"), "utf8").replace(/<\/script/gi, "<\\/script");

const check = process.argv.includes("--check");
let stale = 0;
mkdirSync(out, { recursive: true });
for (const file of readdirSync(src).filter((f) => f.endsWith(".html"))) {
  const html = readFileSync(join(src, file), "utf8")
    .replace("/*__CSS__*/", () => css.trim())
    .replace("/*__SHARED_JS__*/", () => js.trim());
  const target = join(out, file);
  if (check) {
    if (!existsSync(target) || readFileSync(target, "utf8") !== html) {
      console.error(`stale: ${target}`);
      stale += 1;
    }
  } else {
    writeFileSync(target, html);
    console.log(`wrote ${target} (${html.length} bytes)`);
  }
}
process.exit(stale ? 1 : 0);
