// Generates public/og.png (1200x630). Run once: node scripts/og.mjs
import sharp from 'sharp';
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">
<defs><radialGradient id="g" cx="50%" cy="0%" r="80%"><stop offset="0" stop-color="#7357ff" stop-opacity=".55"/><stop offset="1" stop-color="#080a0f" stop-opacity="0"/></radialGradient></defs>
<rect width="1200" height="630" fill="#080a0f"/><rect width="1200" height="630" fill="url(#g)"/>
<g transform="translate(80 80)"><rect width="72" height="72" rx="18" fill="#11131b"/>
<g transform="scale(1.125)"><path d="M14.5 38.5C19.7 21.4 39.3 15.1 50.3 24.5C58.4 31.5 48.5 46.7 33 47.8C19.8 48.8 8.7 41.8 13.4 31.1C17.8 21.1 34 17.2 45.4 22.5" stroke="#7357FF" stroke-width="4.5" stroke-linecap="round" fill="none"/><circle cx="49.2" cy="23.7" r="4.7" fill="#C8FF4D"/></g></g>
<text x="172" y="130" fill="#f8f7f2" font-family="Arial, Helvetica, sans-serif" font-size="40" font-weight="800">PostDelegate</text>
<text x="80" y="300" fill="#f8f7f2" font-family="Arial, Helvetica, sans-serif" font-size="76" font-weight="800">The safest way to let</text>
<text x="80" y="390" fill="#c3b5ff" font-family="Arial, Helvetica, sans-serif" font-size="76" font-weight="800">AI run your social media</text>
<text x="80" y="470" fill="#b0b6c7" font-family="Arial, Helvetica, sans-serif" font-size="34">with any AI you choose.</text>
<text x="80" y="560" fill="#c8ff4d" font-family="Arial, Helvetica, sans-serif" font-size="28" font-weight="700">postdelegate.com</text></svg>`;
await sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toFile('public/og.png');
