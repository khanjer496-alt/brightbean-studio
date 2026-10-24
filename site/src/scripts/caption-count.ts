// Port of static/js/caption-count.js and providers/x.py weighted_length from the app repo.
export interface Rule { unit: string; urlCost?: number; noMentionDomain?: boolean }

const URL_RE = /(?<![=/\p{L}\p{N}_])https?:\/\/[^\s<>]*[^\s<>.,;:!?'")\]}]/gu;
const MENTION_RE = /(?<![=/\p{L}\p{N}_])@([A-Za-z0-9_]+(?:[A-Za-z0-9_.-]+[A-Za-z0-9_]+)?)(?:@[\p{L}\p{N}_.-]+[\p{L}\p{N}_])/gu;
const EMOJI_RE = /[\p{Emoji_Presentation}️\u{1F000}-\u{1FAFF}]/u;
const enc = new TextEncoder();
let seg: Intl.Segmenter | null = null;
const graphemes = (t: string) => { seg ??= new Intl.Segmenter(undefined, { granularity: 'grapheme' }); return Array.from(seg.segment(t), (p) => p.segment); };

function count(text: string, unit: string): number {
  if (unit === 'utf16') return text.length;
  if (unit === 'utf8_bytes') return enc.encode(text).length;
  if (unit === 'graphemes') return graphemes(text).length;
  if (unit === 'codepoints_emoji_bytes') {
    let n = 0;
    for (const g of graphemes(text)) n += EMOJI_RE.test(g) ? enc.encode(g).length : Array.from(g).length;
    return n;
  }
  return Array.from(text).length;
}

// X: twitter-text v3 weighting; a URL weighs 23, an emoji sequence 2, ranges below weigh 1, the rest 2.
const LIGHT: [number, number][] = [[0, 4351], [8192, 8205], [8208, 8223], [8242, 8247]];
const X_URL = new RegExp(
  '(?<![a-z0-9_@.-])(?:(?:https?://|www\\.)\\S*[^\\s.,!?:;"\')\\]}>]|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\\.)+(?:com|net|org|io|co|ai|dev|app|me|ly|gg|tv|info|biz|edu|gov|xyz|news|blog|shop|link|site|online|uk|de|fr|es|it|nl|ca|au|in|jp|br|ru|ch|se|no|us|eu|ae|sa)(?![a-z0-9_])(?:/\\S*[^\\s.,!?:;"\')\\]}>])?)', 'gi');
const isRI = (c: number) => c >= 0x1f1e6 && c <= 0x1f1ff;
const isCont = (c: number) => c === 0xfe0e || c === 0xfe0f || c === 0x20e3 || (c >= 0x1f3fb && c <= 0x1f3ff) || (c >= 0xe0020 && c <= 0xe007f);
function weigh(text: string): number {
  const cps = Array.from(text, (c) => c.codePointAt(0)!);
  let total = 0, i = 0;
  while (i < cps.length) {
    const cp = cps[i]; const next = i + 1 < cps.length ? cps[i + 1] : undefined;
    if (isRI(cp) && next !== undefined && isRI(next)) { total += 2; i += 2; continue; }
    if ((cp >= 0x1f000 && cp <= 0x1faff) || (cp >= 0x2190 && cp <= 0x2bff) || next === 0xfe0f || next === 0x20e3) {
      total += 2; i++;
      while (i < cps.length) { if (isCont(cps[i])) i++; else if (cps[i] === 0x200d && i + 1 < cps.length) i += 2; else break; }
      continue;
    }
    total += LIGHT.some(([a, b]) => cp >= a && cp <= b) ? 1 : 2; i++;
  }
  return total;
}
function weightedLength(text: string): number {
  text = (text || '').normalize('NFC');
  let total = 0, pos = 0;
  for (const m of text.matchAll(X_URL)) { total += weigh(text.slice(pos, m.index)) + 23; pos = m.index! + m[0].length; }
  return total + weigh(text.slice(pos));
}

export function captionLength(text: string, rule: Rule): number {
  text = text || '';
  if (rule.unit === 'weighted') return weightedLength(text);
  if (rule.noMentionDomain) text = text.replace(MENTION_RE, '@$1');
  if (rule.urlCost == null) return count(text, rule.unit);
  let total = 0, pos = 0;
  for (const m of text.matchAll(URL_RE)) { total += count(text.slice(pos, m.index), rule.unit) + rule.urlCost; pos = m.index! + m[0].length; }
  return total + count(text.slice(pos), rule.unit);
}
