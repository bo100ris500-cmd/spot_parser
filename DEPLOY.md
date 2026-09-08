# Deploy & operations guide for Ubuntu VPS
# See also README.md

## 1. Create Telegram bot

1. Open Telegram → @BotFather → `/newbot`
2. Save `BOT_TOKEN`
3. Send any message to your bot
4. Get your chat id via @userinfobot (or temporary logging of `message.chat.id`)
5. Put values into `.env`:
   - `BOT_TOKEN=...`
   - `ALLOWED_CHAT_ID=...`

## 2. Server requirements

- Ubuntu 22.04 / 24.04
- 2 vCPU / 2–4 GB RAM / 40 GB SSD
- Docker + Compose plugin

## 3. Install Docker on Ubuntu

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2 git ufw
sudo usermod -aG docker $USER
# re-login after usermod
```

## 4. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw enable
# Do NOT publish Postgres/Redis ports publicly
```

## 5. Deploy application

```bash
git clone <YOUR_REPO_URL> ~/spot_parser
cd ~/spot_parser
cp .env.example .env
nano .env   # set BOT_TOKEN, ALLOWED_CHAT_ID, POSTGRES_PASSWORD

docker compose up -d --build
docker compose logs -f app
```

## 6. Verify

In Telegram (from allowed chat):

```
/start
/add BTC binance big cd
/list
/otchet BTC
```

## 7. Tuning without rebuild

Edit `config.yaml` on the host (mounted read-only into the container), then:

```bash
docker compose restart app
```

Key knobs: `big.k_multiplier`, `big.window_minutes`, `cd.alert_pct`, `cd.cooldown_sec`, RSI swings.

## 8. Backups

Nightly Postgres dump example:

```bash
mkdir -p ~/backups
crontab -e
# add:
0 3 * * * docker compose -f ~/spot_parser/docker-compose.yml exec -T postgres pg_dump -U spot spot_parser | gzip > ~/backups/spot_$(date +\%F).sql.gz
```

## 9. Updates

```bash
cd ~/spot_parser
git pull
docker compose up -d --build
```

## 10. Pilot calibration (first week)

1. Start with 5–10 liquid pairs (BTC, ETH, SOL, …) on 1–2 exchanges
2. Watch false/missed `big` alerts; adjust `k_multiplier` (10–20)
3. Watch `cd` spam; adjust `alert_pct` and `cooldown_sec`
4. Confirm RSI alerts arrive within ~1 minute after 1h/4h/1d candle close
