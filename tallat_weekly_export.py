#!/usr/bin/env python3
"""
tallat_weekly_export.py
=======================
Exporta cada domingo a las 23:59 los datos de la semana desde Loyverse.
Guarda todo en Supabase (PostgreSQL en la nube).

Uso:
  python tallat_weekly_export.py              # daemon, cada domingo 23:59
  python tallat_weekly_export.py --once       # ejecuta ahora (semana actual)
  python tallat_weekly_export.py --backfill   # rellena desde enero 2026
  python tallat_weekly_export.py --week 2026-04-20  # semana concreta
"""

import os, sys, json, re, time, datetime, smtplib, logging, schedule
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M"
)
log = logging.getLogger("tallat")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
LOYVERSE_TOKEN  = os.getenv("LOYVERSE_TOKEN", "")
STORE_ID        = os.getenv("LOYVERSE_STORE_ID", "")
SUPABASE_URL    = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "")
SEND_EMAIL      = os.getenv("SEND_EMAIL", "false").lower() == "true"
EMAIL_FROM      = os.getenv("EMAIL_FROM", "")
EMAIL_TO        = os.getenv("EMAIL_TO", "")
EMAIL_PASS      = os.getenv("EMAIL_PASS", "")
SMTP_HOST       = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("SMTP_PORT", "587"))
OUTPUT_DIR      = os.getenv("OUTPUT_DIR", "/tmp/output")

LOYVERSE_API    = "https://api.loyverse.com/v1.0"
BACKFILL_START  = datetime.date(2026, 1, 5)

# ─────────────────────────────────────────────────────────────────────────────
# CATÁLOGO — calibrado con datos reales abr-may 2026
# ─────────────────────────────────────────────────────────────────────────────
BOLLERIA = {
    "Galleta chips choco negro":         {"proveedor": "FLORA",   "buffer": 1.15, "avg": 76},
    "Galleta de avena":                  {"proveedor": "FLORA",   "buffer": 1.15, "avg": 29},
    "Brownie":                           {"proveedor": "FLORA",   "buffer": 1.15, "avg": 24},
    "Muffin Banana y Arándanos":         {"proveedor": "FLORA",   "buffer": 1.15, "avg": 29},
    "Muffin Coco y Chocolate (vegano)":  {"proveedor": "FLORA",   "buffer": 1.15, "avg": 29},
    "Banana Bread vegano con chocolate": {"proveedor": "FLORA",   "buffer": 1.10, "avg": 34},
    "Plum Cake Limón":                   {"proveedor": "FLORA",   "buffer": 1.10, "avg": 35},
    "Plum Cake Mandarina":               {"proveedor": "FLORA",   "buffer": 1.10, "avg": 36},
    "Bizcocho zanahoria":                {"proveedor": "FLORA",   "buffer": 1.10, "avg": 42},
    "Croissant (Plain)":                 {"proveedor": "PANESCO", "buffer": 1.15, "avg": 93},
    "Pain au Chocolat":                  {"proveedor": "PANESCO", "buffer": 1.15, "avg": 24},
    "W Croissant":                       {"proveedor": "PANESCO", "buffer": 1.15, "avg":  3},
    "W Pain au Chocolat":                {"proveedor": "PANESCO", "buffer": 1.15, "avg":  1},
    "Cookie Choco Pistacho GF":          {"proveedor": "CLEM",    "buffer": 1.15, "avg": 59},
    "Cookie Cacahuete":                  {"proveedor": "CLEM",    "buffer": 1.15, "avg": 29},
    "Rollo canela":                      {"proveedor": "CLEM",    "buffer": 1.10, "avg": 21},
}

