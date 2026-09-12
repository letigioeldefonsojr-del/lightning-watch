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
(PAGASA)" listed. To test it right away:

1. Click the workflow, click **Run workflow**.
2. Set **max_minutes** to something small, like `5`, for a quick smoke test.
3. Click **Run workflow** again inside the dropdown to actually confirm it —
   don't navigate away until a new run appears at the top of the list with
   a spinner/yellow icon, or it may not have actually started.
4. Watch it run — after a couple of minutes you should see commits
   appearing with new files under `data/lightning/`.

Once you've confirmed it works, real runs (started by the external
scheduler set up in step 5 below) use the default 178 minutes (2h58m)
automatically — you don't need to touch `max_minutes` for those.

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

### 5. Set up the external scheduler (cron-job.org)

This is what actually makes it run unattended, every 3 hours, forever.
GitHub Actions does have its own built-in `schedule:` trigger, but in
practice it turned out to be unreliable for this workflow — it silently
skipped multiple scheduled runs in a row with no error anywhere. Rather
than depend on it, this workflow only listens for `workflow_dispatch`
("start me now") calls, and a free outside service calls that on a
schedule instead — a plain API request, not GitHub's own best-effort cron.

1. **Get a GitHub token**, so the outside service is allowed to start your
   workflow: github.com → your profile picture (top-right) → **Settings**
   → **Developer settings** (bottom of the left sidebar) → **Personal
   access tokens** → **Fine-grained tokens** → **Generate new token**.
   - Name it anything (e.g. `lightning-watch-scheduler`).
   - **Repository access**: "Only select repositories" → pick this repo.
   - **Permissions → Repository permissions** → set **Actions** to
     **Read and write**.
   - **Expiration**: pick the longest option available (or a custom date
     far out) — this token needs to keep working unattended, and it can
     only touch this one repo's Actions, so a long lifetime is low-risk.
   - Generate it and copy the value shown (starts with `github_pat_...`)
     immediately — it's shown once, never again.

