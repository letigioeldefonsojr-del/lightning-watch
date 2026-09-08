# Lightning Watch — unattended PAGASA lightning logger

Runs your `radar_to_tiff.py` lightning watcher (`--lightning --watch --split 60`)
on a schedule inside **GitHub Actions**, so it keeps collecting strikes into a
fresh CSV every hour even when your PC is off. No server, no Google Drive
account, no JavaScript running your Python — GitHub's own scheduler does
that part; the HTML page in `docs/` just *displays* what's already been
collected. See "How this actually works" below if any of that is unclear.

## What's in this folder

```
radar_to_tiff.py                    ← your script, unchanged
requirements.txt                    ← what pip needs to install
scripts/watch_and_commit.py         ← runs the watcher, checkpoints it into git
.github/workflows/lightning-watch.yml  ← the schedule that runs it all
docs/index.html                     ← the viewer page (map + table)
data/lightning/                     ← where the CSVs (and an index.json) land
```

## Setup (one time)

### 1. Create the GitHub repository — and make it **public**

This matters more than it sounds: GitHub Actions minutes are free and
unlimited on public repositories, but a **private** repo only gets 2,000
free minutes a month. This workflow runs roughly 23 hours a day (four
~5h50m runs), which is about **700 hours/month** — way past the private
free tier, and it would start costing real money. If the data isn't
sensitive (public weather/lightning data generally isn't), keep the repo
public.

Go to [github.com/new](https://github.com/new), name it something like
`lightning-watch`, and create it **without** a README/`.gitignore` (you
already have these files).

### 2. Push these files to it

From this folder, in a terminal (PowerShell or Git Bash on Windows):

```
git init
git add .
git commit -m "Set up unattended lightning watcher"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

### 3. Confirm the workflow runs

Go to the repo's **Actions** tab on GitHub. You should see "Lightning watch
(PAGASA)" listed. It's on a schedule (every 6 hours, see below), but to
test it right away without waiting:

1. Click the workflow, click **Run workflow**.
2. Set **max_minutes** to something small, like `5`, for a quick smoke test.
3. Click **Run workflow** again to confirm.
4. Watch it run — after a couple of minutes you should see commits
   appearing with new files under `data/lightning/`.

Once you've confirmed it works, real scheduled runs use the default 350
minutes (5h50m) automatically — you don't need to touch `max_minutes` for
those.

### 4. Turn on the dashboard (GitHub Pages)

Repo **Settings → Pages** → under "Build and deployment", set **Source** to
"Deploy from a branch", branch `main`, folder `/docs`, then **Save**. GitHub
will give you a URL like `https://<your-username>.github.io/<your-repo>/` —
that's your live dashboard. It can take a minute or two to go live the
first time.

The page auto-detects which repo it's serving from from its own URL, so it
should just work. If you ever host it somewhere else (a custom domain) and
it can't guess correctly, click the ⚙ gear icon on the page to set the
GitHub username/repo/branch by hand.

## How this actually works

- **"Runs even when my PC is off"** — GitHub's own servers run the
  workflow, on their schedule, regardless of whether your computer is on.
  That's what "runs through GitHub" (GitHub *Actions*, specifically) means.

- **"Saved every 60 minutes split as CSV"** — this is exactly your script's
  existing `--watch --split 60` behavior: it writes to one CSV for 60
  minutes, then starts a fresh one, forever. `scripts/watch_and_commit.py`
  just runs that command for you and, every 10 minutes, commits whatever's
  new back into the repo (see "Gaps in coverage" below for the one caveat).

- **"Connect it through HTML, CSS, JS. Connect Python in JS"** — JavaScript
  in a browser can't run your Python script; there's no server behind
  `docs/index.html` at all. What it *can* do — and what it does here — is
  read the CSV files and `index.json` your Python script already committed
  to the repo (fetched straight from
  `raw.githubusercontent.com/<you>/<repo>/main/data/lightning/...`) and
  render them as a map and table in the browser. That's the real
  relationship between the three: Python collects and saves the data on a
  schedule; HTML/CSS/JS is just the window you look at it through afterward.

- **Google Drive** — you didn't ask for this in the end (data goes straight
  into the GitHub repo instead, which needed zero extra account setup), but
  if you want a copy pushed to Drive too later, that's a separate,
  addable step (a Google Cloud service account + the Drive API) — just ask
  and I'll add it to the workflow.

## Why a single job can't just run forever

GitHub caps every individual job at **6 hours**. `watch_and_commit.py`
stops the watcher cleanly (same as pressing Ctrl+C) after 350 minutes
(5h50m), commits one last time, and exits. The schedule
(`.github/workflows/lightning-watch.yml`) then starts a brand new run every
6 hours, which picks up right where the last one left off.

### Which hours get a clean, unsplit file

Every hour gets its own CSV (that's `--split 60`) — but the hour a restart
happens to land in ends up split across two files with a short gap in
between, instead of being one clean file. The four restarts are scheduled
for **12AM, 6AM, 12PM, and 6PM Philippines time**, specifically so that
hour is one you're unlikely to care much about:

- 12AM–5:50AM run → 6AM run picks up: the **5–6AM** hour is the split one.
- 6AM–11:50AM run → 12PM run picks up: the **11AM–12PM** hour is the split one.
- 12PM–5:50PM run → 6PM run picks up: the **5–6PM** hour is the split one.
- 6PM–11:50PM run → 12AM run picks up: the **11PM–12AM** hour is the split one.

Every other hour — including 7–8AM, and typical afternoon storm hours like
2–5PM — sits safely in the middle of a run and gets one clean file. If a
*different* set of four hours matters more to you than 5-6AM/11AM-12PM/5-
6PM/11PM-12AM, tell me which ones you'd rather have split instead and I'll
re-time the schedule (`cron` line in `.github/workflows/lightning-watch.yml`)
around that.

### Two smaller gaps

1. **The ~10-minute handoff itself.** Each run stops 10 minutes before the
   next one starts (5h50m watch + a 6h cadence), so there's a short window
   at each restart where nothing is being collected. PAGASA's own feed only
   ever shows a short recent rolling window (no way to ask it for history),
   so this is a real, if small, loss — not just a display glitch.
2. **Scheduled-run delays.** GitHub says scheduled workflows "may be
   delayed during periods of high load" — usually seconds to a couple of
   minutes, occasionally more. Nothing to configure around; just don't
   expect second-perfect timing.

If you later want to shrink the 10-minute gap itself, the fix is a shorter
cadence (e.g. every 5h30m instead of 6h) at the cost of a bit more overlap
risk — ask if you want that tuned.

## Changing things later

- **Richer lightning data (panahon.gov.ph source):** the workflow uses
  `--lightning-source pagasa` (simple, reliable, no extra dependencies —
  good for unattended runs). `radar_to_tiff.py` also supports
  `--lightning-source panahon` for richer per-strike fields (cloud-to-ground
  vs. cloud-to-cloud, peak current, sensor count), but it needs
  `python-socketio` **and Playwright + a headless Chromium download** in
  the workflow, and depends on a connection-ticket mechanism that's more
  fragile to run unattended. Switchable by editing the `--lightning-source`
  flag in `.github/workflows/lightning-watch.yml`, plus adding those
  packages to `requirements.txt` and a Playwright browser-install step —
  ask if you'd like this wired up.
- **Also watching radar frames:** doable as a second, similar job (radar
  frames save as GeoTIFF images, not CSV, so they'd need their own storage
  section on the dashboard) — ask and I'll add it.
- **Repo growing over time:** CSVs are small (lightning-strike rows are
  tiny), so this should stay lightweight for a long time. If it ever grows
  large, an easy fix later is archiving old months into a separate branch
  or a zip, which I can help set up when you get there.
