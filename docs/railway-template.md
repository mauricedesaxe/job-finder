# Deploy and Host Job Finder on Railway

Job Finder searches job boards, scores listings against your preferences, and
puts the strongest matches in a private review queue. This template creates a
review app, a PostgreSQL database, a Dagster webserver, and a Dagster daemon in
one Railway project. Railway generates the initial secrets when you deploy it.

## About Hosting Job Finder

The review app is the only public service. PostgreSQL and both Dagster services
communicate over Railway's private network. PostgreSQL stores search results,
feedback, Dagster state, and encrypted provider credentials. A persistent
volume holds its data. You pay Railway for the services and any external API
usage you choose to enable. Keep the project private and use a separate project
for each person whose data should be isolated.

## Common Use Cases

- Run a personal job search with a private review queue.
- Give someone their own instance and help them through first-time setup.
- Test the search pipeline before operating a longer-running instance.

## Dependencies for Job Finder Hosting

### Deployment Dependencies

- A Railway account with a workspace that can deploy four services.
- A Jina API key and an OpenRouter API key for searches and evaluations. Enter
  these in the app during setup, rather than in Railway variables.

## First-time setup

1. Deploy the template and wait until all four services are healthy.
2. Open the public domain on the `review` service. It redirects to `/setup`.
3. In Railway, open the `review` service's Variables tab and reveal or copy the
   generated `JOB_FINDER_BOOTSTRAP_TOKEN`. Keep it private. Use it to create the
   first owner account on `/setup`.
4. In the app, add your Jina and OpenRouter keys, choose job preferences, set a
   spend budget, and run the bounded test search.
5. After the owner account exists, remove `JOB_FINDER_BOOTSTRAP_TOKEN` from the
   `review` service. Check `/operations/control` for the background schedules.

Only the review service needs a public domain. Leave PostgreSQL and Dagster
private. If the review service has no domain after deployment, generate one in
its Railway Networking settings. The review health check is `/readyz`.

## Why Deploy Job Finder on Railway?

The template provisions the four connected services and their shared variables
in one step. You can inspect each service's logs and deployments in Railway,
while the owner completes their search setup in the review app.
