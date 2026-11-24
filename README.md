# Telegram Mirror Bot

A Telegram bot that takes a link or a file, downloads it if needed, and re-uploads it to **Gofile or Pixeldrain**.

It handles direct file links, web pages where the file is hidden behind a "Download" button, video sites supported by yt-dlp, image galleries supported by gallery-dl, public MEGA links, magnet links, `.torrent` files, and files sent directly in Telegram.

```
Telegram link / .torrent / file
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
- Android partition dumps: `/gf <link> -dump boot,vendor` downloads an OTA zip or fastboot ROM, extracts only the requested images from `payload.bin` or `super.img`, and uploads each image separately.
- Magnet links and `.torrent` files through aria2c (DHT enabled, seeding disabled).
- Files sent directly in Telegram — documents, photos, video, audio, voice notes, animations and stickers — are uploaded to Gofile. Reply with `/px` to send them to Pixeldrain instead. Telegram's cloud Bot API limits bot downloads to 20 MB per attachment.
- Gofile share links (`gofile.io/d/...`) resolve through Gofile's API using a temporary guest token. Folder links are supported; the bot downloads the largest file or every file in `MULTI_FILE_MODE=zip`.
- Gofile guest uploads work with no account. An optional `GOFILE_TOKEN` uses your account. The bot does not reject a file by size on its own; Gofile's server enforces the plan limits.
- Pixeldrain uploads (`/px`) use your Pixeldrain API key, with the free plan allowing 10 GB per file and Pro allowing 100 GB per file.
- Pixeldrain file links (`pixeldrain.com/u/...`) are downloaded through Pixeldrain's API. When `PIXELDRAIN_API_KEY` is set, the bot authenticates the download so a Pro/Prepaid account uses its own transfer quota instead of the shared free per-IP quota.
- Progress bars, cancel buttons, per-user job limits, size limits and timeouts.
- Private DMs are restricted to `ALLOWED_USER_IDS`; groups work while at least one of those users is a member. Channels are ignored.
- Runs on Docker or any small VPS. [Oracle Cloud Always Free](deploy/oracle-cloud.md) is the recommended free 24/7 host. GitHub Actions is supported as a fallback, but it is not intended to be a permanent bot host.

## Recommended 24/7 deployment

For a permanent host with no monthly bill, follow [deploy/oracle-cloud.md](deploy/oracle-cloud.md). It runs the included Docker image on Oracle's Always Free ARM VM (currently about 2 OCPU / 12 GB RAM / 200 GB disk). After the Oracle bot is working, GitHub Actions is only needed for tests.

## Quick start on GitHub Actions (fallback)

> GitHub Actions is not a true 24/7 host and running a persistent mirror bot there can conflict with GitHub's Actions terms. Use the Oracle/Docker deployment for normal use. Read [Actions limitations](#github-actions-limitations) before choosing this option.

1. **Create the repository** and push this folder to it.

   ```bash
   git init
   git add .
   git commit -m "Initial mirror bot"
   git branch -M main
   git remote add origin https://github.com/YOUR_NAME/mirror-bot.git
   git push -u origin main
   ```

   A **public** repository is strongly recommended. Standard GitHub-hosted runners are free for public repositories; private repositories only get 2,000 minutes/month, which is not enough to run a bot all month.

2. **Create the Telegram bot.** Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.

3. **Get your Telegram user ID.** Message [@userinfobot](https://t.me/userinfobot), or leave `ALLOWED_USER_IDS` empty for the first run, start the bot and send `/id`.

4. **Add repository secrets** (Settings → Secrets and variables → Actions → Secrets):

   | Secret | Required | Purpose |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | yes | BotFather token |
   | `ALLOWED_USER_IDS` | recommended | Comma-separated IDs allowed to DM the bot, e.g. `123456789,987654321`. Groups are enabled while one of these IDs is a member. |
   | `GOFILE_TOKEN` | optional | Uses your Gofile account for uploads and share-link downloads instead of guest tokens |
   | `PIXELDRAIN_API_KEY` | optional | Required for `/px` uploads. Also authenticates Pixeldrain downloads; a Pro/Prepaid key uses the account's own transfer quota. |

   Optional repository **variables** (Settings → Secrets and variables → Actions → Variables): `MAX_FILE_SIZE` (`0`, unlimited), `MAX_CONCURRENT_JOBS` (`1`), `JOB_TIMEOUT` (`7200`), `MULTI_FILE_MODE` (`largest`), `YTDLP_FORMAT` (`bv*+ba/b`), `GOFILE_WT_SALT` (only needed if Gofile rotates its website-token salt), `AUTO_RESTART` (`true`; set to `false` to stop the automatic hand-off).

5. **Enable Actions and run it.** Open the **Actions** tab → **Telegram Mirror Bot** → **Run workflow**. Leave `run_seconds` blank for the normal 4h45m run, or set a small value such as `60` for a short test. Wait for the job to start, then send your bot a link.

6. **Send a link or a file.** The bot downloads the file if needed, uploads it to the destination you chose, and replies with the public link.

### GitHub Actions limitations

- **One job is capped at 6 hours.** Each run polls for 4h45m (`BOT_RUN_SECONDS=17100`), then queues the next run before exiting. The 5-hour schedule is a fallback. A run that crashes without queuing is restarted by the keep-alive workflow within about 30 minutes. Messages sent while no run is active are delivered when the next run starts because Telegram keeps updates for 24 hours.
- **Only one poller at a time.** Telegram allows a single `getUpdates` consumer per bot token, so the workflow's concurrency group serializes runs. A single run can still process several uploads concurrently via `MAX_CONCURRENT_JOBS`.
- **Cron is not exact.** GitHub can delay scheduled runs by several minutes.
- **Private repos run out of minutes.** 2,000 minutes/month is roughly 33 hours; this bot needs ~720 hours/month. Use a public repo or a real host.
- **Disk is limited.** The standard runner has about 14 GB of SSD. The bot no longer imposes a file-size cap by default (`MAX_FILE_SIZE=0`), but a single file must still fit on the runner; the practical ceiling is roughly 12 GB. Gofile's own plan limits also apply: guest accounts allow 1 GB per file, free verified accounts 5 GB per file, and paid plans more. Set `MAX_FILE_SIZE` if you want a hard cap, and keep `MAX_CONCURRENT_JOBS=1` when mirroring large files.
- **Scheduled workflows can be disabled after 60 days without repository activity.** This workflow commits `.state/telegram.json` after each run, which keeps the repository active and also resumes the Telegram update offset.
- **State commits.** The workflow pushes a tiny state commit (`chore: update bot state [skip ci]`) when the Telegram offset changes, plus a weekly heartbeat so GitHub does not disable the schedule after 60 idle days. If `main` is protected, allow GitHub Actions to push, or change the workflow to use a cache instead.
- **Actions logs are public in a public repo.** Do not print tokens; the bot does not log them.
- **No inbound webhook.** The bot uses long polling, so it only handles messages while a workflow run is active. The keep-alive workflow starts runs; it does not receive messages itself.
- **Datacenter IPs get blocked.** YouTube and other video sites often ask for a sign-in from GitHub's IPs, and BitTorrent traffic may be throttled. For heavy video/torrent use, run the Docker image on a VPS.
- **End-of-run grace period.** The bot stops accepting new jobs when its run is ending and waits up to 20 minutes for active jobs to finish.

The automatic hand-off makes Actions effectively continuous, but it is still best-effort: GitHub can delay or disable schedules, and a run that fails before the hand-off waits for the keep-alive check. For guaranteed 24/7 operation, use the [Oracle Cloud Always Free guide](deploy/oracle-cloud.md) or deploy the included Docker image to a small VPS.

## Local setup

Requirements: Python 3.10+ (3.12 recommended), [aria2](https://aria2.github.io/) and ffmpeg. Without aria2, direct HTTP downloads still work through Python, but torrents/magnets do not. MEGA links additionally need megatools (`megadl`). `-dump` extraction needs [payload-dumper-go](https://github.com/ssut/payload-dumper-go/releases) and `xz`; gallery-dl and yt-dlp are installed from `requirements.txt`.

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

Install the system tools:

```powershell
# Windows
winget install aria2.aria2
winget install Gyan.FFmpeg
# MEGA links need megatools; use WSL or the Docker image on Windows.
```

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y aria2 ffmpeg megatools
# macOS
brew install aria2 ffmpeg megatools
```

