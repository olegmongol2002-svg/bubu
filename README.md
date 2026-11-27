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

---

# Polymarket: сканер спорных рынков

`polymarket_disputes.py` ищет по **всем** рынкам Polymarket те, где результат спорный или резолв расходится с фактом. Данные из публичного Gamma API, счёт матча из поля `score` самого Polymarket. Python 3.8+, без зависимостей.

```bash
python3 polymarket_disputes.py                                        # всё найденное
python3 polymarket_disputes.py --kinds WRONG_PROPOSAL,CHEAP_WINNER   # только то, где есть деньги
python3 polymarket_disputes.py --slug atp-ono-lennonr-2026-11-22     # разобрать одно событие
python3 polymarket_disputes.py --json > findings.json
```

| Тип | Что значит | Что делать |
|---|---|---|
| `WRONG_PROPOSAL` | рынок не закрыт, исход предложен/в диспуте, а цена против счёта | диспутить (2 часа после пропоуза) или купить правильный исход |
| `CHEAP_WINNER` | матч кончился, по счёту исход известен, а купить можно дешевле `--max-ask` (0.97) | купить и ждать резолва |
| `DISPUTED` | у рынка есть диспуты | читать правила и решать, как проголосует UMA |
| `LADDER` | лестница линий противоречит сама себе (2.5 = No, а 3.5 = Yes) | одна из линий резолвнута неверно |
| `CONTESTED` | событие давно кончилось, рынок не закрыт, цена в середине, книга живая | проверить руками |
| `WRONG_RESOLVED` | рынок уже закрыт не так, как следует из счёта | денег нет, статистика: где ошибаются чаще |

По счёту проверяются: победитель, тоталы, форы, индивидуальные тоталы, обе забьют, точный счёт (футбол, баскетбол, хоккей, NFL и др.); в теннисе — победитель, сеты, тотал сетов/геймов, фора по сетам; в киберспорте — победитель серии, тотал карт, фора по картам. Статистика игроков (передачи, подборы) счётом не проверяется — только через `LADDER` и `DISPUTED`.

Ограничения: если матч закончился отказом, теннисные тоталы не оцениваются (правила разные). Счёт берётся у Polymarket — если он сам ошибся, сканер тоже.

Опции: `--days` (открытые события за N дней, 7), `--closed-hours` (закрытые спортивные за N часов, 48), `--no-closed`, `--max-ask`, `--stale-hours`, `--min-liq`, `--min-vol24`. Все флаги: `python3 polymarket_disputes.py -h`.

### Telegram
Те же секреты `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Workflow `.github/workflows/polymarket-disputes.yml` запускается каждые 15 минут и шлёт только новые находки (о новом диспуте на том же рынке сообщит снова). Типы для уведомлений — переменная `PM_ALERT_KINDS`, доп. флаги — `PM_EXTRA_ARGS`.

```bash
python3 polymarket_disputes.py --dry-run              # что ушло бы в Telegram
python3 polymarket_disputes.py --telegram             # отправить
```