CAFES = {
    "Café con Leche (Large)", "Café con Leche (Small)",
    "Café con Leche (Large BYO)", "Café con Leche (Small BYO)",
    "Café con Leche (Iced Latte small)",
    "Flat White (---)", "Flat White (BYO)",
    "Cappuccino (Large)", "Cappuccino (Small)",
    "Cappuccino (Large BYO)", "Cappuccino (Small BYO)",
    "Espresso (Large)", "Espresso (Small)",
    "Espresso (Large BYO)", "Espresso (Small BYO)",
    "Long Black (Large)", "Long Black (Small)",
    "Long Black (Large BYO)", "Long Black (Iced Coffee)",
    "TALLAT (Small)", "TALLAT (Large)",
    "TALLAT (Small BYO)", "TALLAT (Large BYO)",
    "Matcha Latte", "Matcha Orange", "Matcha Tonic",
    "Chai Latte", "Hot Chocolate",
    "Mocha (Large)", "Mocha (Small)", "Mocha (Small BYO)", "Iced Mocha",
    "Batch Brew", "Filter (Hoop)",
    "Cold Brew (Large)", "Cold Brew (Small)",
    "Freddo Cappuccino", "Freddo Espresso (-)",
    "Affogato", "Babyccino (Small)", "Babyccino (Large)",
    "Espresso Tonic", "Espresso Orange",
    "Naranja Coffee Soda", "Dirty Orxata", "Shakerato (---)",
}

RETAIL_RE          = re.compile(r'250g|1[Kk]g|BYO 250g', re.IGNORECASE)
TAKEOUT_RATIO_HIST = 0.523
# Ratios SOP × ventas reales takeout (17.061 tickets, 19.702 bebidas, 1,15/ticket)
# Vasos 4oz: espresso, TALLAT S, macchiato S           → 0.085/ticket
# Vasos 6oz: espresso L, cappuccino, flat white,       → 0.643/ticket
#            long black, TALLAT L, café con leche S
# Vasos 8oz: café con leche L, chai, matcha,           → 0.378/ticket
#            hot chocolate, batch brew
# 12oz clear: todos los iced + freddo + specials        → 0.049/ticket
# Tapas 6/7oz = 4oz+6oz drinks = 0.728
# Tapas 8oz   = 8oz drinks     = 0.378
# Fundas: solo Long Black (1/10 llevan funda)           → 0.007/ticket
# Bolsas kraft: desde histórico Greenuso                → 0.210/ticket
DESECHABLES_RATIO  = {
    "vasos_4oz":         0.085,
    "vasos_6oz":         0.643,
    "vasos_8oz":         0.378,
    "vasos_total":       1.106,
    "tapas_6oz_7oz":     0.728,
    "tapas_8oz":         0.378,
    "tapas_calientes":   1.106,
    "tapas_frias":       0.049,
    "fundas_corrugadas": 0.007,
    "bolsas_kraft":      0.210,
    "portavasos_x2":     0.010,
    "pajitas":           0.049,
}

# ─────────────────────────────────────────────────────────────────────────────
# SUPABASE  (REST API — sin librería extra)
# ─────────────────────────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",  # upsert
    }

def sb_insert(table, rows):
    """INSERT simple — para tablas hijo donde se hace DELETE antes."""
    if not rows:
        return
    if isinstance(rows, dict):
        rows = [rows]
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers=headers, json=rows, timeout=15)
    if r.status_code not in (200, 201):
        raise Exception(f"Supabase {table}: {r.status_code} {r.text[:200]}")

def sb_upsert(table, rows, on_conflict="week_start"):
    """UPSERT para tablas con unique constraint (semanas, desechables_estimados)."""
    if not rows:
        return
    if isinstance(rows, dict):
        rows = [rows]
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                      headers=headers, json=rows, timeout=15)
    if r.status_code not in (200, 201):
        raise Exception(f"Supabase {table}: {r.status_code} {r.text[:200]}")

def sb_delete(table, column, value):
    """Borra filas donde column = value (para limpiar antes de reinsertar)."""
    url = f"{SUPABASE_URL}/rest/v1/{table}?{column}=eq.{value}"
    requests.delete(url, headers=sb_headers(), timeout=15)

