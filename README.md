# Hourly AliExpress Tracking Monitor

This repository now includes an automation script that checks the same tracking number on:
- **17Track** (`t.17track.net` best effort)
- **Cainiao** (`global.cainiao.com`)

It captures:
- current shipping status
- location (if available)
- estimated delivery date (if available)
- differences between both sources
- changes from the previous run
- UTC timestamp for each check

## Files
- `hourly_tracking_check.py`: main checker script.
- `state/last_result.json`: last check snapshot (created at runtime).
- `reports/history.log`: appended historical log (created at runtime).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install playwright
python -m playwright install firefox
```

## Run once

```bash
python hourly_tracking_check.py --tracking-number 00340435069707912169
```

## Run hourly with cron

Edit crontab:

```bash
crontab -e
```

Add this line (update paths if needed):

```cron
0 * * * * cd /workspace/track && /workspace/track/.venv/bin/python /workspace/track/hourly_tracking_check.py --tracking-number 00340435069707912169 >> /workspace/track/reports/cron.log 2>&1
```

This runs at the top of every hour.

## Notes
- 17Track may sometimes return a generic/demo page or block automation; the script records that as an `error` field instead of crashing.
- Cainiao is usually the primary reliable source for AliExpress shipments.
