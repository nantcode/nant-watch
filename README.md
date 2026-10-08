# BreakdownBot (Project 4)

Read-only market recap bot for my Background Workforce. Posts to its own Discord channel:

- **Daily breakdown** (weekdays after the close): SPY/QQQ summary + top movers
- **Weekly breakdown** (Saturday morning): top 10 weekly gainers/losers across US stocks + race scoreboard (NantBot vs Sparticus vs SPY since Oct 5, 2026)

It **never places orders**.

## Run tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Milestones

- [x] M1 Foundation: formatting + Discord poster + tests (v0.1.0)
- [ ] M2 Market math (pure)
- [ ] M3 Read-only Alpaca client
- [ ] M4 Market scanner (local run)
- [ ] M5 Daily breakdown on AWS
- [ ] M6 Race scoreboard
- [ ] M7 Weekly breakdown on AWS
- [ ] M8 Hardening + portfolio write-up (v1.0.0)
