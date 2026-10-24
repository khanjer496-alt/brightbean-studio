// Local static server that mimics Cloudflare Pages: clean URLs and _redirects (301 and 200 rewrites).
import http from 'node:http';
import { readFileSync, existsSync, statSync } from 'node:fs';
import { join, extname } from 'node:path';
const root = new URL('../dist', import.meta.url).pathname;
const port = Number(process.argv[2] || 4321);
const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp', '.avif': 'image/avif', '.xml': 'application/xml', '.txt': 'text/plain; charset=utf-8', '.ico': 'image/x-icon', '.webmanifest': 'application/manifest+json', '.json': 'application/json' };
const rules = readFileSync(join(root, '_redirects'), 'utf8').split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#')).map((l) => l.split(/\s+/));
http.createServer((req, res) => {
  const path = decodeURIComponent(req.url.split('?')[0]);
  const rule = rules.find((r) => r[0] === path);
  if (rule) {
    const status = Number(rule[2] || 301);
    if (status === 200) return send(res, join(root, rule[1]));
    res.writeHead(status, { Location: rule[1] }); return res.end();
  }
  const candidates = [join(root, path), join(root, path + '.html'), join(root, path, 'index.html')];
  const file = candidates.find((f) => existsSync(f) && statSync(f).isFile());
  if (!file) { res.writeHead(404); return res.end('404'); }
  send(res, file);
}).listen(port, () => console.log(`http://localhost:${port}`));
function send(res, file) { res.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream' }); res.end(readFileSync(file)); }
