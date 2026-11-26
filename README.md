# Telegram Mirror Bot

A Telegram bot that takes a link or a file, downloads it if needed, and re-uploads it to **Gofile or Pixeldrain**.

It handles direct file links, web pages where the file is hidden behind a "Download" button, video sites supported by yt-dlp, image galleries supported by gallery-dl, public MEGA links, magnet links, `.torrent` files, and Telegram attachments you reply to with `/gf` or `/px`.

```
Telegram link / replied file
          │
          ▼
   Resolver (HTML buttons, meta refresh, data-url, JSON-LD, Google Drive, GitHub releases…)
          │
          ▼
   aria2c (HTTP / torrent), yt-dlp (video), gallery-dl (galleries), megadl (MEGA)
          │
          ▼
   Gofile or Pixeldrain upload
          │
          ▼
   Public link sent back in Telegram
```

## Features

- Direct HTTP/HTTPS downloads with aria2c multi-connection downloading.
- "Download button" pages: scans links, buttons, `data-*` URLs, meta refresh, embedded video/audio, JSON-LD and common JavaScript redirects.
- Special handling for Google Drive single files, Dropbox, Pixeldrain links, Gofile share links, MediaFire-style buttons, and GitHub releases.
- Video sites through yt-dlp (YouTube, Vimeo, TikTok, X/Twitter, Reddit, etc.).
- Image galleries through gallery-dl (Pixiv, Danbooru, Gelbooru, Imgur albums, DeviantArt, Flickr, ArtStation, Tumblr, Reddit image posts, and many more).
- Public MEGA file and folder links through megatools (`megadl`).
- Android partition dumps: `/gf <link> -dump boot,vendor` downloads an OTA zip, fastboot ROM, or `.7z`/`.rar` archive, extracts only the requested images from `payload.bin` or `super.img`, and uploads each image separately.
- Maximum compression: `/compress-gf <link>` and `/compress-px <link>` repack the download (or the requested `-dump` partitions) into one 7-Zip archive with LZMA2, report its SHA-256, and upload the archive.
- Magnet links and `.torrent` files through aria2c (DHT enabled, seeding disabled).
- Telegram attachments — documents, photos, video, audio, voice notes, animations and stickers — are mirrored only when you reply to them with `/gf` or `/px`. Unsolicited media is ignored. Telegram's cloud Bot API limits bot downloads to 20 MB per attachment.
- Gofile share links (`gofile.io/d/...`) resolve through Gofile's API using a temporary guest token. Folder links are supported; the bot downloads the largest file or every file in `MULTI_FILE_MODE=zip`.
- Gofile guest uploads work with no account. An optional `GOFILE_TOKEN` uses your account. The bot does not reject a file by size on its own; Gofile's server enforces the plan limits.
- Pixeldrain uploads (`/px`, `/compress-px`) use your Pixeldrain API key, with the free plan allowing 10 GB per file and Pro allowing 100 GB per file.
- Pixeldrain file links (`pixeldrain.com/u/...`) are downloaded through Pixeldrain's API. When `PIXELDRAIN_API_KEY` is set, the bot authenticates the download so a Pro/Prepaid account uses its own transfer quota instead of the shared free per-IP quota.
- Progress bars, cancel buttons, per-user job limits, size limits and timeouts.
- **Inline mode:** type `@YourBot /gf <link>` in any chat and the bot posts the progress card and final link right there. Inline mode accepts direct links only.
- Private DMs are restricted to `ALLOWED_USER_IDS`; groups work while at least one of those users is a member. Channels are ignored.
- Runs on a Windows PC, Docker, or a small VPS. Telegram long polling means no open ports or domain are needed.
- **Split mode:** the PC can act as a Telegram-only controller and send every download/upload job to a one-shot GitHub Actions worker. The PC stays at roughly 60-100 MB of RAM; the runner does the heavy work.
- **Cloudflare mode:** a Worker webhook can replace the PC entirely. It handles commands, authorization, job tracking and cancellation; GitHub Actions still does the downloads and uploads. See [Cloudflare Worker controller](#cloudflare-worker-controller-no-pc).
- The PC runner can also hand the Telegram poller to one GitHub Actions run when it exits, and takes the bot back (cancelling that run) when it starts again. See [deploy/windows-pc.md](deploy/windows-pc.md).

## Recommended: PC controller + GitHub Actions workers

The Telegram poller runs on your Windows PC, but the heavy work does not. The
PC receives commands and starts one `job.yml` workflow run per job. The worker
downloads the file, extracts partitions if asked, uploads to Gofile or
Pixeldrain, edits the progress message directly through the Bot API, and posts
the final link. The PC only tracks the run id so `/status` and `/cancel` work.

This keeps the PC responsive and avoids running aria2c, ffmpeg, payload dumper
and the uploads on your own connection. The tradeoffs are a 1-3 minute wait for
a runner to start, a ~14 GB runner disk, a 6-hour workflow cap, datacenter-IP
blocks, public Actions logs in a public repo, and the risk that GitHub may
restrict Actions usage.

Follow [deploy/windows-pc.md](deploy/windows-pc.md). The short version:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\configure-env.ps1
gh auth login
powershell -ExecutionPolicy Bypass -File scripts\configure-remote.ps1
scripts\run-local.cmd
```

`scripts\configure-remote.ps1` generates a Fernet key, stores it in `.env` with
`REMOTE_JOBS=1`, and uploads the same value as the `REMOTE_JOB_KEY` repository
secret. Job payloads are encrypted with that key before they are passed to
GitHub, so source links and upload credentials are not stored in the dispatch
request in plaintext.

The PC cancels any active `bot.yml` fallback run before polling, so the two
never fight over Telegram updates. `job.yml` workers are deliberately left
alone: they keep running across PC restarts and post their own result cards.
On exit (Ctrl+C or `/handoff`) the PC starts one `bot.yml` fallback run. If the
PC loses power or is turned off without a handoff, the bot is offline until the
PC comes back; Telegram keeps updates for 24 hours.

If you have an always-on Linux box, the included Docker image is a good
alternative. [deploy/oracle-cloud.md](deploy/oracle-cloud.md) describes Oracle
Cloud's Always Free VM, but Oracle now requires card verification and may stop
idle instances, so use it only if that suits you.

## Cloudflare Worker controller (no PC)

The Cloudflare Worker is a third mode: Telegram calls a Worker webhook, and the
Worker dispatches the same one-shot `job.yml` runs. No PC, Docker, VPS, or open
port is needed. Cloudflare's free plan handles the command traffic; every
download, partition extraction, and upload still runs on GitHub Actions.

### How it works

- Telegram sends `POST /telegram` with the `X-Telegram-Bot-Api-Secret-Token`
  header. The Worker rejects requests without the matching secret.
- A Durable Object stores active jobs, deduplicates Telegram update IDs, and
  caches the "is an owner still in this group?" check.
- The Worker parses the same commands as the PC bot, encrypts the same Fernet
  payload, and calls the GitHub `workflow_dispatch` API with
  `return_run_details: true`.
- The Actions worker edits the Telegram progress card and posts the final link.
  It still needs the `TELEGRAM_BOT_TOKEN` and `REMOTE_JOB_KEY` repository
  secrets.
- A one-minute cron trigger checks active runs. It posts the cancellation card
  after a cancel, and a failure card only when the progress card still exists,
  so a normal worker failure is not reported twice.

### Setup

Requirements: a Cloudflare account, Node.js 20+ (22 recommended), Wrangler, and
a GitHub token that can dispatch and cancel Actions runs in this repository.
The free Cloudflare plan is enough for a personal bot. If the 10 ms CPU limit
per invocation becomes a problem, Workers Paid is $5/month; network time does
not count toward that limit.

1. **Deploy and upload the Worker secrets.**

   ```powershell
   cd cloudflare
   npm ci
   npx wrangler login
   cd ..
   powershell -ExecutionPolicy Bypass -File scripts\configure-cloudflare.ps1
   ```

   The script reads the existing `.env`, creates the gitignored
   `cloudflare\.dev.vars`, deploys the Worker, uploads the secrets with
   `wrangler secret bulk`, saves `CLOUDFLARE_WORKER_URL` in `.env`, and checks
   `/health`. This first version is already set up for
   `Farh4d979/mirror-bot`; for a fork, change `ACTIONS_REPO` in
   `cloudflare\wrangler.toml` before deploying.

   The GitHub token comes from `$env:GITHUB_TOKEN`, `GITHUB_TOKEN` in `.env`, or
   `gh auth token`. A fine-grained token needs **Actions: read and write** on
   the repository. The script never prints a secret value.

2. **Check the Worker before touching Telegram.**

   ```powershell
   Invoke-RestMethod https://YOUR-WORKER.workers.dev/health | ConvertTo-Json
   ```

   Wait for `"ok": true` and `"ready": true`. `missing` lists secret names that
   were not uploaded; it never lists their values. `/health` is public, but it
   exposes only readiness and the active job count.

3. **Cut Telegram over.** This stops and disables the `MirrorBot` scheduled
   task, then calls `setWebhook` with `drop_pending_updates=true` so old
   commands are not replayed:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode on
   ```

   `-Mode on` refuses to continue unless `/health` is ready. Send `/id`, then
   test a small link such as
   `/gf https://proof.ovh.net/files/1Mb.dat`, and press the cancel button on
   another job.

4. **Check or change the webhook.**

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode status
   ```

5. **Go back to the PC poller.**

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\cloudflare-webhook.ps1 -Mode off -StartPcBot
   ```

   This deletes the webhook, re-enables the scheduled task, and starts it. You
   can also switch back manually: delete the webhook in BotFather or with
   `deleteWebhook`, then run `Start-ScheduledTask -TaskName MirrorBot`.

### Worker secrets and settings

| Worker secret | Where it comes from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `.env`; must be the same bot as the Actions secret |
| `TELEGRAM_WEBHOOK_SECRET` | Generated by `configure-cloudflare.ps1`; also used by `setWebhook` |
| `GITHUB_TOKEN` | `gh auth token`, `$env:GITHUB_TOKEN`, or `GITHUB_TOKEN` in `.env` |
| `REMOTE_JOB_KEY` | `.env`; must match the GitHub Actions secret exactly |
| `ALLOWED_USER_IDS` | `.env`; owner IDs for DMs and trusted groups |
| `GOFILE_TOKEN` | Optional, from `.env` |
| `PIXELDRAIN_API_KEY` | Optional, required for `/px` and `/compress-px` |

Non-secret Worker settings live in `cloudflare\wrangler.toml`: `ACTIONS_REPO`,
`ACTIONS_REF`, `REMOTE_WORKFLOW`, `MAX_FILE_SIZE`, `JOB_TIMEOUT`,
`MULTI_FILE_MODE`, and `YTDLP_FORMAT`. They are sent inside the encrypted job
payload, so the Actions worker uses the Worker's values rather than its own
repository variables.

The Worker does not read `.env` at runtime. `.env` is only the local input for
the deploy script; `cloudflare\.dev.vars` is for `wrangler dev` and is
gitignored.

### Cloudflare limitations

- **Only one update consumer.** The webhook and PC long polling cannot both
  run. `-Mode on` stops and disables `MirrorBot`, so a later logon does not
  start a second poller. Do not start `scripts\run-local.cmd` while the webhook
  is active.
- **Webhook secret is required.** Telegram includes it on every update; the
  Worker returns 403 without it. The Worker URL itself is public, like any
  webhook endpoint.
- **Free plan limits.** 100,000 requests/day and 10 ms CPU per invocation are
  normally plenty, because the Worker only parses commands and makes API calls.
  Cron runs once a minute. Exceeding the CPU limit requires Workers Paid.
- **Commands are acknowledged after processing.** If GitHub or Telegram is
  slow, Telegram retries the update. The Durable Object deduplicates update IDs
  and the job reservation, so a retry does not dispatch the same job twice.
- **Replied files are still limited to 20 MB.** Telegram's Bot API download
  limit applies to the Actions worker too; send a direct link for larger files.
- **The runner limits do not change.** Expect a 1–3 minute runner start, a
  ~14 GB disk, a 6-hour cap, public Actions logs in a public repo, and
  datacenter-IP blocks. The GitHub Terms of Service risk is the same as in
  split mode.
- **No local queue survives a Cloudflare outage.** Active jobs are tracked in
  the Durable Object; finished jobs are removed when the cron monitor sees
  them. The Actions worker still posts its own final card if the Worker is
  briefly unavailable.

### Worker development and tests

```powershell
cd cloudflare
npm test
npm run typecheck
npx wrangler dev
```

`npm test` includes cross-language fixtures: the Worker decrypts a token
generated by Python's `cryptography.fernet.Fernet`, and the Python test suite
covers the Actions-side payload. The Worker uses WebCrypto, so no Node-only
crypto package is needed at runtime. CI runs the Python and Cloudflare test
jobs from `.github/workflows/tests.yml`.

For local webhook testing, `wrangler dev` loads `cloudflare\.dev.vars`. Send
updates with curl and the `X-Telegram-Bot-Api-Secret-Token` header; do not point
the real Telegram webhook at a dev tunnel while the production webhook is
active.

## GitHub Actions setup

The job workflow has **no schedule**. It runs once per job dispatched by the
PC. The fallback `bot.yml` workflow also has no schedule and no self-queue; it
only runs when the PC hands over or when you start it manually from **Actions → Telegram Mirror Bot →
Run workflow**. A run stops after its time limit instead of keeping GitHub
Actions busy indefinitely.

1. **Create the repository** and push this folder to it.

   ```bash
   git init
   git add .
   git commit -m "Initial mirror bot"
   git branch -M main
   git remote add origin https://github.com/YOUR_NAME/mirror-bot.git
   git push -u origin main
   ```

   A **public** repository is strongly recommended. Standard GitHub-hosted runners are free for public repositories; private repositories only get 2,000 minutes/month, which is not enough for long fallback runs.

2. **Create the Telegram bot.** Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.

   For inline mode, also send `/setinline` to BotFather, pick the bot, and enter a placeholder such as `Mirror a link…`. Then send `/setinlinefeedback`, pick the bot, and choose **100%**. With feedback enabled the job starts as soon as the inline result is picked. Without it Telegram never tells the bot which message was posted, so the card shows a **▶️ Start mirroring** button instead; tap it to start the job. These two settings can only be changed in BotFather; the Bot API cannot enable them.

3. **Get your Telegram user ID.** Message [@userinfobot](https://t.me/userinfobot), or leave `ALLOWED_USER_IDS` empty for the first run, start the bot and send `/id`.

4. **Add repository secrets** (Settings → Secrets and variables → Actions → Secrets):

   | Secret | Required | Purpose |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | yes | BotFather token |
   | `ALLOWED_USER_IDS` | recommended | Comma-separated IDs allowed to DM the bot, e.g. `123456789,987654321`. Groups are enabled while one of these IDs is a member. Used by the fallback poller. |
   | `REMOTE_JOB_KEY` | yes for split mode | Fernet key shared with the PC. `scripts\configure-remote.ps1` generates it and uploads it for you. Never paste it into chat or a commit. |
   | `GOFILE_TOKEN` | optional | Uses your Gofile account for uploads and share-link downloads instead of guest tokens. The split worker receives this from the encrypted payload, so this secret is only needed by the fallback poller. |
   | `PIXELDRAIN_API_KEY` | optional | Required for `/px` and `/compress-px` uploads. Also authenticates Pixeldrain downloads; a Pro/Prepaid key uses the account's own transfer quota. The split worker receives this from the encrypted payload. |

   Optional repository **variables** (Settings → Secrets and variables → Actions → Variables): `MAX_FILE_SIZE` (`0`, unlimited), `MAX_CONCURRENT_JOBS` (`1`), `JOB_TIMEOUT` (`7200`), `MULTI_FILE_MODE` (`largest`), `YTDLP_FORMAT` (`bv*+ba/b`), `GOFILE_WT_SALT` (only needed if Gofile rotates its website-token salt). These apply to the fallback poller; the split worker receives the PC's current values inside the encrypted payload.

5. **Start a fallback run only when needed.** Open the **Actions** tab → **Telegram Mirror Bot** → **Run workflow**. Leave `run_seconds` blank for 4h45m, or set a small value such as `60` for a test. The PC runner normally does this for you.

   Split-mode jobs appear as **Mirror Job Worker** runs. Do not cancel them unless you want to stop that job; the PC's `/cancel` does this for you.

### GitHub Actions limitations

- **Runner startup takes 1-3 minutes.** Each `job.yml` job waits for a fresh Ubuntu runner. Small files therefore take longer than they would on the PC.
- **One job is capped at 6 hours.** A fallback run polls for 4h45m (`BOT_RUN_SECONDS=17100`) and exits. A split worker also has a 350-minute workflow timeout. Neither queues a successor; start another run only if you still need the fallback.
- **Only one poller at a time.** Telegram allows a single `getUpdates` consumer per bot token. The PC runner cancels active `bot.yml` runs and waits before it starts polling; if you run both, one gets `409 Conflict`. `job.yml` workers do not poll and are left running.
- **No automatic failover.** If the PC crashes, sleeps, or loses power without a handoff, nothing starts the fallback poller. Telegram keeps updates for 24 hours, so they are delivered when the PC returns. Split workers that were already dispatched keep running.
- **Private repos run out of minutes.** 2,000 minutes/month is roughly 33 hours. Use a public repo for Actions work.
- **Disk is limited.** The standard runner has about 14 GB of SSD. A single file must fit on the runner; the practical ceiling is roughly 12 GB. Set `MAX_FILE_SIZE` if you want a hard cap.
- **State commits.** The fallback workflow pushes a tiny state commit (`chore: update bot state [skip ci]`) when the Telegram offset changes. If `main` is protected, allow GitHub Actions to push, or change the workflow to use a cache instead. The PC passes its current offset into the fallback run so already-handled updates are not replayed.
- **Actions logs are public in a public repo.** The job payload is encrypted, but host errors and filenames can still appear in the worker log. Do not put secrets in filenames or URLs.
- **Inline mode needs BotFather settings.** `/setinline` enables it. `/setinlinefeedback` at 100% makes the job start automatically when the result is picked; without it the posted card shows a **▶️ Start mirroring** button. Inline jobs are authorized by the user who runs the query; the target chat is unknown to the bot. Inline cards cannot be deleted, so they are edited in place.
- **No inbound webhook.** The bot uses long polling, so commands are only handled while the PC or a fallback run is active.
- **Datacenter IPs get blocked.** YouTube and other video sites often ask for a sign-in from GitHub's IPs, and BitTorrent traffic may be throttled. A file that works on your PC can still fail on the runner.
- **End-of-run grace period.** A fallback run stops accepting new jobs before its time limit and waits up to 20 minutes for active jobs to finish.
- **Terms of service.** GitHub Actions is intended for the repository's own software work. Using it to mirror arbitrary files is a risk to your account. Keep the bot owner-only and use it occasionally rather than as a public service.

## Local setup

Requirements: Python 3.10+ (3.12 recommended), [aria2](https://aria2.github.io/) and ffmpeg. Without aria2, direct HTTP downloads still work through Python, but torrents/magnets do not. MEGA links additionally need megatools (`megadl`, or the newer Windows `megatools.exe`). `-dump` extraction needs [payload-dumper-go](https://github.com/ssut/payload-dumper-go/releases) and [7-Zip](https://www.7-zip.org/) for `.7z`/`.rar` ROMs; `/compress-gf` and `/compress-px` also need 7-Zip. gallery-dl and yt-dlp are installed from `requirements.txt`.

On Windows, the setup script installs aria2, ffmpeg, 7-Zip, megatools,
payload-dumper-go, creates `.venv`, and installs the Python dependencies:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
# fill in .env with the hidden prompt (or edit .env in an editor)
powershell -ExecutionPolicy Bypass -File scripts\configure-env.ps1
gh auth login
scripts\run-local.cmd
```

See [deploy/windows-pc.md](deploy/windows-pc.md) for handoff behavior,
auto-start, and troubleshooting.

Generic manual setup:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# edit .env and set TELEGRAM_BOT_TOKEN
python -m bot
```

Install the system tools on Linux/macOS:

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y aria2 ffmpeg megatools
# macOS
brew install aria2 ffmpeg megatools
```

`LOCAL_MODE=1` enables the PC handoff logic. The normal local runner sets it
automatically, uses `.state/local-telegram.json` to keep the tracked Actions
state clean, and requires `gh auth login`. Set `LOCAL_MODE=0` or run
`scripts\run-local.cmd nohandoff` if you want a plain local bot without GitHub
coordination.

`REMOTE_JOBS=1` enables split mode: the PC polls Telegram and dispatches every
mirror job to `job.yml`. It requires `REMOTE_JOB_KEY` (the same Fernet key as
the GitHub secret) and `gh auth login`. Run
`scripts\configure-remote.ps1` once to generate the key, update `.env`, and
upload the secret. You can use split mode together with `LOCAL_MODE=1`; the
fallback then still exists, but jobs while the PC is online run on Actions.


## Docker

The included `docker-compose.yml` is a convenient alternative for an
always-on Linux host. It uses a persistent volume for `/data`, restarts the
container after a reboot, and builds natively on both x86_64 and ARM64. Do not
run it at the same time as the Windows PC runner or a GitHub Actions fallback;
each is an independent Telegram poller.

```bash
cp .env.example .env
# edit .env and set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS

docker compose up -d --build
docker compose logs -f
```

Stop it with `docker compose down` (`down -v` also deletes the downloads and
state volume).

Manual alternative without Compose:

```bash
docker build -t mirror-bot .
docker run -d --name mirror-bot \
  --env-file .env \
  -v mirror-data:/data \
  --restart unless-stopped \
  mirror-bot
```

The image includes aria2, ffmpeg, megatools, payload-dumper-go, 7-Zip and gallery-dl. Downloads and state live in `/data`. Debian's `7zip` package reads `.7z` archives but not RAR; install the non-free `7zip-rar` plugin in the image if you need RAR there.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token (required) |
| `ALLOWED_USER_IDS` | empty | Comma-separated Telegram user IDs allowed to use the bot in private chats. Groups work while at least one of these IDs is a member. Empty means anyone can DM the bot and use any group. |
| `GOFILE_TOKEN` | empty | Optional Gofile account token. Empty = guest uploads. |
| `PIXELDRAIN_API_KEY` | empty | Pixeldrain API key. Required for `/px` and `/compress-px`; also used to authenticate Pixeldrain downloads. |
| `MAX_FILE_SIZE` | `0` | Maximum downloaded file size; `0` = no bot-side limit |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel local/fallback jobs (`1` in the Actions workflow). Split-mode jobs each get their own runner. |
| `JOB_TIMEOUT` | `7200` | Per-job timeout in seconds |
| `BOT_RUN_SECONDS` | `0` | Stop after N seconds; `0` = run until stopped. Actions fallback runs use `17100`. |
| `LOCAL_MODE` | `0` | `1` = PC-primary mode: cancel active Actions runs on startup and coordinate handoff. |
| `AUTO_HANDOFF` | `LOCAL_MODE` | Start one Actions fallback when the PC runner exits. `0` disables the handoff. |
| `ACTIONS_REPO` | detected | Optional `owner/name` for handoff and remote jobs; normally read from the current `gh` login. |
| `ACTIONS_REF` | current branch | Optional branch that the fallback and job workflows should run. |
| `REMOTE_JOBS` | `0` | `1` = split mode: the PC only polls Telegram and dispatches each job to `job.yml`. Requires `REMOTE_JOB_KEY`. |
| `REMOTE_JOB_KEY` | empty | Fernet key shared with the `REMOTE_JOB_KEY` GitHub secret. Encrypts job payloads; keep it out of git and chat. |
| `REMOTE_WORKFLOW` | `job.yml` | Worker workflow name used for dispatch. |
| `REMOTE_STATE_FILE` | `.state/remote-jobs.json` | PC-side tracking file for dispatched Actions runs. |
| `MULTI_FILE_MODE` | `largest` | For torrents/multi-file downloads: upload the largest file, or `zip` everything |
| `YTDLP_FORMAT` | `bv*+ba/b` | yt-dlp format selector |
| `COOKIES_FILE` | empty | Optional Netscape cookies file for restricted video sites and galleries |
| `DOWNLOAD_DIR` | `downloads` | Temporary download directory |
| `STATE_FILE` | `.state/telegram.json` | Telegram offset state. The PC runner uses `.state/local-telegram.json`. |
| `ARIA2C_PATH` | `aria2c` | Path to the aria2c binary |
| `PAYLOAD_DUMPER_PATH` | `payload-dumper-go` | Path to payload-dumper-go, used by `-dump` |
| `SEVEN_ZIP_PATH` | `7z` | Path to the 7-Zip executable, used by `-dump` for `.7z`/`.rar` archives and by `/compress-gf`/`/compress-px` |
| `ALLOW_PRIVATE_URLS` | `0` | **Dangerous.** Allows localhost/private IP links. Keep off in production. |
| `LOG_LEVEL` | `INFO` | Python log level |

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Usage |
| `/gf <link>` | Upload a link to Gofile, or reply to a file with `/gf` |
| `/gf <link> -dump boot,vendor` | Extract only those Android partitions from `payload.bin` or `super.img` (ZIP, TAR, 7z or RAR), then upload each one |
| `/compress-gf <link>` | Pack the download into a maximum-compression 7z, report its SHA-256, and upload it to Gofile |
| `/compress-px <link>` | Same, but upload the 7z to Pixeldrain (requires `PIXELDRAIN_API_KEY`) |
| `/mirror <link>` | Same as `/gf` |
| `/px <link>` | Upload a link to Pixeldrain, or reply to a file with `/px` (requires `PIXELDRAIN_API_KEY`) |
| `/status` | Active jobs |
| `/cancel` | Cancel your active jobs |
| `/id` | Show your Telegram user/chat ID |
| `/handoff` | PC runner only: stop the local bot and start the Actions fallback |

Send a link or a magnet link directly. To mirror a file, GIF or `.torrent` that is already in the chat, reply to it with `/gf`, `/mirror`, `/px`, `/compress-gf`, or `/compress-px`. Attachments sent without one of those commands are ignored. Cloud bots can download replied attachments up to 20 MB.

In groups, use commands such as `/gf@YourBot <link>`. Plain links are ignored there so the bot does not reply to every message. Private chats are owner-only; a group is enabled while at least one `ALLOWED_USER_IDS` member is in it.

### Inline mode

Type `@YourBot /gf <link>` in any chat, including a private chat with someone else, and pick the result. If inline feedback is enabled in BotFather the job starts automatically. Otherwise the posted card shows **▶️ Start mirroring**; tap it once to start. The bot posts the card in that chat and keeps editing it with live progress and the final link, so the result is visible to everyone there without opening a separate chat with the bot. `/px <link>`, `/compress-gf <link>`, `/compress-px <link>` and `-dump boot,vendor` work the same way.

Inline mode is authorized by the Telegram account that types the query, not by the target chat, because Telegram does not reveal the target chat to the bot. When `ALLOWED_USER_IDS` is set, only those users can use it. Inline cards cannot be deleted by bots, so the completion card is edited in place instead of the delete-then-send used in normal chats. Reply-to-file is not available inline; send a direct link instead.

### Android partition dumps (`-dump`)

`/gf <link> -dump boot,vendor` works like a normal mirror job, but instead of uploading the downloaded archive it looks inside it for Android partition images and uploads only the requested ones. The syntax also accepts spaces and `.img` suffixes: `-dump boot vendor`, `-dump boot.img,vendor.img`.

Supported sources inside the download:

| Source | How it is handled |
|---|---|
| OTA `.zip` containing `payload.bin` (full OTA, or a partial payload that needs no source images) | `payload-dumper-go` extracts only the selected partitions |
| Raw `payload.bin` | Same as above |
| Fastboot ROM `.zip` / `.tar` / `.tar.gz` / `.tgz` / `.7z` / `.rar` containing `super.img` | The LP metadata is parsed and only the requested extents are copied (sparse `super.img` works too) |
| ROM with `super.img` split into `super.img.0`, `super.img.1`, … (ZIP/TAR or plain files) | The parts are merged on the fly (sparse or raw), without writing the full `super.img` to disk |
| Raw or already-extracted `boot.img`, `vendor.img`, … | Used directly |
| ZIP/TAR/7z/RAR containing raw partition images | Only the matching `.img` members are extracted; 7z/RAR members are streamed straight to disk |

If a logical partition exists in both slots, `-dump vendor` picks `vendor_a` (slot A); ask for `vendor_a,vendor_b` to get both. File names in the result are the actual partition names, so `vendor` may produce `vendor_a.img`. Partitions that were requested but do not exist are listed in the final message; if none exist, the job fails with the available partition names. Incremental (delta) OTAs need the previous firmware and are not supported. Password-protected 7z/RAR archives, `.lz4` members and Samsung `.tar.md5` firmware are not supported — extract those first.

`-dump` works with both `/gf` and `/px`, and with a file you reply to.

Extraction needs free disk space for the requested images, not for the whole `super.img`, but the downloaded ROM is kept until the job finishes. payload-dumper-go and 7-Zip are installed by the GitHub Actions workflow, the Docker image (7-Zip only), and `scripts\setup-windows.ps1`. For a manual local run, install [7-Zip](https://www.7-zip.org/) so `7z`/`7zz` is on `PATH` (or set `SEVEN_ZIP_PATH`), download payload-dumper-go from [ssut/payload-dumper-go](https://github.com/ssut/payload-dumper-go/releases) and put it on `PATH` (or set `PAYLOAD_DUMPER_PATH`), and install `xz` if your payloads need it.

### Maximum compression (`/compress-gf`, `/compress-px`)

`/compress-gf <link>` and `/compress-px <link>` download the file, pack it with 7-Zip at maximum compression (`-mx=9`, LZMA2, solid), print the archive's SHA-256, and upload the `.7z` to Gofile or Pixeldrain. The final card lists the archive name, size, SHA-256, and download link. Reply to an attachment with `/compress-gf` or `/compress-px` to compress a Telegram file.

Add `-dump` to compress the extracted partitions instead of the original download. The requested images are packed into one archive named after the source and partitions, for example `ota-boot-vendor.7z`; the images are not uploaded separately.

Compression needs [7-Zip](https://www.7-zip.org/) just like `-dump`. It runs in the shared job pipeline, so the PC fallback, GitHub Actions workers, and Docker image behave the same. The archive is created inside the job's temporary directory and is removed with it. Password protection is not supported.

## Supported links

| Source | Support |
|---|---|
| Direct file URLs (any type: `.zip`, `.iso`, `.apk`, `.pdf`, `.mp3`, `.mp4`, …) | ✅ aria2c |
| Telegram files replied to with `/gf` or `/px` (documents, photos, video, audio, voice, stickers) | ✅ direct upload, ≤20 MB |
| Pages with a Download button | ✅ heuristic resolver |
| GitHub releases | ✅ GitHub API picks the first asset |
| Google Drive single files | ✅ converted to a direct download URL |
| Dropbox share links | ✅ `dl=1` |
| Pixeldrain file links (`pixeldrain.com/u/...`) | ✅ direct API URL, authenticated when `PIXELDRAIN_API_KEY` is set |
| Gofile share links (`gofile.io/d/...`) | ✅ Gofile API, guest token, folders supported |
| MediaFire / SourceForge / similar pages | ✅ when the download link is in the HTML |
| YouTube, Vimeo, TikTok, X, Reddit, etc. | ✅ yt-dlp |
| Image galleries: Pixiv, Danbooru, Gelbooru, Imgur albums, DeviantArt, Flickr, ArtStation, Tumblr | ✅ gallery-dl |
| Public MEGA file and folder links | ✅ megatools (`megadl`) |
| Magnet links and `.torrent` files | ✅ aria2c |
| Google Drive folders | ❌ not supported |
| Password-protected Gofile folders | ❌ not supported |
| Login/CAPTCHA/DRM-protected pages | ❌ not supported |

The resolver is intentionally best-effort. Sites that build download links in JavaScript after a login or CAPTCHA cannot be mirrored without site-specific code.

Pixeldrain's free plan allows uploads up to 10 GB per file and roughly 6 GB of downloads per IP per day. Free download limits are tracked per IP, so a free API key does not raise them. A Pixeldrain Pro or Prepaid account has an account-bound transfer quota and also allows direct/hotlinked downloads; `PIXELDRAIN_API_KEY` lets the bot use that quota. The bot does not bypass Pixeldrain's limits or CAPTCHA — use a paid plan if you need more.

## Troubleshooting

**"That page does not expose a direct file"** — the page needs a login, CAPTCHA, or JavaScript that the resolver cannot execute. Try the original file URL.

**A replied file is rejected as too large** - Telegram's cloud Bot API only lets bots download attachments up to 20 MB. Send a direct link to the file instead; the bot can then mirror it up to the disk and Gofile limits (or `MAX_FILE_SIZE` if you set one).

**Gofile says the token is rejected** — remove `GOFILE_TOKEN` to use guest uploads, or create a fresh token from your Gofile profile.

**Gofile refuses a large upload (`error-limits`)** — Gofile enforces its limits per account on the server: guest 1 GB/file, verified free 5 GB/file, Pro 10 GB/file. The bot sends the file and reports Gofile's answer instead of pre-rejecting it. Changing IP, clearing browser data, or using a VPN does not change account limits. Add `GOFILE_TOKEN` for your plan's limit, upgrade, or use `/px` (Pixeldrain free: 10 GB/file).

**Gofile is rate-limiting downloads (HTTP 429)** — the bot serializes Gofile downloads, retries 429/5xx responses with backoff, and reduces aria2 connections for Gofile. If it still fails, set `MAX_CONCURRENT_JOBS=1`, add `GOFILE_TOKEN` (signed-in accounts get better limits), or wait a few minutes.

**Pixeldrain says `hotlink_detected`, `download_limit_exceeded`, or `ip_download_limited_captcha_required`** — Pixeldrain's free download quota is per IP and cannot be raised by a free API key. A Pro or Prepaid account has an account-bound quota and allows hotlinked downloads; add its API key as `PIXELDRAIN_API_KEY`. The bot does not bypass this limit or solve CAPTCHAs.

**Pixeldrain rejects the API key (`authentication_failed`)** — create a fresh key at `https://pixeldrain.com/user/api_keys` and update the `PIXELDRAIN_API_KEY` secret. Keys are only used on `pixeldrain.com` hosts.

**`/px` or `/compress-px` says `PIXELDRAIN_API_KEY` is not set** - `/gf` and `/mirror` still work with Gofile. Pixeldrain requires an account key for uploads, but the account is free and allows 10 GB per file: create one at `https://pixeldrain.com/user/api_keys` and add it to `.env` (and to the repository secret if you use the fallback poller). There is no upload size cap in the bot itself.

**A Gofile share link fails** — the bot reads share links with a temporary guest token, so no account is needed. Password-protected folders are not supported. If Gofile changes its website-token salt, set `GOFILE_WT_SALT` to the current value from `https://gofile.io/js/wt.obf.js`; the default is `12af056dacea0b`.

**Torrents do not start** — install aria2c (`aria2c --version`) and make sure the PC tools, workflow, or Docker image includes it. A magnet with no seeders stops after 5 minutes (`--bt-stop-timeout=300`).

**Video download fails** - update yt-dlp (`pip install -U yt-dlp`). Age/region-locked sites may need `COOKIES_FILE`. GitHub Actions IPs are frequently blocked by YouTube; set `REMOTE_JOBS=0` temporarily if you need the PC's home connection.

**Gallery download fails** - update gallery-dl (`pip install -U gallery-dl`). Login-only galleries may need `COOKIES_FILE`. Direct image URLs still go through aria2c and do not need gallery-dl.

**MEGA download fails** - install megatools (`apt install megatools`, or run `scripts\setup-windows.ps1` on Windows). Newer Windows builds provide `megatools.exe` instead of `megadl.exe`; the bot handles both. The link must be a public/exported file or folder link, and MEGA transfer quotas still apply.

**Torrents do not connect** — many datacenter networks block BitTorrent. Set `REMOTE_JOBS=0` to use the PC's connection, or try a direct link.

**The Actions run stops after 4h45m** — that is expected. On-demand runs do not queue a successor. Start another run only if you still need the fallback; otherwise let the PC take over.

**The bot is offline** — first check whether the PC runner is running (`scripts\run-local.cmd`). If the PC is off, nothing starts the fallback automatically. Start the workflow manually from the Actions tab, or turn the PC on. Telegram keeps updates for 24 hours.

**`409 Conflict`** — another process is polling the same bot token. Run `gh run list --workflow bot.yml` and cancel any active run before starting the PC bot, or stop the PC bot before starting an Actions run. The PC runner normally does this automatically.

**`certificate is not yet valid` or the Worker health check fails** — the Windows clock is behind real time. Newly issued `workers.dev` certificates are rejected until the clock catches up, even though the Worker is healthy. Open **Settings → Time & language → Date & time → Sync now**, or run `Start-Service w32time; w32tm /resync` in an elevated prompt, then retry.

**The handoff did not start** — check `gh auth status` and that the repository is correct (`ACTIONS_REPO`). The PC logs the `gh` error. You can also start the workflow manually from the Actions tab.

**The state push fails** — the repository's default branch is probably protected. Allow GitHub Actions to push, or replace the state step with `actions/cache` restore/save.

**Disk full** — set `MAX_FILE_SIZE` to a value that fits the runner (about 12 GB practical) or the PC for local jobs. Each job deletes its temporary files when it finishes.

## Security and legal notes

- Set `ALLOWED_USER_IDS`. In private chats the bot only answers those IDs. A group is enabled only while one of those IDs is a member, and every member can then use it, so only add it to groups you trust. Group members use your runner, bandwidth and upload accounts.
- The bot blocks localhost/private-network URLs to reduce SSRF risk, but do not expose it to untrusted users.
- Never commit `.env` or tokens. The GitHub Actions secrets are the right place.
- Only mirror content you have the right to copy. Respect the terms of service of the source site, Gofile, and Pixeldrain. yt-dlp, gallery-dl and megatools only access content the link already exposes; the bot does not bypass logins, paywalls, DRM or CAPTCHAs.
- Gofile guest uploads create a new public folder per upload. Use `GOFILE_TOKEN` if you want uploads tied to your account.
- The Pixeldrain API key is used for `/px` and `/compress-px` uploads and authenticated Pixeldrain downloads. It is sent only to `pixeldrain.com` hosts. The bot does not bypass Pixeldrain's limits, hotlink protection, or CAPTCHA; those require a paid Pixeldrain plan or the site's normal download flow.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The tests cover URL/filename handling, the HTML download-button resolver, Gofile and Pixeldrain resolution and uploads with mocked HTTP responses, command parsing, the downloader's header and file-collection logic, encrypted remote payloads, run tracking/cancellation, and the Actions worker's payload validation and Telegram card delivery.

The Cloudflare Worker has its own suite:

```bash
cd cloudflare
npm ci
npm test
npm run typecheck
```

It covers Fernet compatibility with Python, command parsing, group and owner
authorization, encrypted dispatch payloads, the job store, cancellation, and the
cron monitor's final cards.

## Project layout

```
bot.py                     Telegram handlers and polling lifecycle
mirror/
  config.py                Environment settings
  handoff.py               PC ↔ GitHub Actions dispatch, cancel and takeover
  remote.py                Encrypted job payloads and PC-side run tracking
  worker.py                One-shot Actions entry point for a single job
  render.py                Shared Telegram progress and result cards
  telegram_api.py          Small synchronous Bot API client for the worker
  resolver.py              Page → direct link
  downloader.py            aria2c / yt-dlp / requests engines
  jobs.py                  Queue, progress state, download → upload pipeline
  state.py                 Telegram update offset persistence
  uploaders/
    gofile.py              Gofile upload fleet API
    pixeldrain.py          Pixeldrain raw PUT uploader
.github/workflows/
  job.yml                  One-shot worker dispatched by the controller
  bot.yml                  On-demand GitHub Actions fallback poller
  tests.yml                Python and Cloudflare tests
cloudflare/
  wrangler.toml            Worker routes, Durable Object, cron and settings
  src/
    index.ts               Webhook and cron entry points
    handlers.ts            Commands, authorization, dispatch and cancel
    fernet.ts              Fernet-compatible WebCrypto payload encryption
    github.ts              Workflow dispatch, cancel and run status
    store.ts               Durable Object job store and client
    refresh.ts             Cron monitor for cancelled/failed runs
    render.ts              Telegram cards
    utils.ts               URL, filename and dump helpers
  test/                    Vitest suite
deploy/
  windows-pc.md            Recommended Windows PC + workers guide
  oracle-cloud.md          Optional Oracle Cloud Always Free guide
scripts/
  setup-windows.ps1        Installs Windows tools and Python dependencies
  configure-env.ps1        Writes the bot token and owner IDs to .env
  configure-remote.ps1     Generates REMOTE_JOB_KEY and uploads the secret
  configure-cloudflare.ps1 Deploys the Worker and uploads its secrets
  cloudflare-webhook.ps1   Switches Telegram between the Worker and the PC
  run-local.cmd            Starts the PC runner and enables handoff
  install-startup-task.ps1 Starts the PC runner at logon
docker-compose.yml         Alternative Docker deployment
Dockerfile                 aria2 + ffmpeg + bot (x86_64 and ARM64)
```

## License

Copyright (C) 2026 Farhad.

This project is licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE) for the full text.

You may fork, modify, and self-host it. If you distribute a modified version, the AGPL requires you to keep this license, preserve the copyright notices, and make the corresponding source available. If you run a modified version as a network service that other people use, those users must be able to get its source code. In short: you can fork it, but you cannot take it, strip the license, and republish it as a closed or hidden project.

Two practical notes:

- GitHub does not allow a fork of a public repository to be made private, so public forks stay public.
- No open-source license can forbid private, personal use of an unmodified copy. The AGPL's source-sharing obligations apply when you distribute the software or let others use it over a network.
