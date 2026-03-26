# scrapio

Automated job search and enrichment pipeline for crypto/web3 engineering positions. Searches job boards (Ashby, Lever, Greenhouse), evaluates listings with Claude, and stores qualified jobs in Notion.

## Local Setup

```bash
bun install
cp .env.example .env  # fill in your API keys
bun run scrape
```

## Deploy to Railway (Cron Job)

1. Create a new project on [railway.app](https://railway.app) and connect your GitHub repo
2. Railway auto-detects the `Dockerfile` and builds from it
3. In the service's **Variables** tab, add:
   - `NOTION_TOKEN`
   - `NOTION_DATABASE_ID`
   - `JINA_API_KEY`
   - `ANTHROPIC_API_KEY`
4. In **Settings**, change the service type to **Cron Job**
5. Set the cron schedule to `0 8 */2 * *` (every 2 days at 8 AM UTC)
6. Increase the job timeout to **45 minutes** (the scraper can take 10-30+ min depending on results)

## Testing

```bash
bun test
```
