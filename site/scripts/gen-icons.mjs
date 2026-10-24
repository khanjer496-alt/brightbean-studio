// Builds public/icons.svg (SVG sprite) and src/data/icon-meta.json from Simple Icons (CC0).
// Run: node scripts/gen-icons.mjs   (simple-icons is a devDependency; the output is committed)
import * as si from 'simple-icons';
import { writeFileSync } from 'node:fs';
const map = {
  instagram: 'siInstagram', facebook: 'siFacebook', tiktok: 'siTiktok', youtube: 'siYoutube', x: 'siX', threads: 'siThreads',
  bluesky: 'siBluesky', pinterest: 'siPinterest', mastodon: 'siMastodon', reddit: 'siReddit', telegram: 'siTelegram',
  discord: 'siDiscord', tumblr: 'siTumblr', lemmy: 'siLemmy', farcaster: 'siFarcaster', twitch: 'siTwitch', kick: 'siKick',
  dribbble: 'siDribbble', devto: 'siDevdotto', medium: 'siMedium', wordpress: 'siWordpress', hashnode: 'siHashnode',
  listmonk: 'siListmonk', claude: 'siClaude', gemini: 'siGooglegemini', cursor: 'siCursor', n8n: 'siN8n', make: 'siMake',
  zapier: 'siZapier', github: 'siGithub', openrouter: 'siOpenrouter', ollama: 'siOllama', anthropic: 'siAnthropic',
};
let sprite = '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">';
const meta = {};
for (const [k, v] of Object.entries(map)) {
  const i = si[v]; if (!i) throw new Error('missing ' + v);
  sprite += `<symbol id="i-${k}" viewBox="0 0 24 24"><path d="${i.path}"/></symbol>`;
  meta[k] = '#' + i.hex;
}
sprite += '</svg>';
writeFileSync('public/icons.svg', sprite);
writeFileSync('src/data/icon-meta.json', JSON.stringify(meta, null, 1));
console.log(Object.keys(map).length, 'icons', sprite.length, 'bytes');
