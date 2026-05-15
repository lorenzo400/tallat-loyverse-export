#!/usr/bin/env python3
"""
tallat_weekly_export.py
=======================
Exporta cada domingo a las 23:59 los datos de la semana desde Loyverse.
Sube automáticamente al Google Drive.
Incluye modo histórico para rellenar semanas desde enero 2026.

Uso:
  python tallat_weekly_export.py              # daemon, cada domingo 23:59
  python tallat_weekly_export.py --once       # ejecuta ahora (semana actual)
  python tallat_weekly_export.py --backfill   # rellena todas las semanas desde enero 2026
  python tallat_weekly_export.py --week 2026-04-20  # semana concreta
"""

import os, sys, json, re, time, datetime, smtplib, logging, io, schedule
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    GDRIVE_OK = True
except ImportError:
    GDRIVE_OK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M"
)
log = logging.getLogger("tallat")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (variables de entorno o .env)
# ─────────────────────────────────────────────────────────────────────────────
LOYVERSE_TOKEN     = os.getenv("LOYVERSE_TOKEN", "")
STORE_ID           = os.getenv("LOYVERSE_STORE_ID", "")
SEND_EMAIL         = os.getenv("SEND_EMAIL", "false").lower() == "true"
EMAIL_FROM         = os.getenv("EMAIL_FROM", "")
EMAIL_TO           = os.getenv("EMAIL_TO", "")
EMAIL_PASS         = os.getenv("EMAIL_PASS", "")
SMTP_HOST          = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT          = int(os.getenv("SMTP_PORT", "587"))
OUTPUT_DIR         = os.getenv("OUTPUT_DIR", "./output")
GDRIVE_FOLDER_NAME = os.getenv("GDRIVE_FOLDER_NAME", "Tallat — Reportes Semanales")
GDRIVE_CREDS_FILE  = os.getenv("GDRIVE_CREDS_FILE", "gdrive_credentials.json")
GDRIVE_CREDS_JSON  = os.getenv("GDRIVE_CREDS_JSON", "")   # JSON inline para Railway
GDRIVE_FOLDER_ID   = os.getenv("GDRIVE_FOLDER_ID", "")    # ID carpeta raíz en Drive
BASE_URL           = "https://api.loyverse.com/v1.0"

BACKFILL_START = datetime.date(2026, 1, 5)   # lunes 5 enero 2026

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

RETAIL_RE = re.compile(r'250g|1[Kk]g', re.IGNORECASE)

TAKEOUT_RATIO_HIST = 0.523
DESECHABLES_RATIO  = {
    "vasos_todos":       1.00,
    "tapas_calientes":   0.80,
    "fundas_corrugadas": 0.60,
    "bolsas_kraft":      0.30,
    "portavasos_x2":     0.15,
    "pajitas":           0.20,
}

