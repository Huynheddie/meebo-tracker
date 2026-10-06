# meebo tracker

Pings your phone the moment the 2026 League of Legends World Championship Priceless
experience goes live.

## What it watches

| Page | Trigger |
|---|---|
| Priceless – Riot Games (`/celebrity/22136/riot-games`) | any new `/product/<id>/` listing |
| Priceless – League of Legends E‑Sports (`/celebrity/19286/...`) | any new listing (the 2026 LCS Champs experience, product 237665, was filed here rather than under Riot Games) |
| Priceless – `/lolesports-shop` (where `priceless.com/LoLesports` redirects) | any new listing |
| Chase Cashback Moments – Worlds event page | any change to the event description block |
| Every other League/Riot page on Priceless (14 more as of Oct 2026, found from the sitemap, including ones added later) | any new listing |
| Priceless sitemap (every live listing on the site, rebuilt daily) | any new listing whose URL or page mentions League of Legends / Riot / LCS, or that links to a League/Riot page, wherever it is filed |

| lolesports.com/news | new article mentioning Priceless / Mastercard / Chase Freedom / Fan Fest / playtest / presale |
| Chase media center (media.chase.com/news) | new article about League / Riot / Worlds / esports / Freedom |

All three Priceless pages currently show zero listings, so the first listing that appears is the alert.
If any page fails 3 checks in a row (blocked, layout change), you get a warning push so you
know to check manually instead of trusting silence.

## Setup (5 minutes)

1. **Phone notifications:** install the free **ntfy** app (iOS/Android) and subscribe to a
   long random topic, e.g. `meebo-` + a random string. Topics are public, so don't use
   anything guessable. Turn on "urgent" priority alerts so it breaks through Focus/DND.
2. **Test locally:**
   ```bash
   export NTFY_TOPIC=meebo-yourRandomString
   python tracker.py --test-notify   # should buzz your phone
   python tracker.py --status        # shows current state of all pages
   ```
3. **Run it**, pick one or both:
   - **Local loop** (fastest, every 2 min while your machine is on):
     `INTERVAL=120 python tracker.py`
   - **GitHub Actions** (runs 24/7, every ~20 min): push this folder to a **public** repo (see caveats below),
     add a repo secret `NTFY_TOPIC` (and optionally `DISCORD_WEBHOOK`), then trigger the
     workflow once from the Actions tab to set the baseline. `state.json` is committed back
     only when something changes.

Optional: set `DISCORD_WEBHOOK` to also get a Discord message.

Optional: set `HEALTHCHECK_URL` to a healthchecks.io ping URL. Every run pings it (or pings
`/fail` if the run errored), so healthchecks.io can alert you when runs stop arriving: an expired
cron-job.org token or a GitHub outage otherwise looks exactly like "no drop yet".

Optional: set `PUSHOVER_USER` and `PUSHOVER_TOKEN` to make drop alerts (a new Priceless listing, or
a Chase page change) ring as a Pushover **emergency alarm**: it repeats every 30 seconds for up to
an hour until you tap Acknowledge, and sounds through silent mode if Critical Alerts are allowed
for Pushover in iOS Settings. News and warning alerts stay ntfy-only. Test it from the Actions tab
with the "Ring a test Pushover alarm" option, which stops on its own after 2 minutes.

## Social / news monitoring (free)

X has no free real-time access: the API is paid, Nitter is gone, and free RSS generators
(rss.app, keep.md) only refresh **once a day**. So instead the tracker watches the official
sites that would carry a written announcement: lolesports.com/news and Chase's media center.
Both are free and checked every cycle.

If you still want an X feed as a slow backup, make one at rss.app for `x.com/LoLEsports` and
set `X_RSS_FEEDS` to the feed URL. Expect it to lag by up to a day.

Tune what counts as relevant with the `KEYWORDS` env var (a regex).

## GitHub Actions caveats

- **Use a public repo.** Private repos get 2,000 free Actions minutes/month, and each run bills
  at least 1 minute, so a 5-minute schedule (~8,600 runs/month) blows past that in about a week.
  Public repos have unlimited minutes. Secrets stay hidden either way, and `state.json` contains
  nothing sensitive.
- **Timing is loose.** Scheduled runs are best-effort: often 5-15+ minutes late, occasionally
  skipped when GitHub is busy. For a drop that could sell out fast, the local 2-minute loop is
  the primary; Actions is the backup for when your machine is off.
- **Datacenter IPs can get blocked.** Chase and Priceless might block GitHub's runners even
  though they work from home. You'd get the "check failing" warning push if that happens.

## Notes

- First run records a baseline, so a change to the Chase page is only reported relative to that.
  The Chase "Catch us at 2026 LCS Championship" line will probably be edited soon after the LCS
  event, so expect one early Chase alert that might just be housekeeping. Priceless alerts are the
  high-signal ones.
- Be logged in to priceless.com with your Freedom Flex already linked before the drop so checkout
  is one step.
- Keep the interval at 2 minutes or more to stay polite to both sites.
