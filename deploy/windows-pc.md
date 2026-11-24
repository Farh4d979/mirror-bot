# Run the bot on your Windows PC

This is the recommended free setup. The PC is the normal Telegram poller, but in
split mode it does **not** download or upload anything. Each job is dispatched
to a one-shot GitHub Actions worker. The PC tracks the run id for `/status` and
`/cancel`, while the worker edits the Telegram progress message and posts the
final link.

`bot.yml` remains an **on-demand fallback poller**: the PC starts one run when
it hands over, and takes the bot back when it starts again. There is no schedule
and no self-queue in either workflow.

## How it works

```
PC online:     Telegram → PC controller → job.yml worker on GitHub Actions
                         ↓ /cancel
                         ↓ Ctrl+C or /handoff
PC handoff:    PC stops → one bot.yml fallback run (max 4h45m)
                         ↓ PC starts again
PC takeover:   PC cancels bot.yml → PC polls again
```

- **Only one poller.** Telegram allows one `getUpdates` consumer per bot token.
  The PC cancels any active `bot.yml` run and waits for it to finish before
  polling. `job.yml` workers do not poll and are left alone.
- **Jobs survive a PC restart.** A dispatched worker keeps running on GitHub and
  posts its own result. The PC reloads `.state/remote-jobs.json` to keep
  tracking it.
- **No automatic failover on power loss.** If the PC crashes or is switched off
  without a handoff, the bot is offline until the PC returns. Telegram keeps
  updates for 24 hours, so pending messages are delivered then. This is the
  tradeoff for not running GitHub Actions continuously.
- **The offset is preserved.** The PC passes its last Telegram update ID to the
  fallback run, and merges the offset persisted by Actions when it takes back.
  This avoids replaying old commands.
- **No open ports.** Long polling only needs outbound HTTPS.

## Requirements