# ─────────────────────────────────────────────────────────────────────────────
# LOYVERSE API
# ─────────────────────────────────────────────────────────────────────────────
def loyverse_get(endpoint, params):
    headers = {"Authorization": f"Bearer {LOYVERSE_TOKEN}"}
    items, cursor = [], None
    while True:
        if cursor:
            params = {"cursor": cursor}
        r = requests.get(f"{BASE_URL}/{endpoint}",
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

def get_receipts(date_from: datetime.date, date_to: datetime.date):
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
# FORMATO TEXTO
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

    L.append(f"DESECHABLES -- estimacion  ({data['tickets_takeout']} tickets takeout)")
    for k, lbl in {
        "vasos_todos":       "Vasos (todos los tamanos)",
        "tapas_calientes":   "Tapas calientes",
        "fundas_corrugadas": "Fundas corrugadas 8oz",
        "bolsas_kraft":      "Bolsas papel kraft",
        "portavasos_x2":     "Portavasos x2",
        "pajitas":           "Pajitas (frias)",
    }.items():
        L.append(f"  {lbl:<30}  ~{data['desechables'][k]:>4} uds")
    L.append("")

    L.append("PEDIDO SUGERIDO -- proxima semana  (+15% buffer)")
    for prov, items in pedidos.items():
        L.append(f"  -- {prov} --")
        for it in items:
            flag = " !" if it["pedir"] > it["avg"] * 1.3 else ""
            L.append(f"    {it['item']:<40} -> {it['pedir']:>4} uds{flag}")
        L.append("")

    L += ["-" * 56, "Script: tallat_weekly_export.py  |  Loyverse API v1.0", sep]
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
    L += ["", "*Desechables estimados*",
          f"  Vasos:  ~{data['desechables']['vasos_todos']}",
          f"  Tapas:  ~{data['desechables']['tapas_calientes']}",
          f"  Fundas: ~{data['desechables']['fundas_corrugadas']}",
          f"  Bolsas: ~{data['desechables']['bolsas_kraft']}", "",
          "*Pedido sugerido*"]
    for prov, items in pedidos.items():
        L.append(f"*{prov}*")
        for it in items[:5]:
            L.append(f"  {it['item'][:32]}: {it['pedir']} uds")
    L += ["", "_Generado automaticamente -- Tallat Coffee_"]
    return "\n".join(L)

# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────────────────────
_drive_svc    = None
_folder_cache = {}   # name -> id

def _get_drive():
    global _drive_svc
    if _drive_svc:
        return _drive_svc
    if not GDRIVE_OK:
        log.warning("google-auth no instalado — Drive desactivado")
        return None

    if GDRIVE_CREDS_JSON:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(GDRIVE_CREDS_JSON); tmp.flush()
        creds_path = tmp.name
    else:
        creds_path = GDRIVE_CREDS_FILE

    if not os.path.exists(creds_path):
        log.warning(f"Credenciales Drive no encontradas: {creds_path}")
        return None

    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/drive"])
    _drive_svc = build("drive", "v3", credentials=creds)
    return _drive_svc

def _folder(svc, name, parent_id):
    """Busca o crea una subcarpeta dentro de parent_id."""
    cache_key = f"{parent_id}:{name}"
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]
    q = (f"mimeType='application/vnd.google-apps.folder'"
         f" and name='{name}'"
         f" and '{parent_id}' in parents"
         f" and trashed=false")
    res = svc.files().list(
        q=q, fields="files(id)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    files = res.get("files", [])
    if files:
        fid = files[0]["id"]
    else:
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        fid = svc.files().create(
            body=meta, fields="id",
            supportsAllDrives=True,
        ).execute()["id"]
    _folder_cache[cache_key] = fid
    return fid

def _upload(svc, folder_id, filename, content_str, mime="text/plain"):
    """Sube o actualiza un archivo en la carpeta indicada."""
    q = (f"name='{filename}'"
         f" and '{folder_id}' in parents"
         f" and trashed=false")
    res = svc.files().list(
        q=q, fields="files(id)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    ex  = res.get("files", [])
    med = MediaIoBaseUpload(
        io.BytesIO(content_str.encode("utf-8")),
        mimetype=mime, resumable=False,
    )
    if ex:
        svc.files().update(
            fileId=ex[0]["id"],
            media_body=med,
            supportsAllDrives=True,
        ).execute()
    else:
        svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=med, fields="id",
            supportsAllDrives=True,
        ).execute()
    log.info(f"  Drive: {filename}")

def _update_index(svc, root_id, date_str, week_label, raw):
    fname = "indice_semanas.json"
    q     = f"name='{fname}' and '{root_id}' in parents and trashed=false"
    ex    = svc.files().list(
        q=q, fields="files(id)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute().get("files", [])
    index = {}
    if ex:
        try:
            content = svc.files().get_media(fileId=ex[0]["id"]).execute()
            index   = json.loads(content.decode("utf-8"))
        except Exception:
            pass
    index[date_str] = {
        "semana":       week_label,
        "tickets":      raw["tickets_total"],
        "takeout_pct":  raw["takeout_pct"],
        "revenue_eur":  raw["revenue_eur"],
        "top_bolleria": list(raw["bolleria"].items())[:5],
        "top_cafes":    list(raw["cafes"].items())[:5],
        "total_retail": sum(raw["retail"].values()),
    }
    _upload(svc, root_id, fname,
            json.dumps(index, ensure_ascii=False, indent=2), "application/json")

def upload_to_drive(week_label, date_str, report, wa, raw):
    svc = _get_drive()
    if not svc:
        return
    if not GDRIVE_FOLDER_ID:
        log.warning("  GDRIVE_FOLDER_ID no configurado — Drive desactivado")
        return
    try:
        # Usamos directamente el ID de la carpeta compartida en el Drive personal
        root_id = GDRIVE_FOLDER_ID
        year_id = _folder(svc, "2026", root_id)
        week_id = _folder(svc, f"semana_{date_str}", year_id)

        _upload(svc, week_id, f"reporte_{date_str}.txt",  report)
        _upload(svc, week_id, f"whatsapp_{date_str}.txt", wa)
        _upload(svc, week_id, f"datos_{date_str}.json",
                json.dumps(raw, ensure_ascii=False, indent=2), "application/json")
        _update_index(svc, root_id, date_str, week_label, raw)
        log.info(f"  Drive: semana {date_str} completa")
    except Exception as e:
        log.error(f"  Drive error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# GUARDADO LOCAL + EMAIL
# ─────────────────────────────────────────────────────────────────────────────
def save_local(report, wa, raw, date_str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for fname, txt in [
        (f"reporte_{date_str}.txt",  report),
        (f"whatsapp_{date_str}.txt", wa),
    ]:
        with open(os.path.join(OUTPUT_DIR, fname), "w", encoding="utf-8") as f:
            f.write(txt)
    with open(os.path.join(OUTPUT_DIR, f"datos_{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

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

    try:
        receipts = get_receipts(monday, sunday)
        log.info(f"  {len(receipts)} tickets descargados")

        data    = analyse(receipts)
        pedidos = pedido_sugerido(data["bolleria"])
        report  = fmt_report(data, pedidos, week_label)
        wa      = fmt_whatsapp(data, pedidos, week_label)

        raw = {**data, "week_label": week_label, "date_str": date_str,
               "pedido_sugerido": pedidos}
        del raw["otros_top10"]   # no necesario en el índice

        save_local(report, wa, raw, date_str)
        upload_to_drive(week_label, date_str, report, wa, raw)
        send_email_report(f"Tallat Coffee -- Resumen {week_label}", report)
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
    log.info(f"Backfill: {ok} OK / {fail} errores")

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
