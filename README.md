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
