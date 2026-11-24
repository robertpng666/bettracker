# POTD Tracker

Local web app that watches the potd.lol handicappers in the **top 25 by Rating** and the
**top 25 by ROI (last 30 days)** and lists the bets to mirror: units, stake, target odds and
the lowest odds still worth taking.

## Start

Double-click **POTD Tracker.command** (or run `python3 app.py`). The page opens at
http://127.0.0.1:8765. Keep the Terminal window open while you want tracking; close it or
press **Quit** on the page to stop.

## How it works

- Data is read over plain HTTPS with your own potd.lol session cookie: no background browser.
  Each user signs in with their own potd.lol account (saved in `data/session.json`).
- Every few minutes (Settings → "Check every") it reloads both leaderboards. A
  handicapper's profile is opened only when their last-pick time changed, or to pick up the
  result of a bet that has started.
- New picks trigger a macOS notification and a **NEW** badge.
- Tick a bet once you've placed it: it stays in the list, highlighted green. Unticked bets
  disappear once their game starts; ticked ones stay until 12 hours after kickoff.

## Settings

- **Unit multiplier**: scale every handicapper's units up or down.
- **Odds tolerance %**: "min" odds = target odds − this percentage.
- If several tracked handicappers post the same bet, it is shown once with the largest stake
  and the lowest posted odds as the target.

## Sign-in

On first run (and when the login expires, about every two weeks) a sign-in window opens in
the app. It launches Google Chrome on the potd.lol sign-in page, because the site's human
check only works in a real browser. Sign in; the window closes by itself and tracking starts.

## Files

- `app.py`: tracker loop + local web server
- `potd.py`: Chrome control and potd.lol page readers
- `web/index.html`: the page
- `data/state.json`: picks seen, placed bets, settings
