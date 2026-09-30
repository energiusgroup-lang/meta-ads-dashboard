#!/usr/bin/env python3
"""
Meta Ads Control Center — extração direta via Meta Graph API (Marketing API)
e geração do dashboard HTML estático (para publicação via GitHub Pages).

Substitui o pipeline anterior baseado em Windsor.ai. Reimplementa, ponto a
ponto, a mesma metodologia usada nas execuções anteriores do trigger agendado
(fuso BRT, janelas de data, regra de criticidade de saldo, ranking de
criativos), mas buscando os dados direto na Graph API em vez do Windsor.

Variáveis de ambiente esperadas:
  FB_ACCESS_TOKEN  -> token do System User com escopo ads_read nas 3 contas

Saída:
  site/index.html  -> dashboard pronto para o GitHub Pages
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

ACCOUNTS = {
    "Digiê": "act_233993616381680",
    "Energius [Whats]": "act_1563322824472198",
    "Energius [Forms]": "act_1418722915653884",
}

DATA_START = "2026-01-01"
BRT = ZoneInfo("America/Sao_Paulo")

TOKEN = os.environ.get("FB_ACCESS_TOKEN")
if not TOKEN:
    print("ERRO: variável de ambiente FB_ACCESS_TOKEN não definida.", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "template.html")
OUT_DIR = os.path.join(SCRIPT_DIR, "site")
OUT_PATH = os.path.join(OUT_DIR, "index.html")

MESSAGING_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"


# --------------------------------------------------------------------------
# Graph API helpers
# --------------------------------------------------------------------------

def graph_get(path, params, max_retries=5):
    """GET na Graph API com paginação e retry simples para rate limit/erros transitórios."""
    url = f"{GRAPH_BASE}/{path}"
    params = dict(params)
    params["access_token"] = TOKEN
    out = []
    attempt = 0
    while url:
        resp = requests.get(url, params=params if "?" not in url else None, timeout=60)
        if resp.status_code == 429 or (500 <= resp.status_code < 600):
            attempt += 1
            if attempt > max_retries:
                resp.raise_for_status()
            sleep_s = min(60, 2 ** attempt)
            print(f"  [retry] {resp.status_code} em {path}, aguardando {sleep_s}s...")
            time.sleep(sleep_s)
            continue
        if resp.status_code != 200:
            print(f"ERRO Graph API ({resp.status_code}) em {path}: {resp.text[:800]}", file=sys.stderr)
            resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        out.extend(data)
        paging = body.get("paging", {})
        next_url = paging.get("next")
        if next_url:
            url = next_url
            params = None
            attempt = 0
        else:
            url = None
    return out


def action_value(actions, matcher):
    """Soma o valor de todas as entradas de 'actions' cujo action_type casa com matcher(action_type)==True."""
    if not actions:
        return 0.0
    total = 0.0
    for a in actions:
        at = a.get("action_type", "")
        if matcher(at):
            try:
                total += float(a.get("value", 0) or 0)
            except (TypeError, ValueError):
                pass
    return total


def is_lead_action(action_type):
    return "lead" in action_type.lower()


def is_messaging_action(action_type):
    return action_type == MESSAGING_ACTION_TYPE


# --------------------------------------------------------------------------
# Fuso horário / datas
# --------------------------------------------------------------------------

def now_brt():
    return datetime.now(BRT)


def today_brt():
    return now_brt().date()


def yesterday_brt():
    return today_brt() - timedelta(days=1)


def d_iso(d):
    return d.isoformat()


def seg_of(account_name, campaign_name):
    if account_name == "Energius [Forms]":
        return "forms"
    if account_name == "Energius [Whats]":
        return "whats"
    # Digiê
    if campaign_name == "DIGIÊ [PARCEIROS]":
        return "dg_parceiros"
    return "dg"


SEGMENTS = ["forms", "whats", "dg", "dg_parceiros"]


# --------------------------------------------------------------------------
# PASSO 2: D.daily (série diária desde 2026-01-01 até ontem, BRT)
# --------------------------------------------------------------------------

def fetch_daily(last_daily_date):
    print(f"Buscando série diária de {DATA_START} até {last_daily_date} ...")
    by_date = {}
    d = date.fromisoformat(DATA_START)
    while d <= last_daily_date:
        by_date[d_iso(d)] = {seg: {"spend": 0.0, "leads": 0} for seg in SEGMENTS}
        d += timedelta(days=1)

    for account_name, act_id in ACCOUNTS.items():
        print(f"  conta: {account_name} ({act_id})")
        rows = graph_get(
            f"{act_id}/insights",
            {
                "level": "campaign",
                "fields": "campaign_name,spend,actions",
                "time_range": json.dumps({"since": DATA_START, "until": d_iso(last_daily_date)}),
                "time_increment": 1,
                "limit": 500,
            },
        )
        for row in rows:
            date_start = row.get("date_start")
            if date_start not in by_date:
                continue
            campaign_name = row.get("campaign_name", "")
            seg = seg_of(account_name, campaign_name)
            spend = float(row.get("spend", 0) or 0)
            actions = row.get("actions", [])
            if seg == "whats":
                leads = action_value(actions, is_messaging_action)
            else:
                leads = action_value(actions, is_lead_action)
            entry = by_date[date_start][seg]
            entry["spend"] += spend
            entry["leads"] += leads

    daily = []
    for iso_date in sorted(by_date.keys()):
        rec = {"date": iso_date}
        for seg in SEGMENTS:
            v = by_date[iso_date][seg]
            rec[seg] = {"spend": round(v["spend"], 2), "leads": int(round(v["leads"]))}
        daily.append(rec)
    return daily


# --------------------------------------------------------------------------
# PASSO 3: D.segments (janelas)
# --------------------------------------------------------------------------

def seg_window_sum(daily, seg, since, until):
    spend = 0.0
    leads = 0
    for rec in daily:
        if since <= rec["date"] <= until:
            spend += rec[seg]["spend"]
            leads += rec[seg]["leads"]
    return round(spend, 2), leads


def build_window(daily, seg, since, until):
    spend, leads = seg_window_sum(daily, seg, since, until)
    cpl = round(spend / leads, 2) if leads else None
    return {"spend": spend, "leads": leads, "cpl": cpl}


def last_closed_month_range(today):
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = last_of_prev_month.replace(day=1)
    return d_iso(first_of_prev_month), d_iso(last_of_prev_month)


def build_segments(daily, last_daily_date, today):
    yd = d_iso(last_daily_date)
    last7_start = d_iso(last_daily_date - timedelta(days=6))
    last30_start = d_iso(last_daily_date - timedelta(days=29))
    lmc_start, lmc_end = last_closed_month_range(today)
    cmtd_start = d_iso(today.replace(day=1))

    segments = {}
    for seg in SEGMENTS:
        segments[seg] = {
            "yesterday": build_window(daily, seg, yd, yd),
            "last7": build_window(daily, seg, last7_start, yd),
            "last30": build_window(daily, seg, last30_start, yd),
            "last_month_closed": build_window(daily, seg, lmc_start, lmc_end),
            "current_month_td": build_window(daily, seg, cmtd_start, yd),
        }
    return segments


# --------------------------------------------------------------------------
# PASSO 4: D.saldo
# --------------------------------------------------------------------------

def fetch_account_fields(act_id):
    url = f"{GRAPH_BASE}/{act_id}"
    resp = requests.get(
        url,
        params={"fields": "spend_cap,amount_spent,currency", "access_token": TOKEN},
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"ERRO Graph API ({resp.status_code}) em {act_id}: {resp.text[:800]}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def weekend_factor_for_group(daily, group_segs, lmc_start, lmc_end):
    weekday_spends = []
    weekend_spends = []
    for rec in daily:
        if lmc_start <= rec["date"] <= lmc_end:
            spend = sum(rec[s]["spend"] for s in group_segs)
            dt = date.fromisoformat(rec["date"])
            if dt.weekday() >= 5:  # 5=sáb, 6=dom
                weekend_spends.append(spend)
            else:
                weekday_spends.append(spend)
    avg_weekday = (sum(weekday_spends) / len(weekday_spends)) if weekday_spends else 0.0
    avg_weekend = (sum(weekend_spends) / len(weekend_spends)) if weekend_spends else 0.0
    if avg_weekday <= 0:
        return 1.0
    return avg_weekend / avg_weekday


def build_saldo(daily, last_daily_date, today):
    lmc_start, lmc_end = last_closed_month_range(today)
    last3_dates = [d_iso(last_daily_date - timedelta(days=i)) for i in range(3)]

    groups = {
        "forms": {"label": "Energius Forms", "act_id": ACCOUNTS["Energius [Forms]"], "segs": ["forms"]},
        "whats": {"label": "Energius WhatsApp", "act_id": ACCOUNTS["Energius [Whats]"], "segs": ["whats"]},
        "digie": {"label": "Digiê (DG + Parceiros)", "act_id": ACCOUNTS["Digiê"], "segs": ["dg", "dg_parceiros"]},
    }

    # próximos 3 dias corridos a partir de hoje (BRT)
    weekend_days_next3 = 0
    weekday_days_next3 = 0
    for offset in range(3):
        d = today + timedelta(days=offset)
        if d.weekday() >= 5:
            weekend_days_next3 += 1
        else:
            weekday_days_next3 += 1

    saldo = []
    for key, g in groups.items():
        acc_fields = fetch_account_fields(g["act_id"])
        cap = float(acc_fields.get("spend_cap") or 0) / 100.0
        spent = float(acc_fields.get("amount_spent") or 0) / 100.0
        remaining = max(0.0, cap - spent)

        avg_daily_spend_3d = 0.0
        for iso_date in last3_dates:
            rec = next((r for r in daily if r["date"] == iso_date), None)
            if rec:
                avg_daily_spend_3d += sum(rec[s]["spend"] for s in g["segs"])
        avg_daily_spend_3d = round(avg_daily_spend_3d / 3, 2)

        wfactor = round(weekend_factor_for_group(daily, g["segs"], lmc_start, lmc_end), 4)

        blended_daily = avg_daily_spend_3d * (
            (weekday_days_next3 / 3) * 1.0 + (weekend_days_next3 / 3) * wfactor
        )

        if blended_daily > 0:
            days_remaining_est = round(remaining / blended_daily, 1)
        else:
            days_remaining_est = 0 if remaining <= 0 else 999

        critical_days = days_remaining_est < 3

        saldo.append(
            {
                "key": key,
                "label": g["label"],
                "spend_cap": round(cap, 2),
                "amount_spent": round(spent, 2),
                "remaining": round(remaining, 2),
                "currency": acc_fields.get("currency", "BRL"),
                "avg_daily_spend_3d": avg_daily_spend_3d,
                "weekend_factor": wfactor,
                "days_remaining_est": days_remaining_est,
                "critical_days": critical_days,
            }
        )
    return saldo


# --------------------------------------------------------------------------
# PASSO 5: D.creatives
# --------------------------------------------------------------------------

def fetch_active_ads(act_id):
    """Retorna dict ad_id -> {name, campaign_name, created_time} apenas para ads ACTIVE."""
    rows = graph_get(
        f"{act_id}/ads",
        {
            "fields": "id,name,campaign{name},created_time,effective_status",
            "filtering": json.dumps([{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]),
            "limit": 500,
        },
    )
    out = {}
    for r in rows:
        if r.get("effective_status") != "ACTIVE":
            continue
        out[r["id"]] = {
            "name": r.get("name", ""),
            "campaign_name": (r.get("campaign") or {}).get("name", ""),
            "created_time": r.get("created_time"),
        }
    return out


def fetch_ad_insights(act_id, since, until):
    rows = graph_get(
        f"{act_id}/insights",
        {
            "level": "ad",
            "fields": "ad_id,ad_name,campaign_name,spend,actions",
            "time_range": json.dumps({"since": since, "until": until}),
            "limit": 500,
        },
    )
    return rows


def parse_created_date(created_time_str, exec_date):
    if not created_time_str:
        return None, None
    try:
        dt = datetime.fromisoformat(created_time_str.replace("Z", "+00:00")).astimezone(BRT)
    except ValueError:
        return None, None
    created_date_str = dt.strftime("%d/%m/%Y")
    days_running = (exec_date - dt.date()).days
    return created_date_str, days_running


def build_window_creatives(account_name, act_id, active_ads, since, until, exec_date):
    insight_rows = fetch_ad_insights(act_id, since, until)
    out = []
    for row in insight_rows:
        ad_id = row.get("ad_id")
        if ad_id not in active_ads:
            continue  # só ACTIVE
        active_info = active_ads[ad_id]
        campaign_name = row.get("campaign_name") or active_info["campaign_name"]
        seg = seg_of(account_name, campaign_name)
        spend = round(float(row.get("spend", 0) or 0), 2)
        actions = row.get("actions", [])
        if seg == "whats":
            leads = int(round(action_value(actions, is_messaging_action)))
        else:
            leads = int(round(action_value(actions, is_lead_action)))
        cpl = round(spend / leads, 2) if leads else None
        created_date, days_running = parse_created_date(active_info["created_time"], exec_date)
        out.append(
            {
                "ad_id": ad_id,
                "ad_name": row.get("ad_name") or active_info["name"],
                "account": account_name,
                "campaign": campaign_name,
                "spend": spend,
                "leads": leads,
                "cpl": cpl,
                "created_date": created_date,
                "days_running": days_running,
            }
        )
    return out


def build_creatives(last_daily_date, exec_date):
    print("Buscando ranking de criativos (ads ativos + insights por janela)...")
    yd = d_iso(last_daily_date)
    last7_start = d_iso(last_daily_date - timedelta(days=6))
    last30_start = d_iso(last_daily_date - timedelta(days=29))

    windows = {
        "yesterday": (yd, yd),
        "last7": (last7_start, yd),
        "last30": (last30_start, yd),
        "accumulated": (DATA_START, yd),
    }

    per_window = {k: [] for k in windows}
    for account_name, act_id in ACCOUNTS.items():
        print(f"  conta: {account_name}")
        active_ads = fetch_active_ads(act_id)
        for wkey, (since, until) in windows.items():
            per_window[wkey].extend(
                build_window_creatives(account_name, act_id, active_ads, since, until, exec_date)
            )

    def sort_best(rows):
        rows = [r for r in rows if r["leads"] and r["leads"] > 0]
        rows.sort(key=lambda r: (-r["leads"], r["cpl"] if r["cpl"] is not None else 1e18))
        return rows[:12]

    def sort_worst(rows):
        zero = [r for r in rows if not r["leads"]]
        zero.sort(key=lambda r: -r["spend"])
        nonzero = [r for r in rows if r["leads"]]
        nonzero.sort(key=lambda r: -(r["cpl"] or 0))
        return zero + nonzero

    def sort_accumulated(rows):
        rows = list(rows)
        rows.sort(key=lambda r: -r["spend"])
        return rows

    best_creatives = sort_best(per_window["last30"])
    worst_day = sort_worst(per_window["yesterday"])
    worst_week = sort_worst(per_window["last7"])
    accumulated = sort_accumulated(per_window["accumulated"])

    active_ids_last7 = {r["ad_id"] for r in per_window["last7"] if r["leads"] and r["leads"] > 0}
    active_creatives_last7_count = len(active_ids_last7)

    def strip(rows):
        # remove campo interno 'ad_id'/'campaign' que o template não usa (mantém ad_name/account/spend/leads/cpl)
        out = []
        for r in rows:
            out.append(
                {
                    "ad_id": r["ad_id"],
                    "ad_name": r["ad_name"],
                    "account": r["account"],
                    "campaign": r["campaign"],
                    "spend": r["spend"],
                    "leads": r["leads"],
                    "cpl": r["cpl"],
                    "created_date": r["created_date"],
                    "days_running": r["days_running"],
                }
            )
        return out

    return {
        "best_creatives": strip(best_creatives),
        "worst_day": strip(worst_day),
        "worst_week": strip(worst_week),
        "accumulated": strip(accumulated),
        "active_creatives_last7_count": active_creatives_last7_count,
    }


# --------------------------------------------------------------------------
# Montagem final + publicação
# --------------------------------------------------------------------------

def main():
    exec_dt = now_brt()
    today = exec_dt.date()
    last_daily_date = yesterday_brt()

    print(f"Execução em {exec_dt.strftime('%d/%m/%Y %H:%M')} BRT | último dia fechado: {last_daily_date}")

    daily = fetch_daily(last_daily_date)
    segments = build_segments(daily, last_daily_date, today)
    saldo = build_saldo(daily, last_daily_date, today)
    creatives = build_creatives(last_daily_date, today)
    generated_at = exec_dt.strftime("%d/%m/%Y %H:%M")

    D = {
        "generated_at": generated_at,
        "daily": daily,
        "saldo": saldo,
        "segments": segments,
        "creatives": creatives,
    }

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    marker = "const D=__DATA_PLACEHOLDER__;"
    if marker not in template:
        print("ERRO: marcador __DATA_PLACEHOLDER__ não encontrado no template.html", file=sys.stderr)
        sys.exit(1)

    data_json = json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    html = template.replace(marker, f"const D={data_json};")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard gerado em {OUT_PATH} ({len(html)} bytes)")

    # resumo no log
    last30 = segments["forms"]["last30"]
    print("Resumo forms/last30:", last30)
    for a in saldo:
        flag = " [CRÍTICO]" if a["critical_days"] else ""
        print(f"Saldo {a['label']}: restante R${a['remaining']} | ~{a['days_remaining_est']} dias{flag}")


if __name__ == "__main__":
    main()