2. **Create a free account** at [cron-job.org](https://cron-job.org).

3. **Create a cronjob** with these exact settings:
   - **URL**: `https://api.github.com/repos/<your-username>/<your-repo>/actions/workflows/lightning-watch.yml/dispatches`
   - **Schedule**: every 3 hours, at a few minutes past the hour (e.g.
     `:10`) rather than exactly on the hour — cron-job.org lets you pick a
     timezone per job; set it to `Asia/Manila` and schedule `2:10, 5:10,
     8:10, 11:10` AM and PM.
   - **Request method**: `POST`
   - **Headers**: add these three —
     - `Authorization: Bearer <your token from step 1>`
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - **Request body** (raw JSON):
     ```json
     {"ref": "main", "inputs": {"max_minutes": "178"}}
     ```
     (If you already have this cronjob set up from before with `"170"` in
     the body, edit it on cron-job.org and change that to `"178"` — this
     repo change alone doesn't update anything you already saved there.)
   - Save it. A successful call gets back an empty response with status
     `204` — that's correct, not an error; you can confirm it worked by
     checking the repo's Actions tab for a new run right after the
     scheduled time.

## How this actually works

- **"Runs even when my PC is off"** — GitHub's own servers run the
  workflow, regardless of whether your computer is on. That's what "runs
  through GitHub" (GitHub *Actions*, specifically) means. It's started
  every 3 hours by cron-job.org's free scheduler (step 5 above) calling
  GitHub's API — not by GitHub's own `schedule:` trigger, which turned out
  to be unreliable for this workflow (see "Why an external scheduler"
  below) — but the actual watching/collecting still all happens on
  GitHub's servers either way.

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
stops the watcher cleanly (same as pressing Ctrl+C) after 178 minutes
(2h58m), commits one last time, and exits. The external scheduler
(cron-job.org, step 5 above) then starts a brand new run every 3 hours,
which picks up right where the last one left off.

### Why an external scheduler instead of GitHub's own `schedule:`

Earlier this was set up using GitHub Actions' built-in `schedule:` (cron)
trigger, which is the normal way to do this. In practice, on 2026-09-09,
it silently missed several scheduled runs in a row — no error, no failed
run shown anywhere, it just never started. GitHub's own docs do warn that
scheduled workflows "may be delayed during periods of high load," but this
was worse than a short delay. Rather than keep debugging GitHub's internal
scheduler, this workflow now only responds to `workflow_dispatch` ("start
me now" calls), and a free outside service (cron-job.org) makes that call
every 3 hours instead — a plain, reliably-delivered API request rather
than best-effort internal cron.

### Which hours get a clean, unsplit file

Every hour gets its own CSV (that's `--split 60`) — but the hour a restart
happens to land in ends up split across two files with a short gap in
between, instead of being one clean file. The eight restarts are scheduled
for **2:10, 5:10, 8:10, and 11:10 AM & PM Philippines time** (the `:10`
rather than on-the-hour is deliberate — see above), so the hours split are
1-2AM, 4-5AM, 7-8AM, 10-11AM, and their PM equivalents. Every other hour —
including typical afternoon storm hours like 2-4PM — sits safely in the
middle of a run and gets one clean file. Tell me if a different set of
hours matters more to you and I'll re-time the cron-job.org schedule
around that.

### Two smaller gaps

1. **The ~2-minute handoff itself.** Each run stops 2 minutes before the
   next one starts (2h58m watch + a 3h cadence), so there's a short window
   at each restart where nothing is being collected. PAGASA's own feed only
   ever shows a short recent rolling window (no way to ask it for history),
   so this is a real, if small, loss — not just a display glitch. (This was
   a ~10-minute gap originally, narrowed to 2 minutes once the checkpoint
   logic got more resilient to a still-running watcher overlapping with its
   own commit -- see watch_and_commit.py's git_commit_and_push().)
2. **Ordinary API/network delays.** An outside service calling GitHub's API
   can itself be a little late (seconds, occasionally a minute or two) —
   nothing to configure around, just don't expect second-perfect timing.

## Currently using panahon.gov.ph (richer data, more fragile)

This is set to `--lightning-source panahon` — PAGASA's richer real-time feed
(cloud-to-ground vs. cloud-to-cloud, peak current in kA, strike height,
sensor count), instead of the simpler `pagasa` REST poll it started on.

Worth knowing about this source specifically:

- It needs a real headless browser (Playwright + Chromium, installed by the
  workflow's **"Install Playwright's Chromium"** step) just to fetch a
  short-lived connection ticket before every websocket connect/reconnect —
  see `_fetch_panahon_ws_ticket()` in `radar_to_tiff.py` for why plain HTTP
  requests don't work here. That's more moving parts than `pagasa`'s plain
  polling, and one more thing that can break if panahon.gov.ph changes how
  that ticket works again (it's already changed once before).
- Because of that, `scripts/watch_and_commit.py` now specifically watches
  for the watcher process dying unexpectedly and, if it does, prints a
  `::error::` line (shows up as a red X on the run, plus an annotation —
  the same kind of banner you saw on your pagasa test) instead of quietly
  finishing green with no data. If you ever see a failed run, that's almost
  certainly this — open the **"Watch panahon.gov.ph lightning feed"** step's
  log and look for a line starting with `Couldn't connect to panahon.gov.ph`.
- If it turns out to be too flaky in practice, switching back to `pagasa` is
  a two-line change: `--lightning-source panahon` → `pagasa` in
  `.github/workflows/lightning-watch.yml`, and you can drop the "Install
  Playwright's Chromium" step above it (harmless to leave in either way).
- **Known fix already applied:** `ws.panahon.gov.ph` (the actual strike feed
  server, separate from the `panahon.gov.ph` site itself) fails Python's TLS
  certificate verification with "unable to get local issuer certificate" —
  the server doesn't send its full certificate chain, which a real browser
  tolerates but Python's `ssl` module doesn't. This is the same class of
  issue `radar_to_tiff.py` already documented for `api.meteopilipinas.gov.ph`
  in `download_png()`. The workflow now passes `--insecure` (which
  `scripts/watch_and_commit.py` forwards to `radar_to_tiff.py`, which now
  actually wires it into the panahon websocket connection — it previously
  only affected radar image downloads) to work around it. This only skips
  certificate verification for PAGASA's own public, non-sensitive endpoints,
  same reasoning as everywhere else `--insecure` is used in this project.
- **Also watching radar frames:** doable as a second, similar job (radar
  frames save as GeoTIFF images, not CSV, so they'd need their own storage
  section on the dashboard) — ask and I'll add it.
- **Repo growing over time:** CSVs are small (lightning-strike rows are
  tiny), so this should stay lightweight for a long time. If it ever grows
  large, an easy fix later is archiving old months into a separate branch
  or a zip, which I can help set up when you get there.
