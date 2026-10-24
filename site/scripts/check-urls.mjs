// Checks every old live-site URL and every internal link in dist against the local server (node scripts/serve.mjs).
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
const base = 'http://localhost:4321';
const old = ['/', '/about', '/terms', '/privacy', '/licenses', '/support', '/data-deletion', '/status', '/agents', '/mcp', '/api', '/claude-cowork', '/codex', '/platforms', '/instagram-scheduler', '/linkedin-scheduler', '/facebook-scheduler', '/tiktok-scheduler', '/youtube-scheduler', '/bluesky-scheduler', '/threads-scheduler', '/pinterest-scheduler', '/google-business-scheduler', '/mastodon-scheduler', '/devto-scheduler', '/tools', '/tools/caption-checker', '/tools/utm-builder', '/compare', '/compare/postiz', '/compare/post-bridge', '/sitemap.xml'];
const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));
const links = new Set();
for (const f of walk(new URL('../dist', import.meta.url).pathname).filter((f) => f.endsWith('.html')))
  for (const m of readFileSync(f, 'utf8').matchAll(/href="(\/[^"#?]*)/g)) links.add(m[1]);
const all = [...new Set([...old, ...links])];
let bad = 0;
for (const p of all) {
  let r = await fetch(base + p, { redirect: 'manual' }); let note = '';
  if (r.status === 301) { const to = r.headers.get('location'); note = ' -> ' + to; r = to.startsWith('http') ? await fetch(to, { redirect: 'manual' }) : await fetch(base + to, { redirect: 'manual' }); note += ' ' + r.status; }
  const ok = r.status === 200; if (!ok) bad++;
  if (old.includes(p) || !ok) console.log(ok ? 'OK ' : 'BAD', p, note);
}
console.log(`${all.length} urls checked, ${bad} bad`);
