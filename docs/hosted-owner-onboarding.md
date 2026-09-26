# Set up one hosted Job Finder owner

Use this runbook for a new person who will use a private instance that you operate. Start from the current `main` branch. Each owner gets a separate Railway project and PostgreSQL database.

## Before the setup call

1. Ask for target roles, work location and time zones, salary floor, preferred technologies, and definite rejections. Write down a few job examples the owner would pursue and reject. You will use these to check the first results and prepare qualification evidence.
2. Have working Jina, OpenRouter, and Typesafe API keys. Set provider account spending limits before the first search. Agree on a monthly Job Finder budget and a per-run allowance with the owner.
3. Install Python 3.12 and the Railway CLI. Sign in with `railway login`, then run `railway whoami --json` to get the ID of the Railway workspace that will pay for this instance.

## Provision the private instance

From a current clone of this repository, run:

```sh
python3 scripts/deploy_railway.py --name job-finder-OWNER-SLUG --workspace WORKSPACE_ID
```

Use a unique project name. The command creates PostgreSQL, `review`, `dagster-webserver`, and `dagster-daemon`. It prints the Railway project URL, the review URL, and a one-time owner bootstrap token. Keep the token private. Do not rerun the command after it has created services: a new run creates a new project. See [Deploy a hosted instance for an owner](../README.md#deploy-a-hosted-instance-for-an-owner) for recovery from a partial deployment.

Open `REVIEW_URL/readyz` and confirm it says `ready`. Open `REVIEW_URL`; a fresh instance redirects to `/setup`. Only `review` should have a public domain. Keep PostgreSQL and Dagster private.

## Complete the setup screens with the owner

1. At `/setup`, enter the bootstrap token. Have the owner create and retain the owner password. Confirm that the app opens **Connect the services that do the work**.
2. Enter each provider key on `/setup/providers` and select **Validate and save** for Jina, OpenRouter, and Typesafe. Continue only after all three show **Validated**. Validation makes real provider requests, including small paid model requests.
3. At `/configuration`, edit **Find jobs** with the owner's keywords and job boards. Select **Save acquisition draft**, **Publish acquisition**, then **Activate acquisition**.
4. Edit **Choose relevant jobs** with the owner's personal criteria and target profiles. Select **Save qualification draft**, then **Publish qualification**. Select **Continue to budget**. Qualification is not active for daily runs yet.
5. At `/setup/budget`, set the monthly admission budget, the allowance per run, and the maximum jobs per run. Select **Save budget and prepare test**. Confirm that `/setup/test-search` opens.
6. Select **Start test search**. The page moves through queued and running states, refreshes every five seconds, and shows search, URL, and job counts. Wait for **Test search complete**. If it stops, read the reason and select **Retry test search** after fixing the cause. The test uses the configured allowance and can incur provider charges.
7. Select **Open review queue**. Review the jobs together and record pursue or reject decisions with reasons. A completed test can find zero jobs; that means the pipeline finished, not that the search preferences are good. Adjust the acquisition settings if the results miss the owner's intent.

After the owner account exists, remove `JOB_FINDER_BOOTSTRAP_TOKEN` from the `review` service's Railway variables. Keep the session secret and credential encryption key; losing the encryption key makes stored provider credentials unreadable.

## Enable daily discovery

The bounded test completes owner setup, but **daily qualification stays idle until a qualification target is approved and activated**. Do not tell the owner daily searches are ready at the end of the test alone.

1. Open `/configuration/qualification-targets` and confirm the candidate created from the published setup. If needed, select **Create candidate from current setup**.
2. [Prepare canonical evidence](qualification-evidence-operator.md) for input preparation, relevance, enrichment, deduplication, and composition. Review the frozen examples and provider cost with the owner before running them. The test search does not create these five evidence records.
3. [Preview, approve, and activate the first qualification target](qualification-first-activation.md). Confirm that `/configuration/qualification-promotion` shows the candidate as active.
4. Open `/operations/control`. Confirm that the expected schedules are running and that the Dagster API is available. Check the next full discovery run and the review queue after it finishes. Tell the owner where to review jobs and how to give feedback.

Keep the Railway project URL and review URL in the owner's private handoff. The project URL is for the operator; the review URL is for the owner. The owner password cannot currently be rotated in the app, and there is no separate operator account, so decide who holds that password during the setup call.

For a local or self-hosted installation, use [Run Job Finder for yourself](../README.md#run-job-finder-for-yourself). Its Compose stack has the same browser setup steps, but you generate the secrets in `.env` and open `http://localhost:8080`.