- Windows 10 or 11, 64-bit.
- Python 3.12 from [python.org](https://www.python.org/downloads/). Enable
  **Add Python to PATH** during installation.
- Git.
- GitHub CLI (`gh`) from [cli.github.com](https://cli.github.com/), signed in
  with `gh auth login`.
- A GitHub repository containing this project (`origin` must point to it).
- A free public repository, or enough private-repo Actions minutes for the jobs.
- The Actions runner has about 14 GB of free disk, and each job is capped at 6
  hours. Large downloads must fit there, not on the PC.

## 1. Install the tools

From PowerShell in the project directory:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
```

The script:

- installs **aria2** and **ffmpeg** with winget;
- installs **megatools** and **payload-dumper-go** into `tools\`;
- creates `.venv` and installs `requirements.txt`;
- creates `.env` from `.env.example` if it does not exist.

The system tools are used by local and fallback runs. A split-mode worker installs
its own copy on the Actions runner, so the PC does not need them for remote jobs.
Open a new terminal after it finishes so the updated PATH is visible.

## 2. Configure `.env`

The easiest way is the setup prompt. It hides the token while you type and
writes `.env` for you:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\configure-env.ps1
```

It asks for the BotFather token, then for your Telegram user ID (message
[@userinfobot](https://t.me/userinfobot) to get it). Re-run it whenever the
token changes.

Or edit `.env` yourself and set at least:

```dotenv
TELEGRAM_BOT_TOKEN=your-botfather-token
ALLOWED_USER_IDS=your-telegram-user-id
```

Optional:

```dotenv
GOFILE_TOKEN=your-gofile-token
PIXELDRAIN_API_KEY=your-pixeldrain-key
MAX_CONCURRENT_JOBS=1
JOB_TIMEOUT=7200
MAX_FILE_SIZE=0
```

Do **not** leave `ALLOWED_USER_IDS` empty unless you really want anyone who
finds the bot to use your PC, bandwidth and upload accounts.

`LOCAL_MODE`, `STATE_FILE`, and the local PATH are set by `run-local.cmd`; you
do not need to put them in `.env`. If you want to point the fallback at a
different repository or branch, set `ACTIONS_REPO=owner/name` or
`ACTIONS_REF=main`.

### Enable split mode

Generate the shared payload key, write `REMOTE_JOBS=1` and the key to `.env`,
and upload the same key as a repository secret:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\configure-remote.ps1
```

The script prints neither the key nor the secret. Re-run it with `-Rotate` to
replace the key; restart the PC bot afterwards, and cancel any workers that were
started with the old key. Keep `REMOTE_JOB_KEY` out of chat, logs and commits.

## 3. Verify GitHub CLI access

```powershell
gh auth status
gh repo view --json nameWithOwner
```

`gh auth login` needs access to the repository, and the account must be allowed
to run workflows. If you use a fine-grained token, give it **Actions: write**
and **Contents: write** access.

## 4. Stop the current GitHub bot

Telegram will return `409 Conflict` if two processes poll at the same time.
The PC runner handles this automatically: on startup it lists active
`bot.yml` runs, cancels them, and waits until GitHub marks them completed.
`job.yml` workers are not pollers and are left running.

If that fails, cancel the run manually:

```powershell
gh run list --workflow bot.yml
gh run cancel <run-id>
```

Do this before the first PC start if an old scheduled run is still active.

## 5. Start the bot

```powershell
scripts\run-local.cmd
```

Wait for:

```
Bot polling started (runner=GitHub Actions, run_seconds=unlimited, ...)
```

Send `/start` to the bot in Telegram. Keep the window open and stop the bot
with **Ctrl+C** or **`/handoff`**; closing the console window can kill the
process before it starts the fallback.

### Handoff and takeover

- **`/handoff`** (owner, private chat): stops the PC bot and starts one
  fallback `bot.yml` run. It refuses while a job is active.
- **Ctrl+C**: stops the PC bot and starts the fallback. In split mode, remote
  jobs already dispatched keep running on GitHub and still post their result.
- **Start without fallback**: `scripts\run-local.cmd nohandoff` stops without
  starting Actions.
- **PC starts again**: it cancels the fallback poller and takes over. If a run
  cannot be cancelled within 3 minutes, the PC refuses to start so both
  processes do not poll at once. **This interrupts a job the fallback poller is
  processing.** Send `/status` in Telegram before starting the PC and wait for
  any active fallback job to finish. Split workers are unaffected.

The fallback run lasts at most **4 hours 45 minutes** and then exits. If the PC
will be off longer, start another run manually from the Actions tab.

## 6. Optional: start at logon

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1
```

This registers a Task Scheduler task named `MirrorBot` that runs
`scripts\run-local.cmd` when you log in. It restarts the bot up to three times
if it crashes. Start it immediately with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-startup-task.ps1 -Start
```

Remove it with:

```powershell
Unregister-ScheduledTask -TaskName MirrorBot -Confirm:$false
```

The task runs in your user session, so the PC must be powered on and logged in.
It does not bypass sleep; if the PC sleeps, the bot stops responding and
Windows may not run the handoff. Use `/handoff` before sleeping, or set the PC
to stay awake while plugged in.

## Updating

```powershell
git pull
.venv\Scripts\python.exe -m pip install -r requirements.txt
# restart run-local.cmd or the scheduled task
```

If `git pull` complains about `.state/telegram.json`, that file is tracked for
the Actions fallback. The PC uses `.state/local-telegram.json`, but an old run
may have modified the tracked file. Restore it with:

```powershell
git restore .state/telegram.json
git pull
```

## Troubleshooting

**`gh` is not authenticated** — run `gh auth login` and retry.

**`409 Conflict`** — another poller is active. Cancel it:

```powershell
gh run list --workflow bot.yml
gh run cancel <run-id>
```

Then start the PC bot again.

**The PC refuses to start because a run is still active** — GitHub may take a
minute to stop a cancelled job. Wait, or cancel it from the Actions page. Do
not start the PC bot while a run is still `in_progress`.

**Handoff did not start** — check the PC log for the `gh` error. Common causes:
not signed in, wrong repository (`ACTIONS_REPO`), or a workflow that is
disabled. You can start it manually from the Actions tab.

**A split job never starts** — `job.yml` must exist on the branch in
`ACTIONS_REF`. Push it, then check:

```powershell
gh workflow list
gh run list --workflow job.yml
```

**Could not start GitHub Actions** — the PC's `gh` login needs **Actions:
write**. Re-run `gh auth login` or update the token, then send the job again.

**The worker says it could not decrypt the payload** — the PC's
`REMOTE_JOB_KEY` and the repository secret of the same name do not match. Run
`scripts\configure-remote.ps1 -Rotate`, restart the PC bot, and start a new job.
Old workers that used the previous key cannot be recovered.

**The progress message stops updating** — check the failed `job.yml` run log.
If the message was deleted in Telegram, the worker sends the final card as a new
message. If `/cancel` does nothing, the run may already be finished.

**ffmpeg or aria2c not found** — open a new terminal after running the setup
script. The installer updates the user PATH. Split workers install their own
tools and are not affected.

**MEGA link fails** — `tools\megatools.exe` is installed. Newer megatools
builds use one executable with a `dl` subcommand; the bot detects this. Check
`tools\megatools.exe --help`.

**`-dump` fails** — check that `tools\payload-dumper-go.exe` exists. The bot
adds `tools\` to PATH when started through `run-local.cmd`.

**Bot stops when the PC sleeps** — use `/handoff` first, disable sleep while
plugged in, or accept the downtime. There is no automatic wake-up.

## Security

- `.env` contains the bot token, upload keys and `REMOTE_JOB_KEY`. Never commit
  it. It is in `.gitignore`.
- The same `REMOTE_JOB_KEY` is a GitHub Actions secret. Job payloads are
  encrypted, but anyone who can read that secret or your Actions logs may learn
  the source links and filenames. Keep the repository public only if that is
  acceptable.
- Use an owner-only `ALLOWED_USER_IDS`; a group member can spend your bandwidth
  and upload quota.
- Keep Windows, Python, yt-dlp, gallery-dl and the tools updated.
- The GitHub CLI token on the PC can start and cancel workflows in this
  repository. Treat it like a password.
