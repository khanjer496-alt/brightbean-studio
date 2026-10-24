// Builds static/js/emoji-data.json from the emojibase-data npm package (MIT).
//
//   node build-emoji-data.mjs <extracted emojibase-data package dir> <output file>
//
// Download the package into an empty directory (`npm pack emojibase-data@<exact version>`), extract it
// there, and run this from a different directory. Keep static/js/emoji-data.LICENSE.txt (the package's
// LICENSE plus the Unicode License) in step with the version used. The picker reads rows as
// [glyph, name, keywords, skinToneGlyphs?]; everything else in the dataset is dropped to keep it small.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [pkgDir, outFile] = process.argv.slice(2);
if (!pkgDir || !outFile) {
  console.error('usage: node build-emoji-data.mjs <emojibase-data dir> <output file>');
  process.exit(1);
}

// Emoji newer than this are left out: fonts on most devices don't have their glyphs yet and they would
// show as empty boxes. Raise it as operating systems catch up.
const MAX_EMOJI_VERSION = 16;
// Emojibase's group numbers, in the order the picker shows them. 2 (skin-tone components) is not a group
// of emoji to pick, and regional-indicator letters have no group at all.
const GROUPS = [
  [0, 'Smileys & Emotion'],
  [1, 'People & Body'],
  [3, 'Animals & Nature'],
  [4, 'Food & Drink'],
  [6, 'Activities'],
  [5, 'Travel & Places'],
  [7, 'Objects'],
  [8, 'Symbols'],
  [9, 'Flags'],
];

const read = (file) => JSON.parse(readFileSync(join(pkgDir, file), 'utf8'));
const data = read('en/data.json');
const shortcodes = read('en/shortcodes/cldr.json');
const version = read('package.json').version;

const words = (text) => text.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

function keywords(item) {
  const code = shortcodes[item.hexcode];
  const extra = [...(item.tags || []), ...(Array.isArray(code) ? code : code ? [code] : [])];
  const known = new Set(words(item.label));
  const kept = [];
  for (const word of extra.flatMap((entry) => words(entry))) {
    if (!known.has(word)) {
      known.add(word);
      kept.push(word);
    }
  }
  return kept.join(' ');
}

function skinTones(item) {
  // Only the single-tone variants; two-person emoji with mixed tones are not offered.
  const tones = new Map();
  for (const skin of item.skins || []) {
    if (Number.isInteger(skin.tone) && skin.version <= MAX_EMOJI_VERSION) tones.set(skin.tone, skin.emoji);
  }
  const ordered = [1, 2, 3, 4, 5].map((tone) => tones.get(tone));
  return ordered.every(Boolean) ? ordered : null;
}

const groups = GROUPS.map(([id, name]) => {
  const emoji = data
    .filter((item) => item.group === id && item.version <= MAX_EMOJI_VERSION)
    .sort((a, b) => a.order - b.order)
    .map((item) => {
      const row = [item.emoji, item.label.toLowerCase(), keywords(item)];
      const tones = skinTones(item);
      if (tones) row.push(tones);
      while (row.length > 2 && row[row.length - 1] === '') row.pop();
      return row;
    });
  return { name, emoji };
});

writeFileSync(outFile, JSON.stringify({ source: `emojibase-data ${version} (MIT), emoji up to version ${MAX_EMOJI_VERSION}`, groups }));
const count = groups.reduce((total, group) => total + group.emoji.length, 0);
console.log(`${count} emoji in ${groups.length} groups from emojibase-data ${version}`);
