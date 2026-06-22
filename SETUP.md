# Setup: Your Daily Briefing RSS Feed

## What's in this folder

```
config.json                          <- edit this to add/change topics, no code needed
fetch_and_build_feed.py              <- the script; you shouldn't need to touch this often
.github/workflows/daily-briefing.yml <- the schedule (runs once a day)
docs/feed.xml                        <- the actual RSS feed (auto-generated, don't hand-edit)
docs/items_store.json                <- history of past briefings, used for dedup (auto-generated)
```

## One-time setup

1. **Create a new GitHub repo** (must be Public — private repos need a paid
   plan for Pages). Upload all the files above, preserving the folder
   structure exactly (the workflow file MUST end up at
   `.github/workflows/daily-briefing.yml` — if you create it through the
   GitHub web UI, type the full path with slashes into the filename box;
   GitHub will create the folders for you).

2. **Add your API key as a secret**:
   Repo → Settings → Secrets and variables → Actions → "New repository secret"
   - Name: `ANTHROPIC_API_KEY`
   - Value: your actual key

3. **Edit `config.json`**: replace `YOUR-USERNAME` and `YOUR-REPO-NAME` in
   `feed_link` with your actual GitHub username and repo name.

4. **Turn on GitHub Pages**:
   Repo → Settings → Pages → Source: "Deploy from a branch" → Branch: `main`,
   folder: `/docs` → Save.

5. **Run the workflow once manually** to confirm everything works:
   Repo → Actions tab → "Daily Briefing Feed" → "Run workflow" → Run workflow.
   Check the run logs. If it succeeds, `docs/feed.xml` will be updated with
   real content within a minute or two of the run finishing.

6. **Your feed URL** will be:
   `https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/feed.xml`
   Paste that into Reeder (or whatever reader you're using).

## Adding more topics later

Open `config.json` and add another object to the `topics` array, e.g.:

```json
{
  "id": "sci-fi-books",
  "label": "New Sci-Fi / Fiction Releases",
  "brief": "New book releases and major news in sci-fi/speculative fiction."
}
```

`id` just needs to be a short unique slug — the script uses it internally.
`label` is what shows as the section heading in the feed. `brief` is the
actual instruction the model uses to go find news — be as specific as you
were with the existing topics. No need to touch `fetch_and_build_feed.py`.

You may also want to bump `stories_per_day` in `config.json` if you add
several new topics and want more total stories — right now it's set to 6
across all 4 topics combined.

## How "no repeated stories" works

Every day, the script looks back `lookback_days` (default 3) into
`docs/items_store.json` and tells the model what's already been covered,
instructing it not to repeat the same story unless there's a real update.
This file also doubles as the source of truth for the feed's history (kept
for `feed_history_days`, default 30 days) — don't delete or hand-edit it,
or you'll lose the dedup context and the feed will rebuild from scratch.

## Adjusting the schedule

The cron line in `.github/workflows/daily-briefing.yml` is in **UTC**, not
local time. `"0 11 * * *"` is 11:00 UTC daily ≈ 7am Eastern (during EDT) or
6am Eastern (during EST). Adjust the hour if you want it earlier or later
in your local time — GitHub Actions schedules can run a few minutes late
during peak hours, so don't rely on it for anything truly time-sensitive.

## If something breaks

- Check the **Actions tab** — every run's logs are there, including any
  Python error.
- If the model's output couldn't be parsed as valid JSON, the script will
  fail loudly (non-zero exit) rather than write garbage — so a bad run
  leaves yesterday's feed.xml intact instead of corrupting it.
- Test logic changes locally first with `python3 fetch_and_build_feed.py
  --test` — this skips the live API call and uses canned sample data, so
  you can verify the RSS/HTML building logic without spending API credits.
- If you edit a file directly in the GitHub web UI between scheduled runs,
  the next run's `git push` could conflict — the `git pull --rebase
  --autostash` line in the workflow is there specifically to self-heal
  that.
