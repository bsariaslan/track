#!/usr/bin/env python3
"""Hourly package tracking check for 17Track and Cainiao.

Usage:
  python hourly_tracking_check.py --tracking-number 00340435069707912169

This script:
- checks Cainiao (global.cainiao.com)
- checks 17Track (t.17track.net) on a best-effort basis
- stores last result in state/last_result.json
- appends run reports to reports/history.log
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


@dataclass
class TrackingResult:
    source: str
    tracking_number: str
    checked_at_utc: str
    status: Optional[str] = None
    location: Optional[str] = None
    estimated_delivery: Optional[str] = None
    last_update: Optional[str] = None
    raw_excerpt: Optional[str] = None
    error: Optional[str] = None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_cainiao_text(text: str, tracking_number: str, checked_at: str) -> TrackingResult:
    compact = normalize_text(text)
    status = None
    location = None
    est = None
    last_update = None

    m = re.search(r"(Delivered|Delivering \([^\)]*\)|In transit|Out for delivery|At customs|Accepted by carrier)", compact, re.I)
    if m:
        status = m.group(1)

    lm = re.search(r"Last updated:?\s*([0-9:\-\sGMT\+]+)", compact, re.I)
    if lm:
        last_update = lm.group(1).strip()

    # Heuristic: event line often starts with [Location]
    loc_m = re.search(r"\[([^\]]+)\]\s+(Departed from sorting center|Processing at sorting center|Arrived.*?|Out for delivery|Delivered)", compact, re.I)
    if loc_m:
        location = loc_m.group(1)

    est_m = re.search(r"Estimated delivery(?: time)?:?\s*([0-9\-\s]+(?:to|-)\s*[0-9\-\s]+)", compact, re.I)
    if est_m:
        est = est_m.group(1).strip()

    return TrackingResult(
        source="Cainiao",
        tracking_number=tracking_number,
        checked_at_utc=checked_at,
        status=status,
        location=location,
        estimated_delivery=est,
        last_update=last_update,
        raw_excerpt=compact[:1200],
    )


def parse_17track_text(text: str, tracking_number: str, checked_at: str) -> TrackingResult:
    compact = normalize_text(text)

    if "TestNumber00017" in compact:
        return TrackingResult(
            source="17Track",
            tracking_number=tracking_number,
            checked_at_utc=checked_at,
            error="17Track returned demo/test data instead of live tracking for this run.",
            raw_excerpt=compact[:1200],
        )

    status = None
    location = None
    est = None
    last_update = None

    m = re.search(r"(Delivered|In transit|Out for delivery|At customs|Undelivered|Info received)", compact, re.I)
    if m:
        status = m.group(1)

    blocked_m = re.search(r"(verify you are human|unusual traffic|access denied|temporarily unavailable)", compact, re.I)
    if blocked_m:
        return TrackingResult(
            source="17Track",
            tracking_number=tracking_number,
            checked_at_utc=checked_at,
            error=f"17Track page appears blocked/challenged: {blocked_m.group(1)}",
            raw_excerpt=compact[:1200],
        )

    est_m = re.search(r"Estimated delivery(?: time)?:?\s*([0-9\-\s]+(?:to|-)\s*[0-9\-\s]+)", compact, re.I)
    if est_m:
        est = est_m.group(1).strip()

    return TrackingResult(
        source="17Track",
        tracking_number=tracking_number,
        checked_at_utc=checked_at,
        status=status,
        location=location,
        estimated_delivery=est,
        last_update=last_update,
        raw_excerpt=compact[:1200],
    )


def check_cainiao(page, tracking_number: str, checked_at: str) -> TrackingResult:
    try:
        page.goto("https://global.cainiao.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        for name in ["ACCEPT ALL", "Accept all"]:
            try:
                page.get_by_role("button", name=name).first.click(timeout=1000)
                break
            except Exception:
                pass

        box = page.locator("textarea, input[type='text']").first
        box.wait_for(state="visible", timeout=10000)
        box.fill("")
        box.fill(tracking_number)

        track_btn = page.get_by_role("button", name=re.compile("Track", re.I)).first
        try:
            track_btn.click(timeout=5000)
        except Exception:
            # Cainiao overlay layers can intercept pointer events intermittently.
            track_btn.click(timeout=5000, force=True)

        try:
            box.press("Enter", timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(12000)

        return parse_cainiao_text(page.locator("body").inner_text(), tracking_number, checked_at)
    except Exception as exc:
        return TrackingResult(
            source="Cainiao",
            tracking_number=tracking_number,
            checked_at_utc=checked_at,
            error=f"Cainiao check failed: {type(exc).__name__}: {exc}",
        )


def check_17track(page, tracking_number: str, checked_at: str) -> TrackingResult:
    try:
        page.goto(f"https://t.17track.net/en#nums={tracking_number}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)
        result = parse_17track_text(page.locator("body").inner_text(), tracking_number, checked_at)
        if not result.error and not any([result.status, result.location, result.estimated_delivery, result.last_update]):
            result.error = "17Track returned no parseable tracking details for this run."
        return result
    except PlaywrightTimeoutError as exc:
        return TrackingResult(
            source="17Track",
            tracking_number=tracking_number,
            checked_at_utc=checked_at,
            error=f"17Track check timed out: {exc}",
        )
    except Exception as exc:
        return TrackingResult(
            source="17Track",
            tracking_number=tracking_number,
            checked_at_utc=checked_at,
            error=f"17Track check failed: {type(exc).__name__}: {exc}",
        )


def compare_with_previous(previous: dict, current: dict) -> list[str]:
    def short(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        one_line = normalize_text(str(value))
        if len(one_line) > 180:
            return one_line[:177] + "..."
        return one_line

    changes: list[str] = []
    for source in ["Cainiao", "17Track"]:
        p = previous.get(source, {})
        c = current.get(source, {})
        for field in ["status", "location", "estimated_delivery", "last_update", "error"]:
            if p.get(field) != c.get(field):
                changes.append(f"{source} {field} changed: {short(p.get(field))} -> {short(c.get(field))}")
    return changes


def build_report(cainiao: TrackingResult, track17: TrackingResult, changes: list[str]) -> str:
    lines = [
        f"Check timestamp (UTC): {cainiao.checked_at_utc}",
        "",
        "=== Cainiao ===",
        f"Status: {cainiao.status or 'N/A'}",
        f"Location: {cainiao.location or 'N/A'}",
        f"Estimated delivery: {cainiao.estimated_delivery or 'N/A'}",
        f"Last update: {cainiao.last_update or 'N/A'}",
        f"Error: {cainiao.error or 'None'}",
        "",
        "=== 17Track ===",
        f"Status: {track17.status or 'N/A'}",
        f"Location: {track17.location or 'N/A'}",
        f"Estimated delivery: {track17.estimated_delivery or 'N/A'}",
        f"Last update: {track17.last_update or 'N/A'}",
        f"Error: {track17.error or 'None'}",
        "",
        "=== Comparison ===",
    ]

    if cainiao.status != track17.status or cainiao.location != track17.location or cainiao.estimated_delivery != track17.estimated_delivery:
        lines.append("Discrepancy detected between sources.")
    else:
        lines.append("No major discrepancy detected for status/location/ETA.")

    if changes:
        lines.append("Changes from previous run:")
        lines.extend(f"- {c}" for c in changes)
    else:
        lines.append("No changes from previous run.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hourly tracking checker for Cainiao + 17Track")
    parser.add_argument("--tracking-number", required=True)
    parser.add_argument("--state-file", default="state/last_result.json")
    parser.add_argument("--history-log", default="reports/history.log")
    args = parser.parse_args()

    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        page1 = browser.new_page()
        cainiao = check_cainiao(page1, args.tracking_number, checked_at)

        page2 = browser.new_page()
        track17 = check_17track(page2, args.tracking_number, checked_at)
        browser.close()

    current = {"Cainiao": asdict(cainiao), "17Track": asdict(track17)}

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))

    changes = compare_with_previous(previous, current)
    state_path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")

    report = build_report(cainiao, track17, changes)

    log_path = Path(args.history_log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(report)
        f.write("\n" + "=" * 80 + "\n")

    print(report)


if __name__ == "__main__":
    main()