def save_to_supabase(monday, data, pedidos, week_label):
    date_str = monday.isoformat()
    sunday   = monday + datetime.timedelta(days=6)

    # 1 — semanas (upsert)
    sb_upsert("semanas", {
        "week_start":      date_str,
        "week_end":        sunday.isoformat(),
        "week_label":      week_label,
        "tickets_total":   data["tickets_total"],
        "tickets_takeout": data["tickets_takeout"],
        "takeout_pct":     data["takeout_pct"],
        "revenue_eur":     data["revenue_eur"],
    })

    # 2 — borrar detalles anteriores de esta semana y reinsertar
    for table in ("bolleria_ventas", "cafe_ventas",
                  "retail_ventas", "desechables_estimados"):
        sb_delete(table, "week_start", date_str)

    # 3 — bollería
    boll_rows = []
    for prov, items in pedidos.items():
        for it in items:
            boll_rows.append({
                "week_start":  date_str,
                "proveedor":   prov,
                "producto":    it["item"],
                "qty_vendida": it["vendido"],
                "qty_pedir":   it["pedir"],
                "avg_hist":    it["avg"],
            })
    # añadir productos vendidos que no están en BOLLERIA (por si acaso)
    for name, qty in data["bolleria"].items():
        if not any(r["producto"] == name for r in boll_rows):
            boll_rows.append({
                "week_start":  date_str,
                "proveedor":   "OTRO",
                "producto":    name,
                "qty_vendida": qty,
                "qty_pedir":   0,
                "avg_hist":    0,
            })
    if boll_rows:
        sb_insert("bolleria_ventas", boll_rows)

    # 4 — cafés
    cafe_rows = [
        {"week_start": date_str, "producto": name, "qty_vendida": qty}
        for name, qty in data["cafes"].items()
    ]
    if cafe_rows:
        sb_insert("cafe_ventas", cafe_rows)

    # 5 — retail
    retail_rows = [
        {"week_start": date_str, "producto": name, "qty_vendida": qty}
        for name, qty in data["retail"].items()
    ]
    if retail_rows:
        sb_insert("retail_ventas", retail_rows)

    # 6 — desechables → mapeado a columnas DB existentes
    des = data["desechables"]
    sb_upsert("desechables_estimados", {
        "week_start":        date_str,
        "vasos_todos":       des.get("vasos_total", 0),
        "tapas_calientes":   des.get("tapas_calientes", 0),
        "fundas_corrugadas": des.get("fundas_corrugadas", 0),
        "bolsas_kraft":      des.get("bolsas_kraft", 0),
        "portavasos_x2":     des.get("portavasos_x2", 0),
        "pajitas":           des.get("pajitas", 0),
        # extra detail available in raw data but not in DB schema yet:
        # vasos_4oz, vasos_6oz, vasos_8oz, tapas_6oz_7oz, tapas_8oz, tapas_frias
    })

    log.info(f"  Supabase: semana {date_str} guardada")

# ─────────────────────────────────────────────────────────────────────────────
# LOYVERSE API
# ─────────────────────────────────────────────────────────────────────────────
def loyverse_get(endpoint, params):
    headers = {"Authorization": f"Bearer {LOYVERSE_TOKEN}"}
    items, cursor = [], None
    while True:
        if cursor:
            params = {"cursor": cursor}
        r = requests.get(f"{LOYVERSE_API}/{endpoint}",
                         headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for key, val in data.items():
            if isinstance(val, list):
                items.extend(val)
                break
        cursor = data.get("cursor")
        if not cursor:
            break
    return items

def get_receipts(date_from, date_to):
    params = {
        "created_at_min": datetime.datetime.combine(
            date_from, datetime.time.min).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_at_max": datetime.datetime.combine(
            date_to, datetime.time.max).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 250,
    }
    if STORE_ID:
        params["store_id"] = STORE_ID
    return loyverse_get("receipts", params)

