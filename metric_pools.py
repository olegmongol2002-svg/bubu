#!/usr/bin/env python3
"""Парсер/монитор пулов app.metric.xyz.

Берёт ВСЕ пулы (включая неверифицированные / red-tier, которые сайт по
умолчанию прячет) из публичного API https://api.metric.xyz/public/v1,
фильтрует по минимальному TVL и сортирует по APR.

Примеры:
    python3 metric_pools.py --min-tvl 10000
    python3 metric_pools.py --min-tvl 50000 --sort apr7d --chain 1 --top 20
    python3 metric_pools.py --min-tvl 5000 --watch 300 --alert-apr 200
    python3 metric_pools.py --min-tvl 1000 --json > pools.json
    # Telegram: разовая проверка (для cron / GitHub Actions)
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
        python3 metric_pools.py --min-tvl 10000 --alert-apr 300 --telegram --state alerts.json

Только стандартная библиотека Python 3.8+.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API = "https://api.metric.xyz/public/v1/"
APP = "https://app.metric.xyz"
PAGE = 500

SORT_KEYS = {
    "apr24h": "apr24h",    # APR по комиссиям за последние 24ч (то, что показывает сайт)
    "apr7d": "apr7d",      # APR по комиссиям за 7 дней
    "pnl7d": "pnl7d",      # APR торгового PnL (markout) за 7 дней
    "net7d": "net7d",      # apr7d + pnl7d
    "tvl": "tvl",
    "vol24h": "vol24h",
}


def get(path, params=None, retries=3):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "metric-pools-monitor/1.0",
                                               "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise RuntimeError(f"GET {url}: {e}") from e
            time.sleep(2 ** attempt)


def bps(v):
    return None if v is None else v / 100.0


def fetch_pools(chain_filter=None):
    cfg = get("runtime-config")
    pools = []
    for ch in cfg.get("chains", []):
        if not ch.get("enabled"):
            continue
        cid = ch["chainId"]
        if chain_filter and cid not in chain_filter:
            continue
        for ver, vcfg in (ch.get("contracts", {}).get("pools") or {}).items():
            if vcfg and vcfg.get("isEnabled") is False:
                continue
            offset = 0
            while True:
                resp = get(f"evm/{cid}/metadata", {"contract_version": ver, "include24h": "true",
                                                   "offset": offset, "count": PAGE})
                data = resp.get("data") or []
                for p in data:
                    pools.append(normalize(p, cid, ch.get("name", str(cid))))
                nxt = resp.get("nextOffset")
                if nxt is None or not data:
                    break
                offset = nxt
    return pools


def normalize(p, chain_id, chain_name):
    a24 = p.get("analytics24h") or {}
    apr7 = bps(p.get("aprBps7d"))
    pnl7 = bps(p.get("tradingPnlAprBps7d"))
    cls = p.get("classificationSummary") or {}
    return {
        "chainId": chain_id,
        "chain": chain_name,
        "pair": p.get("pair") or f"{p.get('token0Symbol')}/{p.get('token1Symbol')}",
        "address": p.get("poolAddress"),
        "tvl": p.get("tvlFiat") or 0.0,
        "apr24h": bps(a24.get("aprBps")),
        "apr7d": apr7,
        "pnl7d": pnl7,
        "net7d": None if apr7 is None or pnl7 is None else apr7 + pnl7,
        "vol24h": (p.get("volumeByWindow") or {}).get("24h") or a24.get("volumeFiat") or 0.0,
        "fees24h": a24.get("feesFiat"),
        "tier": cls.get("tierQualification"),
        "reasons": ",".join(cls.get("reasonCodes") or []),
        "verified": bool(p.get("verifiedAdmin")),
        "curator": p.get("adminUsername") or "",
        "paused": (p.get("pauseLevel") or 0) > 0,
        "allowlist": bool(p.get("depositWhitelistingEnabled")),
        "status": p.get("priceProviderStatus"),
        "created": p.get("createdTimestamp"),
        "url": f"{APP}/pool/{chain_id}/{p.get('poolAddress')}",
    }


def select(pools, a):
    out = []
    for p in pools:
        if p["tvl"] < a.min_tvl:
            continue
        if a.max_tvl is not None and p["tvl"] > a.max_tvl:
            continue
        if a.verified_only and not p["verified"]:
            continue
        if a.tier and (p["tier"] or "") not in a.tier:
            continue
        if not a.include_paused and p["paused"]:
            continue
        if a.no_allowlist and p["allowlist"]:
            continue
        if a.token and not any(t.upper() in p["pair"].upper().split("/") for t in a.token):
            continue
        v = p[a.sort]
        if a.min_apr is not None and (p[a.sort] is None or v < a.min_apr):
            continue
        out.append(p)
    out.sort(key=lambda p: (p[a.sort] is not None, p[a.sort] or 0), reverse=True)
    return out[: a.top] if a.top else out


def money(v):
    if v is None:
        return "-"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suf}"
    return f"${v:.0f}"


def pct(v):
    return "-" if v is None else f"{v:,.1f}%"


def print_table(rows, sort_key):
    cols = [("#", 3), ("Chain", 9), ("Pair", 16), ("APR 24h", 10), ("APR 7d", 10),
            ("PnL 7d", 10), ("TVL", 9), ("Vol 24h", 9), ("Tier", 6), ("Ver", 3), ("URL", 0)]
    print("  ".join(n.ljust(w) if w else n for n, w in cols))
    for i, p in enumerate(rows, 1):
        vals = [str(i), p["chain"][:9], p["pair"][:16], pct(p["apr24h"]), pct(p["apr7d"]),
                pct(p["pnl7d"]), money(p["tvl"]), money(p["vol24h"]), (p["tier"] or "-"),
                "✓" if p["verified"] else "", p["url"]]
        print("  ".join(v.ljust(w) if w else v for v, (_, w) in zip(vals, cols)))
    print(f"\n{len(rows)} пулов, сортировка: {sort_key}")


def emit(rows, a):
    if a.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    elif a.csv:
        w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()) if rows else ["pair"])
        w.writeheader()
        w.writerows(rows)
    else:
        print_table(rows, a.sort)


# ---------------------------------------------------------------- alerts ---

def load_state(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, state):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def find_alerts(rows, a, state, now):
    """Пулы, которые сейчас выше порога и о которых не сообщали последние --cooldown часов.

    state = {"alerted": {address: unix_ts_последнего_уведомления}}.
    Если пул опустился ниже порога, запись удаляется: при новом пересечении придёт новое уведомление
    (но не раньше чем через --cooldown часов после предыдущего).
    """
    alerted = (state or {}).get("alerted", {})
    hot = [p for p in rows if p[a.sort] is not None and p[a.sort] >= a.alert_apr]
    hot_addr = {p["address"] for p in hot}
    cooldown = a.cooldown * 3600
    new = [p for p in hot if now - alerted.get(p["address"], 0) >= cooldown]
    # забываем пулы, которые ушли ниже порога и чей кулдаун истёк
    alerted = {k: v for k, v in alerted.items() if k in hot_addr or now - v < cooldown}
    for p in new:
        alerted[p["address"]] = now
    return new, {"alerted": alerted, "updated": now}


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_alert(p, sort_key):
    tier = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(p["tier"], "⚪")
    ver = "✅ " + html_escape(p["curator"] or "verified") if p["verified"] else "не verified"
    flags = []
    if p["allowlist"]:
        flags.append("allowlist")
    if p["status"] and p["status"] != "healthy":
        flags.append("оракул: " + p["status"])
    return (
        f'{tier} <b><a href="{p["url"]}">{html_escape(p["pair"])}</a></b> · {html_escape(p["chain"])}\n'
        f'APR 24ч <b>{pct(p["apr24h"])}</b> · 7д {pct(p["apr7d"])} · PnL 7д {pct(p["pnl7d"])}\n'
        f'TVL {money(p["tvl"])} · объём 24ч {money(p["vol24h"])} · {ver}'
        + (f'\n⚠️ {", ".join(flags)}' if flags else "")
    )


def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("нужны переменные окружения TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram ответил {e.code}: {e.read().decode(errors='replace')[:300]}") from None


def deliver(new, a):
    if not new:
        return
    head = (f"🚀 {len(new)} {'пул' if len(new) == 1 else 'пулов'} с {a.sort} ≥ {a.alert_apr:g}% "
            f"(TVL ≥ {money(a.min_tvl)})")
    blocks = [format_alert(p, a.sort) for p in new]
    if a.telegram:
        # Telegram ограничивает сообщение 4096 символами — режем на части
        msg = head
        for b in blocks:
            if len(msg) + len(b) + 2 > 3900:
                telegram_send(msg)
                msg = ""
            msg += ("\n\n" if msg else "") + b
        if msg:
            telegram_send(msg)
    if a.dry_run or not a.telegram:
        print(head, file=sys.stderr)
        for b in blocks:
            print("\n" + b, file=sys.stderr)


def run_alert_once(a):
    rows = select(fetch_pools(a.chain), a)
    now = int(time.time())
    new, state = find_alerts(rows, a, load_state(a.state), now)
    if a.dry_run:
        a.telegram = False
    deliver(new, a)
    save_state(a.state, state)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] пулов под фильтром: {len(rows)}, новых уведомлений: {len(new)}",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Монитор пулов app.metric.xyz по APR с фильтром TVL "
                                             "(включая неверифицированные пулы).")
    ap.add_argument("--min-tvl", type=float, default=0, help="минимальный TVL в USD (по умолч. 0)")
    ap.add_argument("--max-tvl", type=float, default=None, help="максимальный TVL в USD")
    ap.add_argument("--sort", choices=SORT_KEYS, default="apr24h", help="по чему сортировать (по умолч. apr24h)")
    ap.add_argument("--min-apr", type=float, default=None, help="минимальный APR в %% по полю --sort")
    ap.add_argument("--chain", type=int, action="append", help="chainId (1=Ethereum, 56=BSC, 4663=Robinhood); можно несколько")
    ap.add_argument("--token", action="append", help="только пары с этим токеном (напр. USDC); можно несколько")
    ap.add_argument("--tier", action="append", choices=["green", "yellow", "red"], help="фильтр по tier")
    ap.add_argument("--verified-only", action="store_true", help="только верифицированные кураторы")
    ap.add_argument("--include-paused", action="store_true", help="показывать пулы на паузе")
    ap.add_argument("--no-allowlist", action="store_true", help="скрыть пулы с allowlist на депозит")
    ap.add_argument("--top", type=int, default=30, help="сколько строк показать (0 = все)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="вывод в JSON")
    g.add_argument("--csv", action="store_true", help="вывод в CSV")
    ap.add_argument("--watch", type=int, metavar="SEC", help="режим мониторинга: обновлять каждые SEC секунд")
    ap.add_argument("--alert-apr", type=float, default=None,
                    help="сообщать о пулах, где APR (по полю --sort) >= этого значения")
    ap.add_argument("--telegram", action="store_true",
                    help="слать уведомления в Telegram (env TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID); "
                         "без --watch делает одну проверку и выходит")
    ap.add_argument("--state", default="alerts-state.json",
                    help="файл, где хранится, о каких пулах уже сообщали (по умолч. alerts-state.json)")
    ap.add_argument("--cooldown", type=float, default=24,
                    help="не повторять уведомление об одном пуле чаще, чем раз в N часов (по умолч. 24)")
    ap.add_argument("--dry-run", action="store_true", help="печатать уведомления вместо отправки")
    a = ap.parse_args()
    if a.top == 30 and (a.telegram or a.dry_run):
        a.top = 0  # для уведомлений смотрим все пулы, а не только первые 30
    if (a.telegram or a.dry_run) and a.alert_apr is None:
        ap.error("для уведомлений укажи --alert-apr")
    if a.telegram and not a.dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        ap.error("для --telegram задай переменные окружения TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")

    if not a.watch:
        if a.telegram or a.dry_run:
            run_alert_once(a)
        else:
            emit(select(fetch_pools(a.chain), a), a)
        return

    first = True
    while True:
        try:
            rows = select(fetch_pools(a.chain), a)
        except Exception as e:  # noqa: BLE001
            print(f"[{datetime.now():%H:%M:%S}] ошибка: {e}", file=sys.stderr)
            time.sleep(a.watch)
            continue
        if not (a.json or a.csv):
            print("\033[2J\033[H", end="")
            print(f"Обновлено {datetime.now():%Y-%m-%d %H:%M:%S}, min TVL {money(a.min_tvl)}\n")
        emit(rows, a)
        if a.alert_apr is not None:
            new, st = find_alerts(rows, a, load_state(a.state), int(time.time()))
            if first and not (a.telegram or a.dry_run):
                new = []  # в терминале не шумим о том, что уже было на экране при старте
            sent = True
            if new:
                print("\a", end="")
                try:
                    deliver(new, a)
                except Exception as e:  # noqa: BLE001
                    sent = False  # не запоминаем — попробуем отправить в следующий раз
                    print(f"ошибка отправки: {e}", file=sys.stderr)
            if sent:
                save_state(a.state, st)
        first = False
        sys.stdout.flush()
        time.sleep(a.watch)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
