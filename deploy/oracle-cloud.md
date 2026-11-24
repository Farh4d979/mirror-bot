# Deploy the bot on Oracle Cloud Always Free

This guide runs the bot 24/7 on Oracle's free ARM VM. GitHub Actions is only
needed for tests after this migration.

## What Oracle gives you

As of 2026, the Always Free compute allowance is approximately:

- 2 Ampere A1 ARM OCPUs
- 12 GB RAM
- 200 GB total block storage
- 10 TB outbound traffic per month

Older guides mention 4 OCPU / 24 GB; Oracle reduced the allowance in 2026.
Check the Always Free limits shown in your own tenancy.

A credit or debit card is required for verification. You are not charged as
long as you stay inside the Always Free limits. Do not create paid shapes or
extra block volumes.

> Oracle may stop Always Free instances that look idle over a 7-day period
> (very low CPU and network use). The bot is mostly idle, so this can happen.
> If it does, start the instance again from the Oracle console. Docker is
> enabled on boot and the container has `restart: unless-stopped`, so the bot
> comes back automatically.

## 1. Create the VM

1. Sign in to the Oracle Cloud console.
2. Go to **Compute → Instances → Create instance**.
3. Name it `mirror-bot`.
4. Image: **Canonical Ubuntu 24.04** (aarch64 / ARM).
5. Shape: **Ampere → VM.Standard.A1.Flex**.
   - OCPUs: `2`
   - Memory: `12 GB`
6. Networking: accept the default VCN with a public subnet and assign a
   **public IPv4 address**.
7. SSH keys: upload your public key. Save the private key.
8. Boot volume: set a custom size of **100–200 GB**. The full 200 GB is free,
   but 100 GB is enough for normal use.
9. Create the instance.

If you see **Out of host capacity**, try another availability domain or retry
later. This is an Oracle capacity issue, not a configuration error.

## 2. Connect over SSH

```bash
ssh -i ~/.ssh/your-oracle-key ubuntu@YOUR_PUBLIC_IP
```

## 3. Install Docker and Git

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker "$USER"
newgrp docker
```

Verify Docker:

```bash
docker run --rm hello-world
```

If `docker` still says permission denied, log out and back in.

## 4. Get the bot and configure it

```bash
git clone https://github.com/Farh4d979/mirror-bot.git
cd mirror-bot
cp .env.example .env
chmod 600 .env
nano .env
```

At minimum, set:

```dotenv
TELEGRAM_BOT_TOKEN=your-token
ALLOWED_USER_IDS=your-telegram-user-id

# Optional but recommended
GOFILE_TOKEN=your-gofile-token
PIXELDRAIN_API_KEY=your-pixeldrain-key

MAX_CONCURRENT_JOBS=1
JOB_TIMEOUT=7200
MAX_FILE_SIZE=0
MULTI_FILE_MODE=largest
BOT_RUN_SECONDS=0
```

`BOT_RUN_SECONDS=0` means run forever. The GitHub run duration is not used.

Do **not** leave `ALLOWED_USER_IDS` empty. Empty means anyone can use the bot.

`MAX_FILE_SIZE=0` means no bot-side limit; the 200 GB disk is the real limit.
If you chose a smaller boot volume, set a sensible limit.

## 5. Stop GitHub Actions, then start the bot

Telegram allows only one `getUpdates` consumer per bot token. Do **not** run the
GitHub bot and the Oracle bot at the same time, or one of them will get
`409 Conflict`.

Stop the GitHub fallback first. From your own computer:

```bash
gh run list -R Farh4d979/mirror-bot --workflow bot.yml --json databaseId,status
gh run cancel <active-run-id> -R Farh4d979/mirror-bot
```

You can also cancel the active run from the GitHub Actions page. The workflow
has no schedule and does not restart itself, so once the active run is gone it
stays idle.

Then, on the Oracle VM:

```bash
cd ~/mirror-bot
docker compose up -d --build
docker compose logs -f
```

Wait for a line containing `Bot polling started`. Press `Ctrl+C` to stop
following logs; the container keeps running.

Check it:

```bash
docker compose ps
```

Send `/start` to the bot in Telegram. The container uses long polling, so you
do not need to open any inbound ports or configure a domain.

## 6. Updates and maintenance

Update the bot:

```bash
cd ~/mirror-bot
git pull
docker compose up -d --build
docker image prune -f
```

View logs:

```bash
docker compose logs --tail=200
```

Restart:

```bash
docker compose restart
```

Back up `.env` and the Docker volume that contains state and downloads. Find
the volume name first:

```bash
docker volume ls
```

Then back it up (replace the volume name if yours differs):

```bash
docker run --rm \
  -v mirror-bot_mirror-data:/data \
  -v "$PWD":/backup \
  alpine tar czf /backup/mirror-data.tgz -C /data .
```

## 7. Remove the GitHub Actions runtime

Do this **only after** the Oracle bot replies in Telegram.

```bash
git rm .github/workflows/bot.yml
git commit -m "chore: move bot runtime to Oracle Cloud"
git push
```

Keep `.github/workflows/tests.yml`; it only runs tests and is normal GitHub
Actions usage.

## 8. Oracle-specific notes

- **Idle reclamation:** if Oracle stops the VM, restart it from the console.
  The container starts automatically because Docker is enabled at boot.
- **Traffic:** 10 TB/month outbound is included. Very heavy mirroring can
  exceed it.
- **Billing:** stay on Always Free resources. A card on file does not mean you
  are charged; creating paid shapes or extra volumes can charge you.
- **Security:** keep the SSH key private. The Oracle security list only needs
  SSH (port 22), preferably limited to your IP. The bot itself needs no open
  ports.
- **Cloudflare:** not required for long polling. If you later switch to
  webhooks, Cloudflare Tunnel can expose the local port without opening it in
  Oracle's firewall.
