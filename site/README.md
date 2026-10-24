# PostDelegate marketing site

Astro 5 (static output) + Tailwind v4. Zero client JS by default; small scripts only for the pricing toggle, mobile nav and the caption-checker and UTM tools. FAQ uses `<details>`.

## Build

```sh
cd site
pnpm install
pnpm build        # output: site/dist
pnpm dev          # local dev server
```

Facts live in `src/data/` (plans from `apps/billing/plans.py`, networks from `providers/`, FAQ). Change them there and every page, JSON-LD, `llms.txt` and `llms-full.txt` follow.

## Cloudflare Pages settings

| Setting | Value |
| --- | --- |
| Root directory | `site` |
| Build command | `pnpm install --frozen-lockfile && pnpm build` |
| Build output directory | `dist` |
| Environment variable | `NODE_VERSION` = `22` (Astro 5 supports Node 18.20+, 20.3+, 22+) |

`public/_redirects` sends `/privacy`, `/terms`, `/licenses` and `/data-deletion` (301) to the app's canonical `/legal/*` pages and rewrites `/sitemap.xml` to the generated sitemap. `public/_headers` sets security headers and long caching for hashed assets. `www.postdelegate.com` to apex is not expressible in `_redirects`; add a Cloudflare Redirect Rule (or attach both domains to the Pages project and redirect www to apex in the dashboard).

## Local verification helpers

```sh
node scripts/serve.mjs 4321      # serves dist with clean URLs and _redirects semantics
node scripts/check-urls.mjs      # every old URL and internal link must be 200 or 301 to 200
node scripts/og.mjs              # regenerates public/og.png
```

`screens/` holds review screenshots and `reference/` the inventory of the previous site. Neither is deployed (they are outside `public/`).

## Notes

- Network badges use brand colours with initials (as the app does) rather than third-party logos.
- Product screenshots are from `docs/design/screens/wave2/` in the app repo.
- Competitor statements are hedged and must be re-verified before launch; see the compare pages.
