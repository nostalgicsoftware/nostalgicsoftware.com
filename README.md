# nostalgicsoftware.com

Storefront for [nostalgicsoftware.com](https://nostalgicsoftware.com) — eBay seller `nostalgic-software`, trusted since 2001, 100% positive feedback.

## How it works

`update.py` fetches live listings directly from the eBay API and generates static HTML pages deployed to Cloudflare Pages. No CMS, no database, no middlemen.

- **Data source:** eBay Finding API + Shopping API
- **Hosting:** Cloudflare Pages
- **Updates:** GitHub Actions (daily at 6 AM ET, or manually triggered)

## Repo structure

```
index.html                        ← Homepage with full catalog
sitemap.xml                       ← Auto-generated, submitted to Google
robots.txt                        ← Cloudflare-friendly
nostalgicsoftware-hero.mp4        ← Hero video
nostalgicsoftware-hero-thumb.jpg  ← Auto-extracted thumbnail
items/                            ← 85+ individual item pages
update.py                         ← Site generator script
.github/workflows/update-site.yml ← GitHub Actions workflow
```

## Running an update

**Automatic:** runs daily at 6 AM ET via GitHub Actions schedule.

**Manual:** go to Actions tab → "Update NostalgicSoftware.com" → "Run workflow".

## Required GitHub Secrets

Set these in Settings → Secrets and variables → Actions:

| Secret | Description |
|--------|-------------|
| `EBAY_APP_ID` | eBay Production App ID from developer.ebay.com |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token with Pages:Edit permission |
| `CLOUDFLARE_ACCOUNT_ID` | Your Cloudflare account ID (Dashboard → right sidebar) |
