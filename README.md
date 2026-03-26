# scrapio

Automated job search and enrichment pipeline for crypto/web3 engineering positions. Searches job boards (Ashby, Lever, Greenhouse), evaluates listings with Claude, and stores qualified jobs in Notion.

After scraping, a reconciliation pass runs automatically to keep flag status consistent across the Notion database:

1. **Unflag stale**: Jobs marked "Flagged" are set back to "To Review" if the company's most recent application is older than 6 months.
2. **Propagate flags**: If any job from a company is "Flagged", all other "To Review" jobs from that company are flagged too — catches cases where an Application Date was added manually but only one job was updated.
3. **Flag applied companies**: If any job from a company has a recent Application Date (within 6 months), all "To Review" jobs from that company are flagged.

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
