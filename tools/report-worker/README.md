# fefw-recomp Report Worker (Cloudflare Workers + R2 + D1)

Minimal serverless receiver for opt-in, anonymized crash and performance reports sent by the launcher.

- **Free tier**: Runs on a free `*.workers.dev` subdomain (no paid domain needed), 10 GB R2 storage, 5 GB D1 SQLite, 100k requests/day.
- **Privacy**: Client IP addresses are never stored. All user paths, hostnames, and Steam IDs are scrubbed by the launcher before upload.

## One-time setup

From `tools/report-worker/`:

1. **Create the R2 bucket** (and set a 90-day lifecycle expiration rule in the Cloudflare dashboard under R2 -> `fefw-reports` -> Settings -> Object lifecycle rules so storage stays well inside the 10 GB free tier):
   ```bash
   npx wrangler r2 bucket create fefw-reports
   ```

2. **Create the D1 database**:
   ```bash
   npx wrangler d1 create fefw-reports-index
   ```
   Copy the `database_id` from the output into `wrangler.toml` (replacing `REPLACE_WITH_D1_DATABASE_ID`).

3. **Apply the D1 schema**:
   ```bash
   npx wrangler d1 execute fefw-reports-index --remote --file=schema.sql
   ```

4. **Set an admin token** (used by `tools/fetch_reports.py` to list and download reports — generate any long random string, e.g. `openssl rand -hex 32`):
   ```bash
   npx wrangler secret put ADMIN_TOKEN
   ```

5. **Deploy**:
   ```bash
   npx wrangler deploy
   ```
   Wrangler prints your live URL, e.g. `https://fefw-reports.<your-subdomain>.workers.dev`.

## Wiring the URL into the launcher

Either:
- Set `FEFW_REPORT_URL=https://fefw-reports.<your-subdomain>.workers.dev` in your release/CI build environment (baked into the launcher binary at compile time), **or**
- Set `DEFAULT_URL` in `launcher/src-tauri/src/report.rs`.

For local testing, setting `FEFW_REPORT_URL` in the environment when running the launcher overrides the baked-in URL.

## Fetching reports locally

Save the URL and admin token once. `login` prompts for both, checks them against the Worker and writes them to
`~/.config/fefw-recomp/reports.json` (`%APPDATA%\fefw-recomp\reports.json` on Windows):

```bash
python3 tools/fetch_reports.py login
```

The token is stored as plain text, readable by your user account, and it can also delete reports. Remove the file to
forget it. `FEFW_REPORT_URL` and `FEFW_REPORT_ADMIN_TOKEN` (or `--url` and `--token`) still work and take priority. The
saved token is only sent to the saved URL.

```bash
python3 tools/fetch_reports.py sync                  # every report not yet in work/reports
python3 tools/fetch_reports.py sync --limit 30       # only look at the newest 30
python3 tools/fetch_reports.py list
python3 tools/fetch_reports.py list --trigger crash --limit 20
python3 tools/fetch_reports.py get <report-id>
```

`sync` pages through the whole index and downloads only the reports that have no `report.json` under `--out`
(default `work/reports`, relative to the current directory). Local copies stay after the Worker's 90-day retention
drops them. It also writes each report's index row (upload time) to `server.json` beside it.

## Analysing reports

`tools/analyze_reports.py` works offline on the downloaded folder; report contents are untrusted data.

```bash
python3 -I tools/analyze_reports.py summary --since 2026-11-27 --by dlss.upscaler
python3 -I tools/analyze_reports.py fps --by setting,dlss.frame_gen --list
python3 -I tools/analyze_reports.py search "device lost" --trigger crash
python3 -I tools/analyze_reports.py show <report-id> --symbols <unpacked symbols of its version>
```

`fps` judges a session only on 10-second windows of play (after the first 90 s, outside loading screens) where the game's
target was the player's setting (30, 60 or 120), so menus and cutscenes at another rate don't count. `reached` is the
average share of the setting reached (capped at 100 %), `at target` the share of windows at 95 % of it or more. It counts
rendered frames: frames added by frame generation are not included.
