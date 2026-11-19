# metric.xyz pools monitor

Показывает пулы app.metric.xyz с самым высоким APR, с фильтром по минимальному TVL.
В отличие от сайта, показывает **все** пулы, включая неверифицированные (red tier).

Данные берутся из публичного API `https://api.metric.xyz/public/v1/evm/{chainId}/metadata`. Нужен только Python 3.8+, без зависимостей.

```bash
python3 metric_pools.py --min-tvl 10000                      # топ-30 по APR 24h, TVL >= $10k
python3 metric_pools.py --min-tvl 50000 --sort apr7d --chain 1
python3 metric_pools.py --min-tvl 5000 --token USDC --tier red
python3 metric_pools.py --min-tvl 5000 --watch 300 --alert-apr 200   # мониторинг раз в 5 мин + алерт о новых пулах с APR >= 200%
python3 metric_pools.py --min-tvl 1000 --top 0 --csv > pools.csv
```

Сортировки (`--sort`): `apr24h` (APR от комиссий за 24ч, как на сайте), `apr7d`, `pnl7d` (APR торгового PnL/markout за 7д), `net7d` (apr7d + pnl7d), `tvl`, `vol24h`.

Прочие фильтры: `--max-tvl`, `--min-apr`, `--verified-only`, `--no-allowlist`, `--include-paused`. Все флаги: `python3 metric_pools.py -h`.

## Сайт

`docs/index.html` — та же логика в виде веб-страницы, данные грузятся прямо из браузера (API отдаёт CORS `*`).
Фильтры: мин. TVL, мин. APR, сеть, tier, токен, verified, allowlist; сортировка кликом по столбцу; автообновление и подсветка новых пулов с APR выше порога. Настройки запоминаются в браузере.

- Локально: просто открой `docs/index.html` в браузере (или `python3 -m http.server -d docs`).
- GitHub Pages: Settings → Pages → Deploy from a branch → ветка с этим кодом, папка `/docs`.

## Уведомления в Telegram

Скрипт шлёт в Telegram пулы, у которых APR выше порога. Об одном пуле напоминает не чаще раза в `--cooldown` часов (по умолчанию 24). Если APR пула упал ниже порога, а потом снова вырос, придёт новое уведомление.

### 1. Бот и chat id
1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получишь **токен** вида `123456:ABC...`.
2. Напиши своему боту любое сообщение (иначе он не сможет тебе писать).
3. Открой `https://api.telegram.org/bot<ТОКЕН>/getUpdates` и найди `"chat":{"id": ...}`. Это **chat id**. Для группы добавь бота в группу; id группы начинается с `-`.

### 2а. Автоматически через GitHub Actions (ничего не нужно держать включённым)
В репозитории: **Settings → Secrets and variables → Actions**
- вкладка *Secrets*: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- вкладка *Variables* (необязательно): `MIN_TVL` (10000), `ALERT_APR` (300), `SORT` (apr24h), `COOLDOWN_HOURS` (24), `EXTRA_ARGS` (например `--chain 1 --no-allowlist`)

Workflow `.github/workflows/telegram-alerts.yml` запускается каждые 15 минут (GitHub иногда запаздывает на несколько минут). Запустить вручную: вкладка **Actions → Telegram alerts → Run workflow**. Первый запуск пришлёт все пулы, которые уже выше порога, дальше будут приходить только новые.

### 2б. Локально
```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=123456789
python3 metric_pools.py --min-tvl 10000 --alert-apr 300 --telegram --watch 300   # проверка каждые 5 минут
python3 metric_pools.py --min-tvl 10000 --alert-apr 300 --dry-run                # посмотреть, что бы отправилось
```
