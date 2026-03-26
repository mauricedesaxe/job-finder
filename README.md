# scrapio

Automated job search and enrichment pipeline for crypto/web3 engineering positions. Searches job boards (Ashby, Lever, Greenhouse), evaluates listings with Claude, and stores qualified jobs in Notion.

## Job Statuses

| Status | Set by | Meaning |
|--------|--------|---------|
| `To Review` | System | New job, needs human review |
| `Applied` | User/System | Applied to this job (auto-set if Application Date is filled) |
| `Skipped` | User | Job isn't a fit, but company is fine |
| `Rejected` | System | LLM evaluation rejected this job |
| `Company Applied` | System | Another job at this company was applied to recently |
| `Company Blocked` | User | Company is not a fit (e.g., not remote EU) |
| `Archived` | System/User | Done with this listing |

After scraping, a reconciliation pass runs automatically:

1. **Auto-mark Applied**: Jobs with an Application Date but wrong status get set to "Applied"
2. **Unstale Company Applied**: Recent (30 days) "Company Applied" jobs are set back to "To Review" if the application is now >6 months old
3. **Propagate Company Applied**: If you applied to a company recently, all "To Review" jobs from that company get marked "Company Applied"
4. **Archive blocked companies**: "To Review" jobs from "Company Blocked" companies get archived

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