## Docker

The included `docker-compose.yml` is the recommended way to run the bot. It
uses a persistent volume for `/data`, restarts the container after a reboot,
and builds natively on both x86_64 and ARM64 (including Oracle's Ampere VM).

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

The image includes aria2, ffmpeg, megatools, payload-dumper-go and gallery-dl. Downloads and state live in `/data`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token (required) |
| `ALLOWED_USER_IDS` | empty | Comma-separated Telegram user IDs allowed to use the bot in private chats. Groups work while at least one of these IDs is a member. Empty means anyone can DM the bot and use any group. |
| `GOFILE_TOKEN` | empty | Optional Gofile account token. Empty = guest uploads. |
| `PIXELDRAIN_API_KEY` | empty | Pixeldrain API key. Required for `/px`; also used to authenticate Pixeldrain downloads. |
| `MAX_FILE_SIZE` | `0` | Maximum downloaded file size; `0` = no bot-side limit |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel jobs (`1` in the Actions workflow) |
| `JOB_TIMEOUT` | `7200` | Per-job timeout in seconds |
| `BOT_RUN_SECONDS` | `0` | Stop after N seconds; `0` = run until stopped |
| `MULTI_FILE_MODE` | `largest` | For torrents/multi-file downloads: upload the largest file, or `zip` everything |
| `YTDLP_FORMAT` | `bv*+ba/b` | yt-dlp format selector |
| `COOKIES_FILE` | empty | Optional Netscape cookies file for restricted video sites and galleries |
| `DOWNLOAD_DIR` | `downloads` | Temporary download directory |
| `STATE_FILE` | `.state/telegram.json` | Telegram offset state |
| `ARIA2C_PATH` | `aria2c` | Path to the aria2c binary |
| `PAYLOAD_DUMPER_PATH` | `payload-dumper-go` | Path to payload-dumper-go, used by `-dump` |
| `ALLOW_PRIVATE_URLS` | `0` | **Dangerous.** Allows localhost/private IP links. Keep off in production. |
| `LOG_LEVEL` | `INFO` | Python log level |

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Usage |
| `/gf <link>` | Upload a link to Gofile, or reply to a file with `/gf` |
| `/gf <link> -dump boot,vendor` | Extract only those Android partitions from `payload.bin` or `super.img`, then upload each one |
| `/mirror <link>` | Same as `/gf` |
| `/px <link>` | Upload a link to Pixeldrain, or reply to a file with `/px` (requires `PIXELDRAIN_API_KEY`) |
| `/status` | Active jobs |
| `/cancel` | Cancel your active jobs |
| `/id` | Show your Telegram user/chat ID |

You can also send a link, a magnet link, a `.torrent` file, or any file directly. Files sent in Telegram are uploaded to Gofile; cloud bots can download attachments up to 20 MB. To mirror a file you already sent, reply to it with `/gf`, `/mirror`, or `/px`.

In groups, use commands such as `/gf@YourBot <link>`. Plain links are ignored there so the bot does not reply to every message. Private chats are owner-only; a group is enabled while at least one `ALLOWED_USER_IDS` member is in it.

### Android partition dumps (`-dump`)

`/gf <link> -dump boot,vendor` works like a normal mirror job, but instead of uploading the downloaded archive it looks inside it for Android partition images and uploads only the requested ones. The syntax also accepts spaces and `.img` suffixes: `-dump boot vendor`, `-dump boot.img,vendor.img`.

Supported sources inside the download:

| Source | How it is handled |
|---|---|
| OTA `.zip` containing `payload.bin` (full OTA, or a partial payload that needs no source images) | `payload-dumper-go` extracts only the selected partitions |
| Raw `payload.bin` | Same as above |
| Fastboot ROM `.zip` / `.tar` / `.tar.gz` / `.tgz` containing `super.img` | The LP metadata is parsed and only the requested extents are copied (sparse `super.img` works too) |
| Raw or already-extracted `boot.img`, `vendor.img`, … | Used directly |
| ZIP/TAR containing raw partition images | Only the matching `.img` members are extracted |

If a logical partition exists in both slots, `-dump vendor` picks `vendor_a` (slot A); ask for `vendor_a,vendor_b` to get both. File names in the result are the actual partition names, so `vendor` may produce `vendor_a.img`. Partitions that were requested but do not exist are listed in the final message; if none exist, the job fails with the available partition names. Incremental (delta) OTAs need the previous firmware and are not supported. `7z`/`RAR`, `.lz4` members and Samsung `.tar.md5` firmware are not supported — extract those first.

`-dump` works with both `/gf` and `/px`, and with a file you reply to.

Extraction needs free disk space for the requested images, not for the whole `super.img`, but the downloaded ROM is kept until the job finishes. payload-dumper-go is installed by the GitHub Actions workflow and the Docker image. For a local run, download it from [ssut/payload-dumper-go](https://github.com/ssut/payload-dumper-go/releases), put it on `PATH` (or set `PAYLOAD_DUMPER_PATH`), and install `xz`.

## Supported links

| Source | Support |
|---|---|
| Direct file URLs (any type: `.zip`, `.iso`, `.apk`, `.pdf`, `.mp3`, `.mp4`, …) | ✅ aria2c |
| Files sent to the bot in Telegram (documents, photos, video, audio, voice, stickers) | ✅ direct upload, ≤20 MB |
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

**A file sent in Telegram is rejected as too large** — Telegram's cloud Bot API only lets bots download attachments up to 20 MB. Send a direct link to the file instead; the bot can then mirror it up to the disk and Gofile limits (or `MAX_FILE_SIZE` if you set one).

**Gofile says the token is rejected** — remove `GOFILE_TOKEN` to use guest uploads, or create a fresh token from your Gofile profile.

**Gofile refuses a large upload (`error-limits`)** — Gofile enforces its limits per account on the server: guest 1 GB/file, verified free 5 GB/file, Pro 10 GB/file. The bot sends the file and reports Gofile's answer instead of pre-rejecting it. Changing IP, clearing browser data, or using a VPN does not change account limits. Add `GOFILE_TOKEN` for your plan's limit, upgrade, or use `/px` (Pixeldrain free: 10 GB/file).

**Gofile is rate-limiting downloads (HTTP 429)** — the bot serializes Gofile downloads, retries 429/5xx responses with backoff, and reduces aria2 connections for Gofile. If it still fails, set `MAX_CONCURRENT_JOBS=1`, add `GOFILE_TOKEN` (signed-in accounts get better limits), or wait a few minutes.

**Pixeldrain says `hotlink_detected`, `download_limit_exceeded`, or `ip_download_limited_captcha_required`** — Pixeldrain's free download quota is per IP and cannot be raised by a free API key. A Pro or Prepaid account has an account-bound quota and allows hotlinked downloads; add its API key as `PIXELDRAIN_API_KEY`. The bot does not bypass this limit or solve CAPTCHAs.

**Pixeldrain rejects the API key (`authentication_failed`)** — create a fresh key at `https://pixeldrain.com/user/api_keys` and update the `PIXELDRAIN_API_KEY` secret. Keys are only used on `pixeldrain.com` hosts.

**`/px` says `PIXELDRAIN_API_KEY` is not set** — `/gf` and `/mirror` still work with Gofile. Pixeldrain requires an account key for uploads, but the account is free and allows 10 GB per file: create one at `https://pixeldrain.com/user/api_keys` and add it as the `PIXELDRAIN_API_KEY` repository secret. There is no upload size cap in the bot itself.

**A Gofile share link fails** — the bot reads share links with a temporary guest token, so no account is needed. Password-protected folders are not supported. If Gofile changes its website-token salt, set `GOFILE_WT_SALT` to the current value from `https://gofile.io/js/wt.obf.js`; the default is `12af056dacea0b`.

**Torrents do not start** — install aria2c (`aria2c --version`) and make sure the workflow/Docker image includes it. A magnet with no seeders stops after 5 minutes (`--bt-stop-timeout=300`).

**Video download fails** - update yt-dlp (`pip install -U yt-dlp`). Age/region-locked sites may need `COOKIES_FILE`. GitHub Actions IPs are frequently blocked by YouTube; a VPS is more reliable for video sites.

**Gallery download fails** - update gallery-dl (`pip install -U gallery-dl`). Login-only galleries may need `COOKIES_FILE`. Direct image URLs still go through aria2c and do not need gallery-dl.

**MEGA download fails** - install megatools (`apt install megatools`). The link must be a public/exported file or folder link, and MEGA transfer quotas still apply.

**Torrents do not connect** — many datacenter networks block BitTorrent. Try a VPS, or use a direct link.

**The Actions run stops after 4h45m** — that is expected. The run queues its successor before exiting, so the bot normally comes back within a minute or two. The keep-alive workflow also checks every 30 minutes.

**The bot is offline for more than 30 minutes** — open the Actions tab and check for failed runs. If the `Queue next run` step failed, the keep-alive workflow should start a new run within 30 minutes; you can also run the workflow manually. To stop automatic restarts, set the repository variable `AUTO_RESTART` to `false` (this stops both the hand-off and the keep-alive) and disable the workflow.

**The state push fails** — the repository's default branch is probably protected. Allow GitHub Actions to push, or replace the state step with `actions/cache` restore/save.

**Disk full** — set `MAX_FILE_SIZE` to a value that fits the runner and keep `MAX_CONCURRENT_JOBS=1`. Each job deletes its temporary files when it finishes.

## Security and legal notes

- Set `ALLOWED_USER_IDS`. In private chats the bot only answers those IDs. A group is enabled only while one of those IDs is a member, and every member can then use it, so only add it to groups you trust. Group members use your runner, bandwidth and upload accounts.
- The bot blocks localhost/private-network URLs to reduce SSRF risk, but do not expose it to untrusted users.
- Never commit `.env` or tokens. The GitHub Actions secrets are the right place.
- Only mirror content you have the right to copy. Respect the terms of service of the source site, Gofile, and Pixeldrain. yt-dlp, gallery-dl and megatools only access content the link already exposes; the bot does not bypass logins, paywalls, DRM or CAPTCHAs.
- Gofile guest uploads create a new public folder per upload. Use `GOFILE_TOKEN` if you want uploads tied to your account.
- The Pixeldrain API key is used for `/px` uploads and authenticated Pixeldrain downloads. It is sent only to `pixeldrain.com` hosts. The bot does not bypass Pixeldrain's limits, hotlink protection, or CAPTCHA; those require a paid Pixeldrain plan or the site's normal download flow.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The tests cover URL/filename handling, the HTML download-button resolver, Gofile and Pixeldrain resolution and uploads with mocked HTTP responses, command parsing, and the downloader's header and file-collection logic.

## Project layout

```
bot.py                     Telegram handlers and polling lifecycle
mirror/
  config.py                Environment settings
  resolver.py              Page → direct link
  downloader.py            aria2c / yt-dlp / requests engines
  jobs.py                  Queue, progress state, download → upload pipeline
  state.py                 Telegram update offset persistence
  uploaders/
    gofile.py              Gofile upload fleet API
    pixeldrain.py          Pixeldrain raw PUT uploader
.github/workflows/
  bot.yml                  Optional GitHub Actions bot fallback
  keepalive.yml            Restarts the GitHub Actions fallback
  tests.yml                Unit tests
deploy/
  oracle-cloud.md          Free 24/7 Oracle Cloud deployment guide
docker-compose.yml         Recommended Docker deployment
Dockerfile                 aria2 + ffmpeg + bot (x86_64 and ARM64)
```

## License

Copyright (C) 2026 Farhad.

This project is licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE) for the full text.

You may fork, modify, and self-host it. If you distribute a modified version, the AGPL requires you to keep this license, preserve the copyright notices, and make the corresponding source available. If you run a modified version as a network service that other people use, those users must be able to get its source code. In short: you can fork it, but you cannot take it, strip the license, and republish it as a closed or hidden project.

Two practical notes:

- GitHub does not allow a fork of a public repository to be made private, so public forks stay public.
- No open-source license can forbid private, personal use of an unmodified copy. The AGPL's source-sharing obligations apply when you distribute the software or let others use it over a network.
