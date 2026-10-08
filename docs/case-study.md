# Case study: NantWatch, a read-only serverless market-intel bot

**Role:** sole designer and builder · **Stack:** AWS Lambda, EventBridge Scheduler, SSM Parameter Store, CloudWatch, SNS, SAM, Python 3.12 · **Repo:** nantcode/nant-watch

## 1. The problem

I run two paper-trading bots (NantBot on AWS, Sparticus on my Mac) and wanted one place that tells me, without me checking charts:

- what the whole US market did today and this week (biggest movers, not just SPY), and
- whether my bots are actually beating the market since the race started on Oct 5, 2026.

**Constraints:** almost zero cost, nothing I have to babysit, and it must be *impossible* for this bot to trade, because it reuses my trading account's API keys.

## 2. Architecture

Two scheduled Lambdas in one SAM stack (see the diagram in the README):

1. **EventBridge Scheduler** fires in New York time (weekdays 5:15 PM, Saturdays 9:00 AM).
2. The **Lambda** loads its secrets from **SSM** in one call, then reads Alpaca's market calendar, asset list (~14,400 assets, ~12,300 eligible) and daily SIP bars in batches of 200 symbols.
3. Pure functions filter (price >= $5, average volume >= 500K shares), rank, and format the report under Discord's 2,000-character limit.
4. The report is posted to a **Discord webhook**. Failures post a warning to Discord *and* trip a **CloudWatch alarm** that emails me through **SNS**.

## 3. Key decisions and trade-offs

| Decision | Alternatives I considered | Why I chose it |
|---|---|---|
| Lambda + EventBridge Scheduler | EC2 cron, ECS task | Runs ~23 times a month; pay-per-use, no OS to patch |
| Scheduler timezone `America/New_York` | UTC cron | Daylight saving is handled by AWS, not by me twice a year |
| SSM SecureString | Secrets Manager | Free for standard parameters; rotation isn't needed for paper keys |
| Standard library only | `requests`, `alpaca-py` | No dependencies to patch, tiny package, builds anywhere |
| Read-only enforced in code | Trusting myself not to call `/orders` | Alpaca keys can trade, so the client hard-codes GET plus a path allowlist, and tests prove `POST` and `/orders` are blocked |
| Batch 200 symbols per request, 180 requests/min limiter | One request per symbol | ~62 requests instead of ~12,300; stays under the free plan's 200/min |
| Omit `end` on bar requests | Request "now" | The free plan forbids SIP data newer than 15 minutes; letting Alpaca pick the end time avoids that error |
| `adjustment=split` | Raw prices | A 10:1 split would otherwise look like a -90% "top loser" |
| No Lambda async retries | Default 2 retries | A failed run alerts once instead of retrying and possibly double-posting; the client already retries API calls with backoff |

## 4. Security

- **Least privilege per function:** the daily Lambda can call `ssm:GetParameters` on exactly 3 parameter ARNs; the weekly one on exactly 5. There are no wildcards.
- **Secrets never touch git, logs or Discord:** they're fetched at runtime, the secret key is masked out of error messages, and the alert email address is a CloudFormation dynamic reference to SSM, so it isn't in the repo either.
- **Defense in depth on "never trade":** a GET-only transport, a path allowlist, and a block on any path containing "order" or "position".
- **Discord safety:** `allowed_mentions: {parse: []}` means a post can never ping `@everyone`.

## 5. Reliability and operations

- **Holidays:** the market calendar drives everything; a scheduled run on a market holiday quietly skips.
- **Partial failures:** if one batch of symbols fails, it's counted and skipped; if *every* batch fails, the run fails loudly.
- **Alerting has two layers:** a Discord warning for job errors, and a CloudWatch alarm with email for anything, including failures before secrets load.
- **Dry-run mode** on both functions for safe testing in production.
- **Logs** are kept 14 days. Each run logs one summary line: symbols scanned, how many passed the filters, failed batches, requests, seconds.

## 6. Testing

- **137 unit tests** run in about a second, with no AWS, no network and no secrets. They use dependency injection: fake clients, a fake clock, fake SSM, fake Discord.
- **GitHub Actions** runs the suite on Python 3.9 (my Mac) and 3.12 (Lambda) on every push.
- Hand-written New York time conversion, verified against Python's `zoneinfo` for every hour from 2024 to 2030.

## 7. Cost

All usage sits inside the AWS free tier: about 23 Lambda runs a month, 2 alarms, standard SSM parameters, and a handful of SNS emails. Alpaca's free market-data plan covers the data.

## 8. What I'd do next

- Store each run's results in **DynamoDB** to chart the race over time.
- Add a CloudWatch **dashboard**: run duration, symbols kept, failed batches.
- If the universe grew 10x, fan out batches with **Step Functions Map** instead of a single Lambda loop.
- Move the infrastructure to **Terraform or CDK** to compare with SAM.

## 9. Interview talking points

- **"Tell me about a security decision."** I reused trading keys, so I enforced read-only in three layers of code and proved it with tests, because IAM can't restrict a third-party API.
- **"How do you handle failure?"** Partial batch failures degrade gracefully; total failures fail loudly. Two alert paths (Discord and SNS email) cover errors both before and after secrets load.
- **"How do you control cost?"** Serverless on a schedule, standard library only, batched requests, 14-day log retention: about $0 a month.
