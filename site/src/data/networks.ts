// Source of truth: providers/__init__.py PROVIDER_REGISTRY and each provider's caption rules.
// unit: codepoints | utf16 | utf8_bytes | graphemes | codepoints_emoji_bytes | weighted (X)
export interface Network {
  key: string; name: string; color: string; abbr: string; limit: number;
  unit: string; urlCost?: number; noMentionDomain?: boolean; scheduler?: string; note?: string;
}
export const NETWORKS: Network[] = [
  { key: 'instagram', name: 'Instagram', color: '#c4256e', abbr: 'Ig', limit: 2200, unit: 'codepoints', scheduler: 'instagram-scheduler' },
  { key: 'facebook', name: 'Facebook', color: '#1877f2', abbr: 'Fb', limit: 63206, unit: 'codepoints', scheduler: 'facebook-scheduler' },
  { key: 'linkedin', name: 'LinkedIn', color: '#0a66c2', abbr: 'in', limit: 3000, unit: 'codepoints', scheduler: 'linkedin-scheduler' },
  { key: 'tiktok', name: 'TikTok', color: '#25f4ee', abbr: 'Tk', limit: 2200, unit: 'utf16', scheduler: 'tiktok-scheduler' },
  { key: 'youtube', name: 'YouTube', color: '#ff0000', abbr: 'Yt', limit: 5000, unit: 'utf8_bytes', scheduler: 'youtube-scheduler' },
  { key: 'x', name: 'X', color: '#e7e7ea', abbr: 'X', limit: 280, unit: 'weighted', note: 'Premium accounts get 25,000.' },
  { key: 'threads', name: 'Threads', color: '#9aa0b4', abbr: 'Th', limit: 500, unit: 'codepoints_emoji_bytes', scheduler: 'threads-scheduler' },
  { key: 'bluesky', name: 'Bluesky', color: '#0085ff', abbr: 'Bs', limit: 300, unit: 'graphemes', scheduler: 'bluesky-scheduler' },
  { key: 'pinterest', name: 'Pinterest', color: '#e60023', abbr: 'Pi', limit: 500, unit: 'codepoints', scheduler: 'pinterest-scheduler' },
  { key: 'mastodon', name: 'Mastodon', color: '#6364ff', abbr: 'Ma', limit: 500, unit: 'codepoints', urlCost: 23, noMentionDomain: true, scheduler: 'mastodon-scheduler' },
  { key: 'google_business', name: 'Google Business', color: '#4285f4', abbr: 'Gb', limit: 1500, unit: 'codepoints', scheduler: 'google-business-scheduler' },
  { key: 'reddit', name: 'Reddit', color: '#ff4500', abbr: 'Re', limit: 40000, unit: 'codepoints' },
  { key: 'telegram', name: 'Telegram', color: '#26a5e4', abbr: 'Tg', limit: 4096, unit: 'codepoints' },
  { key: 'discord', name: 'Discord', color: '#5865f2', abbr: 'Dc', limit: 2000, unit: 'codepoints' },
  { key: 'slack', name: 'Slack', color: '#8c3a90', abbr: 'Sl', limit: 4000, unit: 'codepoints' },
  { key: 'tumblr', name: 'Tumblr', color: '#5a7396', abbr: 'Tu', limit: 4096, unit: 'codepoints' },
  { key: 'lemmy', name: 'Lemmy', color: '#00bc8c', abbr: 'Le', limit: 10000, unit: 'codepoints' },
  { key: 'nostr', name: 'Nostr', color: '#8e30eb', abbr: 'No', limit: 5000, unit: 'codepoints' },
  { key: 'farcaster', name: 'Farcaster', color: '#8a63d2', abbr: 'Fc', limit: 320, unit: 'utf8_bytes' },
  { key: 'twitch', name: 'Twitch', color: '#9146ff', abbr: 'Tw', limit: 500, unit: 'codepoints' },
  { key: 'kick', name: 'Kick', color: '#53fc18', abbr: 'Ki', limit: 500, unit: 'codepoints' },
  { key: 'dribbble', name: 'Dribbble', color: '#ea4c89', abbr: 'Dr', limit: 4000, unit: 'codepoints' },
  { key: 'devto', name: 'DEV.to', color: '#e7e7ea', abbr: 'De', limit: 25000, unit: 'codepoints', scheduler: 'devto-scheduler' },
  { key: 'medium', name: 'Medium', color: '#e7e7ea', abbr: 'Me', limit: 50000, unit: 'codepoints' },
  { key: 'wordpress', name: 'WordPress', color: '#21759b', abbr: 'Wp', limit: 50000, unit: 'codepoints' },
  { key: 'hashnode', name: 'Hashnode', color: '#2962ff', abbr: 'Hn', limit: 50000, unit: 'codepoints' },
  { key: 'listmonk', name: 'Listmonk (email)', color: '#0055d4', abbr: 'Lm', limit: 100000, unit: 'codepoints' },
];
export const NETWORK_COUNT = NETWORKS.length; // 27 (Instagram and LinkedIn each have two connection routes)
