#!/usr/bin/env python3
"""Сканер спорных и ошибочно резолвящихся рынков Polymarket.

Что ищет (по всем рынкам, не только спорт):
  WRONG_PROPOSAL  рынок ещё не закрыт, исход предложен или в диспуте, а счёт матча говорит обратное
  CHEAP_WINNER    матч закончился, по счёту исход известен, а его можно купить дешевле $1
  DISPUTED        рынок с диспутами (история umaResolutionStatuses)
  LADDER          лестница линий противоречит сама себе (O/U 2.5 = No, а O/U 3.5 = Yes)
  CONTESTED       событие давно кончилось, рынок не закрыт, цена в середине и книга живая
  WRONG_RESOLVED  рынок уже закрыт, но не так, как следует из счёта (денег тут нет, это статистика)

Данные: публичный Gamma API Polymarket, счёт берётся из поля event.score.
Нужен только Python 3.8+, без зависимостей.

Примеры:
  python3 polymarket_disputes.py
  python3 polymarket_disputes.py --kinds WRONG_PROPOSAL,CHEAP_WINNER,DISPUTED
  python3 polymarket_disputes.py --telegram --state disputes-state.json
  python3 polymarket_disputes.py --slug atp-ono-lennonr-2026-11-22      # разобрать одно событие
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://gamma-api.polymarket.com"
UA = "polymarket-disputes-scanner/1.0"

KINDS = ["WRONG_PROPOSAL", "CHEAP_WINNER", "DISPUTED", "LADDER", "CONTESTED", "WRONG_RESOLVED"]
PRIORITY = {k: i for i, k in enumerate(KINDS)}


# ---------------------------------------------------------------- API

def get(path, params=None, retries=4):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url}: {err}")


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_events(closed, since, until, tag_id=None, max_events=50000):
    params = {"closed": str(closed).lower(), "limit": 500,
              "end_date_min": iso(since), "end_date_max": iso(until)}
    if tag_id:
        params["tag_id"] = tag_id
    out, cursor = [], None
    while len(out) < max_events:
        p = dict(params)
        if cursor:
            p["after_cursor"] = cursor
        d = get("/events/keyset", p)
        evs = d.get("events") or []
        out += evs
        cursor = d.get("next_cursor")
        if not cursor or not evs:
            break
    return out


def fetch_event_by_slug(slug):
    d = get("/events", {"slug": slug})
    return d[0] if d else None


# ---------------------------------------------------------------- helpers

def jl(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:  # noqa: BLE001
        return default


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_time(s):
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    s = re.sub(r"\+00(:00)*$", "+00:00", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            t = dt.datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dispute_count(m):
    return sum(1 for s in jl(m.get("umaResolutionStatuses"), []) if s == "disputed")


def market_state(m):
    """Что сейчас 'думает' рынок: (цена первого исхода, решён ли окончательно)."""
    prices = jl(m.get("outcomePrices"), [])
    p0 = fnum(prices[0]) if prices else None
    return p0, bool(m.get("closed"))


def ask_for(m, idx):
    """Лучшая цена покупки исхода idx (0 или 1)."""
    if idx == 0:
        return fnum(m.get("bestAsk"))
    bid = fnum(m.get("bestBid"))
    return None if bid is None or bid <= 0 else round(1 - bid, 4)


# ---------------------------------------------------------------- score parsing

TENNIS = {"atp", "wta", "itf", "atp-doubles", "wta-doubles", "daviscup", "bjk", "utr", "challenger"}


def parse_score(e):
    """Возвращает dict со счётом завершённого события или None.

    kind='points': {'a','b'}          (баскетбол, хоккей, футбол, амер. футбол, крикет...)
    kind='series': {'a','b','bo'}     (киберспорт, BO3/BO5)
    kind='tennis': {'sets': [(x,y),...], 'complete': bool, 'a','b'}
    Первое число всегда относится к outcomes[0] рынка moneyline (проверено на закрытых рынках).
    """
    s = (e.get("score") or "").strip()
    if not s or not e.get("ended"):
        return None
    sport = ((e.get("sport") or {}).get("sport") or "").lower()
    if "|" in s:
        parts = s.split("|")
        m = re.match(r"^(\d+)-(\d+)$", parts[1].strip()) if len(parts) > 1 else None
        if not m:
            return None
        bo = re.search(r"(\d+)", parts[2]) if len(parts) > 2 else None
        if m.group(1) == m.group(2) == "0":
            return None  # матч не сыгран / отменён
        return {"kind": "series", "a": int(m.group(1)), "b": int(m.group(2)),
                "bo": int(bo.group(1)) if bo else None}
    is_tennis = sport in TENNIS or "," in s or "(" in s
    if is_tennis:
        sets = []
        for chunk in s.split(","):
            m = re.match(r"^\s*(\d+)-(\d+)", chunk)
            if not m:
                return None
            sets.append((int(m.group(1)), int(m.group(2))))
        if not sets or sets == [(0, 0)]:
            return None

        def done(x, y):
            hi, lo = max(x, y), min(x, y)
            return (hi >= 6 and hi - lo >= 2) or (hi == 7 and lo == 6) or (hi >= 10 and hi - lo >= 2)

        complete_flags = [done(x, y) for x, y in sets]
        a = sum(1 for (x, y), c in zip(sets, complete_flags) if c and x > y)
        b = sum(1 for (x, y), c in zip(sets, complete_flags) if c and y > x)
        need = 3 if max(a, b) >= 3 or len(sets) > 3 else 2
        complete = all(complete_flags) and max(a, b) >= need
        return {"kind": "tennis", "sets": sets, "set_done": complete_flags,
                "complete": complete, "a": a, "b": b}
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if not m:
        return None
    return {"kind": "points", "a": int(m.group(1)), "b": int(m.group(2))}


def team_names(e):
    """[team_a, team_b] в порядке счёта."""
    for m in e.get("markets") or []:
        if m.get("sportsMarketType") == "moneyline":
            o = jl(m.get("outcomes"), [])
            if len(o) == 2 and o != ["Yes", "No"]:
                return o
            q = m.get("question") or ""
            mm = re.match(r"Will (.+?) vs\.? (.+?) end in a draw", q)
            if mm:
                return [mm.group(1), mm.group(2)]
    title = re.sub(r"^[^:]*:\s*", "", e.get("title") or "")
    title = re.sub(r"\s*\(.*?\)\s*$", "", title)
    parts = re.split(r"\s+vs\.?\s+", title)
    return parts if len(parts) == 2 else None


def side_of(name, teams):
    """Индекс команды (0/1) по имени или аббревиатуре."""
    if not teams or not name:
        return None
    n = norm(name)
    for i, t in enumerate(teams):
        if norm(t) == n:
            return i
    for i, t in enumerate(teams):
        if n and (n in norm(t) or norm(t) in n):
            return i
    # аббревиатура (LLH, PRE): совпадение по первым буквам
    for i, t in enumerate(teams):
        if norm(t).replace(" ", "").startswith(n.replace(" ", "")):
            return i
    return None


LINE_RE = re.compile(r"\(([+-]?\d+(?:\.\d+)?)\)")
OU_RE = re.compile(r"O/U\s*([\d.]+)|Over/Under\s*([\d.]+)", re.I)


def ou_line(text):
    m = OU_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1) or m.group(2))


def over_under(total, line, outcomes):
    """Индекс победившего исхода для O/U рынка."""
    if total == line:
        return 0.5
    over = total > line
    o = [x.lower().split()[0] if x.strip() else "" for x in outcomes]  # "Over 2.5" -> "over"
    if "over" in o and "under" in o:
        return o.index("over") if over else o.index("under")
    if o[:2] == ["yes", "no"]:
        return 0 if over else 1
    return None


def expected_outcome(m, e, sc, teams):
    """Ожидаемый исход по счёту: 0, 1, 0.5 (50-50) или None (не умеем/нет данных)."""
    if not sc:
        return None
    t = m.get("sportsMarketType") or ""
    q = m.get("question") or ""
    g = m.get("groupItemTitle") or ""
    outcomes = jl(m.get("outcomes"), [])
    if len(outcomes) != 2:
        return None
    yn = [x.lower() for x in outcomes] == ["yes", "no"]

    # ---- теннис
    if sc["kind"] == "tennis":
        sets = sc["sets"]
        if t == "moneyline":
            if not sc["complete"]:
                return None  # отказ: победитель тот, кто прошёл дальше, по счёту не видно
            return 0 if sc["a"] > sc["b"] else 1
        if t in ("tennis_first_set_winner", "tennis_set_winner"):
            mm = re.search(r"Set\s*(\d)", q) if t == "tennis_set_winner" else None
            n = int(mm.group(1)) if mm else 1
            if n > len(sets) or not sc["set_done"][n - 1]:
                return 0.5 if sc["complete"] else None
            x, y = sets[n - 1]
            return 0 if x > y else 1
        if not sc["complete"]:
            return None
        if t == "tennis_set_totals":
            line = ou_line(q)
            return None if line is None else over_under(len(sets), line, outcomes)
        if t == "tennis_match_totals":
            line = ou_line(q)
            games = sum(x + y for x, y in sets)
            return None if line is None else over_under(games, line, outcomes)
        if t == "tennis_set_handicap":
            mm = re.search(r"Set Handicap:\s*(.+?)\s*\(([+-]?[\d.]+)\)", q)
            if not mm:
                return None
            fav = side_of(mm.group(1), teams)
            if fav is None:
                return None
            line = float(mm.group(2))
            diff = (sc["a"] - sc["b"]) * (1 if fav == 0 else -1)
            o0 = side_of(outcomes[0], teams)
            fav_covers = diff + line > 0
            return 0 if (o0 == fav) == fav_covers else 1
        return None

    a, b = sc["a"], sc["b"]

    # ---- киберспорт (серия)
    if sc["kind"] == "series":
        if t == "moneyline":
            return None if a == b else (0 if a > b else 1)
        if t == "totals" and "game" in (q + g).lower():
            line = ou_line(q) or ou_line(g)
            return None if line is None else over_under(a + b, line, outcomes)
        if t == "map_handicap":
            mm = re.search(r"Handicap:\s*(.+?)\s*\(([+-]?[\d.]+)\)", q)
            o0 = side_of(outcomes[0], teams)
            if not mm or o0 is None:
                return None
            line = float(mm.group(2))
            diff = (a - b) * (1 if o0 == 0 else -1)
            return 0 if diff + line > 0 else 1
        return None

    # ---- очки/голы
    if t == "moneyline":
        if not yn:
            return None if a == b else (0 if a > b else 1)
        if "draw" in q.lower():
            return 0 if a == b else 1
        mm = re.match(r"Will (.+?) win", q)
        i = side_of(mm.group(1), teams) if mm else None
        if i is None:
            return None
        won = (a > b) if i == 0 else (b > a)
        return 0 if won else 1
    if t in ("totals",) and not re.search(r"half|quarter|q\d|period|inning|map|set", q, re.I):
        line = ou_line(q) or ou_line(g)
        return None if line is None else over_under(a + b, line, outcomes)
    if t == "spreads" and not re.search(r"half|quarter|q\d", q + g, re.I):
        mm = re.search(r"Spread:\s*(.+?)\s*\(([+-]?[\d.]+)\)", q)
        if not mm:
            return None
        i = side_of(mm.group(1), teams)
        o0 = side_of(outcomes[0], teams)
        if i is None or o0 is None:
            return None
        line = float(mm.group(2))
        diff = (a - b) * (1 if i == 0 else -1)
        if diff + line == 0:
            return 0.5
        covers = diff + line > 0
        return 0 if (o0 == i) == covers else 1
    if t in ("team_totals", "soccer_team_totals") and not re.search(r"half|quarter|q\d", q, re.I):
        line = ou_line(q) or ou_line(g)
        name = re.sub(r"\s*O/U.*$", "", g).strip() or None
        i = side_of(name, teams)
        if line is None or i is None:
            return None
        return over_under(a if i == 0 else b, line, outcomes)
    if t == "both_teams_to_score":
        return 0 if (a > 0 and b > 0) else 1
    if t == "soccer_exact_score":
        mm = re.search(r"Exact Score:\s*(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+?)\?", q)
        if not mm:
            return None
        i = side_of(mm.group(1), teams)
        if i is None:
            return None
        x, y = int(mm.group(2)), int(mm.group(3))
        if i == 1:
            x, y = y, x
        return 0 if (a, b) == (x, y) else 1
    return None


# ---------------------------------------------------------------- ladders

THRESH_RE = re.compile(
    r"(?:O/U|Over/Under|above|over|more than|greater than|at least|≥|>)\s*\$?([\d][\d,]*(?:\.\d+)?)\s*([kKmMbB])?",
    re.I)
MULT = {"k": 1e3, "m": 1e6, "b": 1e9}


def ladder_key(m):
    """(группа, линия) для рынков вида 'X: Stat O/U 3.5' / 'Bitcoin above $100k'."""
    q = m.get("question") or ""
    g = m.get("groupItemTitle") or ""
    text = g if THRESH_RE.search(g) else q
    mm = THRESH_RE.search(text)
    if not mm:
        return None
    line = float(mm.group(1).replace(",", "")) * MULT.get((mm.group(2) or "").lower(), 1)
    group = norm(THRESH_RE.sub("#", q))
    outcomes = [x.lower() for x in jl(m.get("outcomes"), [])]
    if outcomes not in (["yes", "no"], ["over", "under"]):
        return None
    return group, line


def decided(m):
    """1 = 'больше' (Yes/Over), 0 = 'меньше', None = не решено."""
    p0, closed = market_state(m)
    if p0 is None:
        return None
    hi, lo = (0.99, 0.01) if closed else (0.97, 0.03)
    if p0 >= hi:
        return 1
    if p0 <= lo:
        return 0
    return None


def ladder_violations(e):
    groups = {}
    for m in e.get("markets") or []:
        k = ladder_key(m)
        if k:
            groups.setdefault(k[0], []).append((k[1], m))
    out = []
    for _, items in groups.items():
        items.sort(key=lambda x: x[0])
        dec = [(line, m, decided(m)) for line, m in items]
        dec = [x for x in dec if x[2] is not None]
        # 'больше X' должно быть невозрастающим по X: Yes на большей линии при No на меньшей = противоречие
        for i in range(len(dec)):
            for j in range(i + 1, len(dec)):
                lo_line, lo_m, lo_v = dec[i]
                hi_line, hi_m, hi_v = dec[j]
                if lo_v == 0 and hi_v == 1:
                    out.append((lo_m, hi_m, lo_line, hi_line))
    return out


# ---------------------------------------------------------------- analysis

def event_url(e):
    return f"https://polymarket.com/event/{e.get('slug')}"


def finding(kind, e, m, msg, edge=None, **extra):
    p0, _ = market_state(m)
    d = {"kind": kind, "event": e.get("title"), "market": m.get("question"), "market_id": m.get("id"),
         "url": event_url(e), "price": p0, "liquidity": fnum(m.get("liquidity")),
         "disputes": dispute_count(m), "status": m.get("umaResolutionStatus"),
         "score": e.get("score"), "msg": msg, "edge": edge}
    d.update(extra)
    return d


def analyze_event(e, a, now):
    out = []
    markets = e.get("markets") or []
    sc = parse_score(e)
    teams = team_names(e) if sc else None
    finished = parse_time(e.get("finishedTimestamp"))
    end = parse_time(e.get("endDate"))
    ended_at = finished or (end if (end and end < now) else None)
    hours_since = (now - ended_at).total_seconds() / 3600 if ended_at else None
    if e.get("ended") and hours_since is None:
        hours_since = 0
    contested_seen = {}

    for m in markets:
        outcomes = jl(m.get("outcomes"), [])
        p0, closed = market_state(m)
        if p0 is None or len(outcomes) != 2:
            continue
        status = m.get("umaResolutionStatus")
        disputes = dispute_count(m)
        liq = fnum(m.get("liquidity")) or 0
        exp = expected_outcome(m, e, sc, teams)
        exp_name = (outcomes[exp] if exp in (0, 1) else "50-50") if exp is not None else None

        if closed:
            if exp is not None and a.closed:
                got = 0.5 if 0.4 < p0 < 0.6 else (0 if p0 >= 0.99 else 1 if p0 <= 0.01 else None)
                if got is not None and got != exp:
                    got_name = "50-50" if got == 0.5 else outcomes[got]
                    out.append(finding("WRONG_RESOLVED", e, m,
                                       f"закрыт как {got_name}, по счёту {e.get('score')} должно быть {exp_name}"))
            continue

        # ---- открытые рынки
        if exp in (0, 1):
            want = p0 if exp == 0 else 1 - p0
            if status in ("proposed", "disputed") and want < 0.5:
                out.append(finding("WRONG_PROPOSAL", e, m,
                                   f"статус {status}, цена против счёта: по {e.get('score')} должно быть {exp_name}, "
                                   f"рынок даёт {exp_name} {want:.2f}", edge=round(1 - want, 3)))
            ask = ask_for(m, exp)
            if ask is not None and ask <= a.max_ask and m.get("acceptingOrders") is not False \
                    and (hours_since or 0) >= a.min_hours_cheap:
                out.append(finding("CHEAP_WINNER", e, m,
                                   f"по счёту {e.get('score')} выигрывает {exp_name}, купить можно по {ask:.3f}",
                                   edge=round(1 - ask, 3), ask=ask))
        if disputes >= 1:
            out.append(finding("DISPUTED", e, m,
                               f"{disputes} диспут(ов), статус {status}, цена {outcomes[0]} {p0:.3f}"
                               + (f"; по счёту {exp_name}" if exp_name else "")))
        elif (hours_since is not None and hours_since >= a.stale_hours and 0.1 <= p0 <= 0.9
              and abs(p0 - 0.5) > 0.02  # ровно ~0.50 = обычно ждёт 50-50 (перенос/отмена)
              and liq >= a.min_liq and (fnum(m.get("spread")) or 1) <= 0.1 and exp is None
              and (fnum(m.get("volume24hr")) or 0) >= a.min_vol24
              and not contested_seen.get(e.get("id"))):
            contested_seen[e.get("id")] = True  # одна находка на событие, иначе 30 линий одного матча
            out.append(finding("CONTESTED", e, m,
                               f"кончилось {hours_since:.0f}ч назад, не закрыт, цена {outcomes[0]} {p0:.3f}, "
                               f"статус {status or '-'}"))

    for lo_m, hi_m, lo_line, hi_line in ladder_violations(e):
        is_open = not (lo_m.get("closed") and hi_m.get("closed"))
        if not is_open and not a.closed:
            continue
        out.append(finding("LADDER", e, hi_m,
                           f"линия {lo_line:g} = меньше, а линия {hi_line:g} = больше: "
                           f"«{lo_m.get('question')}» vs «{hi_m.get('question')}»"
                           + ("" if is_open else " (оба закрыты)"),
                           pair=[lo_m.get("id"), hi_m.get("id")], open=is_open))
    return out


def dedupe(findings):
    seen, out = set(), []
    for f in findings:
        k = (f["kind"], f["market_id"], tuple(f.get("pair") or ()))
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def scan(a):
    now = dt.datetime.now(dt.timezone.utc)
    events = []
    if a.slug:
        for s in a.slug:
            e = fetch_event_by_slug(s)
            if e:
                events.append(e)
            else:
                print(f"событие {s} не найдено", file=sys.stderr)
    else:
        since = now - dt.timedelta(days=a.days)
        events += fetch_events(False, since, now)
        if a.closed:
            events += fetch_events(True, now - dt.timedelta(hours=a.closed_hours), now, tag_id=1)
    findings = []
    for e in events:
        findings += analyze_event(e, a, now)
    findings = dedupe(findings)
    if a.kinds:
        findings = [f for f in findings if f["kind"] in a.kinds]
    findings.sort(key=lambda f: (PRIORITY[f["kind"]], -(f.get("edge") or 0), -(f.get("liquidity") or 0)))
    return events, findings


# ---------------------------------------------------------------- output

def print_findings(findings, n_events):
    print(f"проверено событий: {n_events}, находок: {len(findings)}\n")
    cur = None
    for f in findings:
        if f["kind"] != cur:
            cur = f["kind"]
            print(f"== {cur} ==")
        liq = f"liq ${f['liquidity']:,.0f}" if f.get("liquidity") else ""
        print(f"- {f['market']}  [{f['event']}]  {liq}")
        print(f"    {f['msg']}")
        print(f"    {f['url']}")
    if not findings:
        print("ничего не найдено")


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_alert(f):
    edge = f" · эдж {f['edge'] * 100:.1f}¢" if f.get("edge") else ""
    return (f"<b>{f['kind']}</b>{edge}\n{html_escape(f['market'])}\n<i>{html_escape(f['event'])}</i>\n"
            f"{html_escape(f['msg'])}\n{f['url']}")


def telegram_send(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"alerted": {}}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def alert_key(f):
    # DISPUTED: новое уведомление при каждом новом диспуте
    extra = f"#{f['disputes']}" if f["kind"] == "DISPUTED" else ""
    return f"{f['kind']}:{f['market_id']}:{','.join(map(str, f.get('pair') or []))}{extra}"


def run_alerts(findings, a):
    state = load_state(a.state)
    alerted = state.setdefault("alerted", {})
    now = int(time.time())
    new = [f for f in findings if f["kind"] in a.alert_kinds and alert_key(f) not in alerted]
    for f in new:
        msg = format_alert(f)
        if a.dry_run or not a.telegram:
            print(msg + "\n")
        else:
            telegram_send(msg)
            time.sleep(0.5)
        alerted[alert_key(f)] = now
    # чистим старое (30 дней)
    state["alerted"] = {k: v for k, v in alerted.items() if now - v < 30 * 86400}
    if not a.dry_run:
        save_state(a.state, state)
    print(f"новых уведомлений: {len(new)}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Сканер спорных рынков Polymarket")
    ap.add_argument("--days", type=float, default=7, help="открытые события с датой окончания за N дней (7)")
    ap.add_argument("--closed-hours", type=float, default=48,
                    help="закрытые спортивные события за N часов, для WRONG_RESOLVED/LADDER (48)")
    ap.add_argument("--no-closed", dest="closed", action="store_false", help="не проверять закрытые рынки")
    ap.add_argument("--kinds", type=lambda s: [x.strip().upper() for x in s.split(",") if x.strip()],
                    help="какие типы показывать, через запятую: " + ",".join(KINDS))
    ap.add_argument("--max-ask", type=float, default=0.97, help="CHEAP_WINNER: максимальная цена покупки (0.97)")
    ap.add_argument("--min-hours-cheap", type=float, default=0.25,
                    help="CHEAP_WINNER: сколько часов должно пройти после конца матча (0.25)")
    ap.add_argument("--stale-hours", type=float, default=6, help="CONTESTED: часов после окончания (6)")
    ap.add_argument("--min-liq", type=float, default=500, help="CONTESTED: минимальная ликвидность $ (500)")
    ap.add_argument("--min-vol24", type=float, default=100,
                    help="CONTESTED: минимальный объём торгов за 24ч $ (100); отсекает мёртвые рынки по 0.50")
    ap.add_argument("--slug", action="append", help="проверить только это событие (можно несколько раз)")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--telegram", action="store_true", help="слать новые находки в Telegram")
    ap.add_argument("--dry-run", action="store_true", help="показать, что ушло бы в Telegram, не отправляя")
    ap.add_argument("--alert-kinds", type=lambda s: [x.strip().upper() for x in s.split(",") if x.strip()],
                    default=["WRONG_PROPOSAL", "CHEAP_WINNER", "DISPUTED", "LADDER"],
                    help="типы для уведомлений (по умолч. WRONG_PROPOSAL,CHEAP_WINNER,DISPUTED,LADDER)")
    ap.add_argument("--state", default="disputes-state.json", help="файл с уже отправленными уведомлениями")
    a = ap.parse_args()
    for k in (a.kinds or []) + a.alert_kinds:
        if k not in KINDS:
            ap.error(f"неизвестный тип {k}; доступны: {', '.join(KINDS)}")
    if a.telegram and not a.dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        ap.error("для --telegram задай TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")

    events, findings = scan(a)
    if a.telegram or a.dry_run:
        run_alerts(findings, a)
        return
    if a.json:
        json.dump(findings, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print_findings(findings, len(events))


if __name__ == "__main__":
    main()
