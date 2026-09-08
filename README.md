# Spot Parser

Автоматизированная система мониторинга спотовых сделок на Binance, Bybit, Bitget, OKX, HTX, KuCoin, MEXC, Gate, BingX с уведомлениями через приватный Telegram-бота.

## Возможности (v1)

- Флаг **big** — крупные сделки (адаптивный порог ×K или абсолютный `big:USD`)
- Флаг **cd** — кумулятивная дельта, алерты на резкий дисбаланс, отчёт
- **RSI-дивергенция** (classic) на 1h / 4h / 1d
- Команды `/add`, `/remove`, `/list`, `/otchet`
- PostgreSQL для агрегатов (без сырой ленты сделок)
- Redis pub/sub для алертов
- Docker Compose

## Быстрый старт (локально)

```bash
cp .env.example .env
# заполните BOT_TOKEN и ALLOWED_CHAT_ID

docker compose up -d --build
docker compose logs -f app
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/add BTC binance big cd` | Добавить пару |
| `/add SOL bybit big:50000` | Крупные сделки с абсолютным порогом $50k |
| `/remove BTC` | Удалить на всех биржах |
| `/remove BTC binance` | Удалить на одной бирже |
| `/list` | Список пар |
| `/otchet BTC` | Отчёт по таймфреймам |

Бот отвечает только `ALLOWED_CHAT_ID`.

## Конфигурация

- Секреты: `.env`
- Пороги/окна: `config.yaml` (можно менять без пересборки образа → `docker compose restart app`)

## Развёртывание на Ubuntu

См. [DEPLOY.md](DEPLOY.md).

## Стек

Python 3.11 · asyncio · aiogram 3 · ccxt.pro · PostgreSQL · Redis · Docker Compose

## Тесты

```bash
pip install -r requirements.txt
pytest -q
```