# ─────────────────────────────────────────────────────────────────────────────
# ANÁLISIS
# ─────────────────────────────────────────────────────────────────────────────
def analyse(receipts):
    bolleria = defaultdict(int)
    cafes    = defaultdict(int)
    retail   = defaultdict(int)
    otros    = defaultdict(int)
    total_t  = takeout_t = 0
    revenue  = 0.0

    for r in receipts:
        if r.get("cancelled_at"):
            continue
        total_t += 1
        if "take" in (r.get("dining_option") or "").lower():
            takeout_t += 1
        revenue += float(r.get("total_money") or 0)
        for line in r.get("line_items", []):
            name = (line.get("item_name") or "").strip()
            qty  = int(line.get("quantity") or 1)
            if   name in BOLLERIA:       bolleria[name] += qty
            elif name in CAFES:          cafes[name]    += qty
            elif RETAIL_RE.search(name): retail[name]   += qty
            else:                        otros[name]    += qty

    des_base = takeout_t or round(total_t * TAKEOUT_RATIO_HIST)
    return {
        "bolleria":        dict(sorted(bolleria.items(), key=lambda x: -x[1])),
        "cafes":           dict(sorted(cafes.items(),    key=lambda x: -x[1])),
        "retail":          dict(sorted(retail.items(),   key=lambda x: -x[1])),
        "otros_top10":     dict(list(sorted(otros.items(), key=lambda x: -x[1]))[:10]),
        "desechables":     {k: round(des_base * v) for k, v in DESECHABLES_RATIO.items()},
        "tickets_total":   total_t,
        "tickets_takeout": takeout_t,
        "takeout_pct":     round(takeout_t / total_t * 100, 1) if total_t else 0,
        "revenue_eur":     round(revenue, 2),
    }

def pedido_sugerido(bolleria_sold):
    out = defaultdict(list)
    for name, qty in bolleria_sold.items():
        if name not in BOLLERIA:
            continue
        cfg = BOLLERIA[name]
        sug = round(qty * cfg["buffer"])
        sug = round(sug / 10) * 10 if sug >= 20 else round(sug / 5) * 5
        out[cfg["proveedor"]].append({
            "item": name, "vendido": qty, "pedir": sug, "avg": cfg["avg"],
        })
    return dict(out)

