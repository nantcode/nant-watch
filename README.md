# NantWatch (Project 4)

Read-only market recap bot for my Background Workforce. Posts to its own Discord channel:

- **Daily breakdown** (weekdays, 5:15 PM ET): SPY/QQQ summary + top 10 gainers/losers across ~12,000 US stocks
- **Weekly breakdown** (Saturday morning): top 10 weekly gainers/losers + race scoreboard (NantBot vs Sparticus vs SPY since Oct 5, 2026)

It **never places orders**: the Alpaca client can only send GET requests to an allowlist of read-only paths.

## How it runs

EventBridge Scheduler (America/New_York) -> Lambda (python3.12, arm64) -> Alpaca (read-only) -> Discord webhook.
Secrets live in SSM Parameter Store (SecureString); the Lambda can read only the exact parameters it needs.

## Run tests

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## Deploy

    sam build && sam deploy

## Milestones

- [x] M1 Foundation: formatting + Discord poster + tests (v0.1.0)
- [x] M2 Market math (pure) (v0.2.0)
- [x] M3 Read-only Alpaca client (v0.3.0)
- [x] M4 Market scanner (local run) (v0.4.0)
- [x] M5 Daily breakdown on AWS (v0.5.0)
- [x] M6 Race scoreboard (v0.6.0)
- [x] M7 Weekly breakdown on AWS (v0.7.0)
- [ ] M8 Hardening + portfolio write-up (v1.0.0)