# ─────────────────────────────────────────────────────────────────────────────
# FORMATO TEXTO (para logs y email)
# ─────────────────────────────────────────────────────────────────────────────
def fmt_report(data, pedidos, week_label):
    L, sep = [], "=" * 56
    L += [sep, "  TALLAT COFFEE -- RESUMEN SEMANAL", f"  {week_label}", sep, ""]
    L += ["GENERAL",
          f"  Tickets totales :  {data['tickets_total']:>6}",
          f"  Takeout         :  {data['tickets_takeout']:>6}  ({data['takeout_pct']}%)",
          f"  Ventas netas    :  {data['revenue_eur']:>8.2f} EUR", ""]

    L.append("CAFES -- uds vendidas")
    max_c = max(data["cafes"].values(), default=1)
    for name, qty in list(data["cafes"].items())[:12]:
        bar = "#" * round(qty / max_c * 18)
        L.append(f"  {name:<34} {qty:>4}  {bar}")
    rest_c = sum(list(data["cafes"].values())[12:])
    if rest_c:
        L.append(f"  (+ {len(data['cafes'])-12} items mas: {rest_c} uds)")
    L += [f"  {'TOTAL':<34} {sum(data['cafes'].values()):>4}", ""]

    L.append("BOLLERIA -- uds vendidas")
    for name, qty in data["bolleria"].items():
        prov = BOLLERIA.get(name, {}).get("proveedor", "?")
        L.append(f"  [{prov:<7}] {name:<36} {qty:>4}")
    L += [f"  {'TOTAL':<46} {sum(data['bolleria'].values()):>4}", ""]

    if data["retail"]:
        L.append("RETAIL CAFE -- bolsas vendidas")
        for name, qty in data["retail"].items():
            L.append(f"  {name:<44} {qty:>4}")
        L += [f"  {'TOTAL':<44} {sum(data['retail'].values()):>4}", ""]

    des = data["desechables"]
    L.append(f"DESECHABLES -- estimacion  ({data['tickets_takeout']} tickets takeout)")
    for k, lbl in {
        "vasos_4oz":         "Vasos 4oz (espresso/TALLAT S)",
        "vasos_6oz":         "Vasos 6oz (capp/flat/long black)",
        "vasos_8oz":         "Vasos 8oz (cafe leche L/chai/matcha)",
        "vasos_total":       "TOTAL vasos calientes",
        "tapas_6oz_7oz":     "Tapas negra 6/7oz",
        "tapas_8oz":         "Tapas negra/blanca 8oz",
        "tapas_calientes":   "TOTAL tapas calientes",
        "tapas_frias":       "Tapas planas PET (iced)",
        "fundas_corrugadas": "Fundas (long black 1/10)",
        "bolsas_kraft":      "Bolsas kraft (takeaway boll.)",
        "portavasos_x2":     "Portavasos x2",
        "pajitas":           "Pajitas (bebidas frias)",
    }.items():
        L.append(f"  {lbl:<34}  ~{des.get(k,0):>4} uds")
    L.append("")

    L.append("PEDIDO SUGERIDO -- proxima semana  (+15% buffer)")
    for prov, items in pedidos.items():
        L.append(f"  -- {prov} --")
        for it in items:
            flag = " !" if it["pedir"] > it["avg"] * 1.3 else ""
            L.append(f"    {it['item']:<40} -> {it['pedir']:>4} uds{flag}")
        L.append("")

    L += ["-" * 56, "Datos guardados en Supabase  |  Loyverse API v1.0", sep]
    return "\n".join(L)

def fmt_whatsapp(data, pedidos, week_label):
    L = [f"*TALLAT -- {week_label}*", "",
         "*General*",
         f"Tickets: {data['tickets_total']}  |  Takeout: {data['takeout_pct']}%",
         f"Ventas: {data['revenue_eur']:.0f} EUR", "", "*Cafes (top 6)*"]
    for n, q in list(data["cafes"].items())[:6]:
        L.append(f"  {n}: {q}")
    L += ["", "*Bolleria*"]
    for n, q in list(data["bolleria"].items())[:8]:
        prov = BOLLERIA.get(n, {}).get("proveedor", "")
        L.append(f"  [{prov}] {n}: {q}")
    if data["retail"]:
        L += ["", "*Retail cafe*"]
        for n, q in list(data["retail"].items())[:6]:
            L.append(f"  {n}: {q}")
    des = data["desechables"]
    L += ["", "*Desechables estimados*",
          f"  Vasos 4oz: ~{des.get('vasos_4oz',0)}  6oz: ~{des.get('vasos_6oz',0)}  8oz: ~{des.get('vasos_8oz',0)}",
          f"  Tapas cal. 6/7oz: ~{des.get('tapas_6oz_7oz',0)}  |  8oz: ~{des.get('tapas_8oz',0)}",
          f"  Tapas PET (frias): ~{des.get('tapas_frias',0)}",
          f"  Fundas (LB 1/10): ~{des.get('fundas_corrugadas',0)}",
          f"  Bolsas kraft: ~{des.get('bolsas_kraft',0)}  |  Pajitas: ~{des.get('pajitas',0)}", "",
          "*Pedido sugerido*"]
    for prov, items in pedidos.items():
        L.append(f"*{prov}*")
        for it in items[:5]:
            L.append(f"  {it['item'][:32]}: {it['pedir']} uds")
    L += ["", "_Generado automaticamente -- Tallat Coffee_"]
    return "\n".join(L)

# ─────────────────────────────────────────────────────────────────────────────
# GUARDADO LOCAL + EMAIL
# ─────────────────────────────────────────────────────────────────────────────
def save_local(report, wa, date_str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for fname, txt in [
        (f"reporte_{date_str}.txt",  report),
        (f"whatsapp_{date_str}.txt", wa),
    ]:
        with open(os.path.join(OUTPUT_DIR, fname), "w", encoding="utf-8") as f:
            f.write(txt)

def send_email_report(subject, body):
    if not SEND_EMAIL or not EMAIL_FROM:
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(); s.login(EMAIL_FROM, EMAIL_PASS)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    log.info(f"  Email enviado a {EMAIL_TO}")

# ─────────────────────────────────────────────────────────────────────────────
# JOB: procesar UNA semana
# ─────────────────────────────────────────────────────────────────────────────
def process_week(monday: datetime.date):
    sunday     = monday + datetime.timedelta(days=6)
    date_str   = monday.strftime("%Y-%m-%d")
    week_label = f"{monday.strftime('%d/%m')} – {sunday.strftime('%d/%m/%Y')}"

    log.info(f"=== Semana {week_label} ===")

    if not LOYVERSE_TOKEN:
        log.error("LOYVERSE_TOKEN no configurado"); return False
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("SUPABASE_URL o SUPABASE_KEY no configurados"); return False

    try:
        receipts = get_receipts(monday, sunday)
        log.info(f"  {len(receipts)} tickets descargados")

        data    = analyse(receipts)
        pedidos = pedido_sugerido(data["bolleria"])
        report  = fmt_report(data, pedidos, week_label)
        wa      = fmt_whatsapp(data, pedidos, week_label)

        save_local(report, wa, date_str)
        save_to_supabase(monday, data, pedidos, week_label)
        send_email_report(f"Tallat Coffee -- Resumen {week_label}", report)

        # Mostrar versión WhatsApp en logs para copiar fácilmente
        log.info("\n── WHATSAPP ──\n" + wa + "\n──────────────")
        return True

    except requests.HTTPError as e:
        log.error(f"  API {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        log.exception(f"  Error: {e}")
    return False

# ─────────────────────────────────────────────────────────────────────────────
# BACKFILL
# ─────────────────────────────────────────────────────────────────────────────
def backfill():
    today, monday, weeks = datetime.date.today(), BACKFILL_START, []
    while monday <= today:
        weeks.append(monday)
        monday += datetime.timedelta(days=7)

    log.info(f"Backfill: {len(weeks)} semanas desde {BACKFILL_START} hasta {today}")
    ok = fail = 0
    for i, mon in enumerate(weeks, 1):
        log.info(f"[{i}/{len(weeks)}]")
        if process_week(mon): ok += 1
        else:                 fail += 1
        if i < len(weeks): time.sleep(2)
    log.info(f"Backfill completado: {ok} OK / {fail} errores")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def run_current_week():
    today  = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    process_week(monday)

if __name__ == "__main__":
    args = set(sys.argv[1:])

    if "--backfill" in args:
        backfill()

    elif "--week" in args:
        try:
            idx    = sys.argv.index("--week") + 1
            monday = datetime.date.fromisoformat(sys.argv[idx])
            monday -= datetime.timedelta(days=monday.weekday())
            process_week(monday)
        except (IndexError, ValueError) as e:
            log.error(f"Formato: --week YYYY-MM-DD  ({e})")

    elif "--once" in args or "--test" in args:
        run_current_week()

    else:
        log.info("Daemon -- ejecutara cada domingo a las 23:59")
        schedule.every().sunday.at("23:59").do(run_current_week)
        log.info(f"Proxima ejecucion: {schedule.next_run()}")
        while True:
            schedule.run_pending()
            time.sleep(30)
