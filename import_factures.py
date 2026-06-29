#!/usr/bin/env python3
"""
Import incrémental des factures Pennylane → Google Sheet PulpMeUp
──────────────────────────────────────────────────────────────────
Ce script fait TOUT — aucun Apps Script requis.

Actions à chaque run :
  1. Authentification Google (service account JWT, sans interaction)
  2. Lecture des factures existantes dans le Sheet
  3. Récupération de toutes les factures Pennylane depuis START_DATE
  4. Détection des avoirs → factures créditées marquées "Exclure" en col I
  5. Ajout des nouvelles factures uniquement (import incrémental)
  6. Mise à jour du statut de paiement des factures existantes
  7. Enrichissement encaissement (M/N) pour factures payées sans date
  8. Tracking colonne J : pour toute cellule J modifiée par l'équipe
     (valeur présente, col K vide), écrit la date du run et USER_EMAIL en K/L

Colonnes du Sheet "(Auto)Liste des factures" :
  A  Date d'émission        E  Montant HT (€)
  B  Nom du client          F  Statut paiement
  C  N° de facture          G  Type (Acompte / Partielle / Clôture / vide)
  D  Catégorie              H  Montant devis HT (€)
                            I  Prise en compte (Claude auto) ← ce script
                            J  Modification équipe           ← modifié manuellement
                            K  Modifié le                    ← rempli par ce script
                            L  Modifié par                   ← rempli par ce script
                            M  Date d'encaissement           ← ce script (Pennylane)
                            N  Montant encaissé HT (€)       ← ce script (Pennylane)

Note : pour forcer un recalcul de M/N sur des factures payées,
       effacer le contenu de la colonne M dans le Sheet puis relancer.

Usage :
  python3 import_factures.py
  python3 import_factures.py --email prenom@pulpmeup.com   (override USER_EMAIL)
"""

import json, os, sys, time, base64, subprocess, urllib.request, urllib.parse
from datetime import datetime

# ──────────────────────────────────────────────────────────────────
#  CONFIGURATION  (env vars prioritaires, fallback local)
# ──────────────────────────────────────────────────────────────────

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN",
                                 "5dbuWydQVNp2s5AfXfK7PDnokU6v0FDiA90q-gymamU")
SA_FILE         = os.environ.get("SA_FILE",
                                 "/home/user/pulpmeup/service_account.json")
SHEET_ID        = "1KO3fnOlxsnEedl8NUH93r9aIUqwc-3zJYrjwsMDx0yc"
INVOICE_TAB     = "(Auto)Liste des factures"
START_DATE      = "2026-01-01"
USER_EMAIL      = "fxmorre@pulpmeup.com"

PENNYLANE_BASE  = "https://app.pennylane.com/api/external/v2"
SHEETS_BASE     = "https://sheets.googleapis.com/v4/spreadsheets"

MONTH_NAMES = {
    1: "Janvier 2026",  2: "Février 2026",   3: "Mars 2026",
    4: "Avril 2026",    5: "Mai 2026",        6: "Juin 2026",
    7: "Juillet 2026",  8: "Août 2026",       9: "Septembre 2026",
    10: "Octobre 2026", 11: "Novembre 2026",  12: "Décembre 2026",
}

STATUS_MAP = {
    "paid":      "Payée",
    "late":      "En retard",
    "draft":     "Brouillon",
    "pending":   "En attente",
    "cancelled": "Annulée",
    "unpaid":    "Non payée",
}

# ──────────────────────────────────────────────────────────────────
#  SERVICE ACCOUNT — reconstruit depuis SA_JSON env var si présent
# ──────────────────────────────────────────────────────────────────

def load_sa():
    sa_json = os.environ.get("SA_JSON", "")
    if sa_json:
        path = "/tmp/sa_pulpmeup.json"
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(base64.b64decode(sa_json).decode())
        return path
    return SA_FILE

# ──────────────────────────────────────────────────────────────────
#  AUTH GOOGLE — service account JWT signé via openssl
# ──────────────────────────────────────────────────────────────────

def get_google_token():
    sa = json.load(open(load_sa()))
    key_file = "/tmp/_sa_key_import.pem"
    with open(key_file, "w") as f:
        f.write(sa["private_key"])

    def b64url(d):
        if isinstance(d, str): d = d.encode()
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    now = int(time.time())
    h   = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}))
    p   = b64url(json.dumps({
        "iss":   sa["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud":   sa["token_uri"],
        "iat":   now,
        "exp":   now + 3600,
    }))
    msg      = f"{h}.{p}"
    msg_file = "/tmp/_jwt_msg_import.txt"
    with open(msg_file, "w") as f:
        f.write(msg)

    sig = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_file, msg_file],
        capture_output=True,
    )
    jwt  = f"{msg}.{b64url(sig.stdout)}"
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":  jwt,
    }).encode()
    req = urllib.request.Request(
        sa["token_uri"], data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["access_token"]

# ──────────────────────────────────────────────────────────────────
#  PENNYLANE API
# ──────────────────────────────────────────────────────────────────

def pl_get(path):
    req = urllib.request.Request(
        PENNYLANE_BASE + path,
        headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def fetch_all_invoices():
    invoices = []
    filter_param = urllib.parse.quote(
        f'[{{"field":"date","operator":"gteq","value":"{START_DATE}"}}]'
    )
    cursor = None
    while True:
        path = (
            f"/customer_invoices?filter={filter_param}"
            f"&sort=date&direction=asc&per_page=100"
        )
        if cursor:
            path += f"&cursor={urllib.parse.quote(cursor)}"
        data = pl_get(path)
        invoices.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.15)
    return invoices


def fetch_payment_date(inv_id):
    """Retourne la date (FR) de la transaction d'encaissement la plus récente, ou ''."""
    try:
        data  = pl_get(f"/customer_invoices/{inv_id}/matched_transactions")
        items = data.get("items", [])
        if not items:
            return ""
        latest = max(items, key=lambda x: x["date"])
        return date_iso_to_fr(latest["date"])
    except Exception:
        return ""

# ──────────────────────────────────────────────────────────────────
#  GOOGLE SHEETS API
# ──────────────────────────────────────────────────────────────────

def sheets_get(gtoken, range_name):
    url = f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {gtoken}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()).get("values", [])


def sheets_append(gtoken, range_name, values):
    url = (
        f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}"
        f":append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    data = json.dumps({"values": values}).encode()
    req  = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {gtoken}",
        "Content-Type":  "application/json",
    }, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def sheets_put(gtoken, range_name, values):
    url = (
        f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}"
        f"?valueInputOption=RAW"
    )
    data = json.dumps({"values": values}).encode()
    req  = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {gtoken}",
        "Content-Type":  "application/json",
    }, method="PUT")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def sheets_batch_put(gtoken, updates):
    url  = f"{SHEETS_BASE}/{SHEET_ID}/values:batchUpdate"
    body = {
        "valueInputOption": "RAW",
        "data": [
            {"range": r, "values": v}
            for r, v in updates
        ],
    }
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {gtoken}",
        "Content-Type":  "application/json",
    }, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ──────────────────────────────────────────────────────────────────
#  UTILITAIRES
# ──────────────────────────────────────────────────────────────────

def fmt(val):
    try:
        return round(float(val), 2)
    except Exception:
        return ""


def date_iso_to_fr(iso):
    p = iso.split("-")
    return f"{p[2]}/{p[1]}/{p[0]}"


def month_from_fr_date(date_fr):
    try:
        return MONTH_NAMES.get(int(date_fr.split("/")[1]), "")
    except Exception:
        return ""


def detect_invoice_type(line_items, inv_label):
    labels = " ".join(l.get("label", "").lower() for l in line_items)
    all_labels = labels + " " + inv_label.lower()
    has_negative = any(float(l.get("currency_amount_before_tax", 0)) < 0 for l in line_items)
    is_acompte   = any(k in all_labels for k in ("acompte", "deposit", "avance", "à-valoir", "a-valoir"))
    is_partielle = any(k in all_labels for k in ("partiel", "partielle", "situation", "intermédiaire", "progress", "tranche"))
    if is_acompte:   return "Acompte"
    if has_negative: return "Clôture"
    if is_partielle: return "Partielle"
    return ""


def auto_prise_en_compte(inv_type, inv_status, date_fr):
    if inv_type in ("Acompte", "Partielle") or inv_status == "cancelled":
        return "Exclure"
    return month_from_fr_date(date_fr)

# ──────────────────────────────────────────────────────────────────
#  ENRICHISSEMENT D'UNE FACTURE
# ──────────────────────────────────────────────────────────────────

def enrich(inv, customer_cache, quote_cache):
    cid = inv["customer"]["id"]
    if cid not in customer_cache:
        customer_cache[cid] = pl_get(f"/customers/{cid}").get("name", "N/A")
        time.sleep(0.08)

    cats    = pl_get(f"/customer_invoices/{inv['id']}/categories")
    cat_str = ", ".join(c["label"] for c in cats.get("items", [])) or "N/A"
    time.sleep(0.08)

    lines      = pl_get(f"/customer_invoices/{inv['id']}/invoice_lines")
    line_items = lines.get("items", [])
    inv_type   = detect_invoice_type(line_items, inv.get("label", ""))
    time.sleep(0.08)

    quote_ht = ""
    if inv.get("quote"):
        qid = inv["quote"]["id"]
        if qid not in quote_cache:
            quote_cache[qid] = pl_get(f"/quotes/{qid}").get("currency_amount_before_tax", "")
            time.sleep(0.08)
        quote_ht = fmt(quote_cache[qid])

    statut  = STATUS_MAP.get(inv["status"], inv["status"])
    date_fr = date_iso_to_fr(inv["date"])
    pec     = auto_prise_en_compte(inv_type, inv["status"], date_fr)

    # Encaissement (cols M, N)
    encaiss_date   = ""
    encaiss_amount = ""
    if inv["status"] == "paid":
        encaiss_date   = fetch_payment_date(inv["id"])
        encaiss_amount = fmt(inv["currency_amount_before_tax"])
        time.sleep(0.08)

    return [date_fr, customer_cache[cid], inv["invoice_number"],
            cat_str, fmt(inv["currency_amount_before_tax"]),
            statut, inv_type, quote_ht, pec,
            "", "", "",             # J, K, L  (équipe / tracking)
            encaiss_date, encaiss_amount]  # M, N

# ──────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    global USER_EMAIL
    if "--email" in sys.argv:
        idx = sys.argv.index("--email")
        if idx + 1 < len(sys.argv):
            USER_EMAIL = sys.argv[idx + 1]

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    print("=" * 62)
    print("  Import incrémental Pennylane → Google Sheet — PulpMeUp")
    print(f"  Run : {now_str}  |  Opérateur : {USER_EMAIL}")
    print("=" * 62)

    print("\n[1/8] Authentification Google (service account)...")
    gtoken = get_google_token()
    print("      ✓ Token OK")

    # Écriture des en-têtes M1/N1 si nécessaire
    headers_row = sheets_get(gtoken, f"{INVOICE_TAB}!M1:N1")
    if not headers_row or not headers_row[0]:
        sheets_batch_put(gtoken, [
            (f"{INVOICE_TAB}!M1", [["Date d'encaissement"]]),
            (f"{INVOICE_TAB}!N1", [["Montant encaissé HT (€)"]]),
        ])
        print("      ✓ En-têtes M1/N1 écrits")

    print(f"\n[2/8] Lecture des factures existantes dans le Sheet...")
    existing_rows = sheets_get(gtoken, f"{INVOICE_TAB}!A2:N2000")

    existing_nums   = {}
    existing_status = {}
    existing_m      = {}
    j_pending       = []

    for i, row in enumerate(existing_rows):
        row = row + [""] * (14 - len(row))
        num = row[2]
        if num:
            existing_nums[num]   = i
            existing_status[num] = row[5]
            existing_m[num]      = row[12]   # col M
        j_val = row[9]
        k_val = row[10]
        if j_val and not k_val:
            j_pending.append(i + 2)

    print(f"      {len(existing_nums)} factures présentes")
    print(f"      {len(j_pending)} modification(s) équipe non tracée(s) en colonne J")

    print(f"\n[3/8] Récupération depuis Pennylane (depuis {START_DATE})...")
    all_invoices = fetch_all_invoices()
    print(f"      {len(all_invoices)} factures récupérées")

    print("\n[4/8] Détection des avoirs...")
    avoir_ids         = set()
    credited_ids      = set()
    credited_in_sheet = {}
    all_by_id         = {inv["id"]: inv for inv in all_invoices}

    for inv in all_invoices:
        if inv.get("credited_invoice"):
            avoir_ids.add(inv["id"])
            orig_id  = inv["credited_invoice"]["id"]
            orig_inv = all_by_id.get(orig_id)
            if orig_inv:
                orig_num = orig_inv["invoice_number"]
                credited_ids.add(orig_id)
                if orig_num in existing_nums:
                    credited_in_sheet[orig_num] = existing_nums[orig_num]

    to_skip = avoir_ids | credited_ids
    print(f"      Avoirs : {len(avoir_ids)}  |  Factures créditées : {len(credited_ids)}")

    new_invoices = [
        inv for inv in all_invoices
        if inv["invoice_number"] not in existing_nums
        and inv["id"] not in to_skip
    ]
    print(f"\n[5/8] Nouvelles factures à importer : {len(new_invoices)}")

    added = 0
    if new_invoices:
        print("      Enrichissement (clients, catégories, lignes, devis, encaissement)...")
        customer_cache, quote_cache, new_rows = {}, {}, []
        for i, inv in enumerate(new_invoices):
            new_rows.append(enrich(inv, customer_cache, quote_cache))
            if (i + 1) % 10 == 0:
                print(f"      {i+1}/{len(new_invoices)} traitées...")
        result = sheets_append(gtoken, f"{INVOICE_TAB}!A:N", new_rows)
        added  = result.get("updates", {}).get("updatedRows", len(new_rows))
        print(f"      ✓ {added} nouvelle(s) ligne(s) ajoutée(s)")

    print(f"\n[6/8] Mise à jour des statuts de paiement...")
    status_updates = []
    for inv in all_invoices:
        num    = inv["invoice_number"]
        new_st = STATUS_MAP.get(inv["status"], inv["status"])
        if num in existing_status and existing_status[num] != new_st:
            sheet_row = existing_nums[num] + 2
            status_updates.append((f"{INVOICE_TAB}!F{sheet_row}", [[new_st]]))
            print(f"      Ligne {sheet_row} ({num}) : {existing_status[num]} → {new_st}")

    if status_updates:
        sheets_batch_put(gtoken, status_updates)
        print(f"      ✓ {len(status_updates)} statut(s) mis à jour")
    else:
        print("      Aucun statut à modifier")

    if credited_in_sheet:
        print(f"\n      Factures créditées → Exclure...")
        avoir_updates = []
        for num, row_idx in credited_in_sheet.items():
            sheet_row = row_idx + 2
            avoir_updates.append((f"{INVOICE_TAB}!I{sheet_row}", [["Exclure"]]))
            print(f"      Ligne {sheet_row} ({num}) → Exclure")
        sheets_batch_put(gtoken, avoir_updates)

    print(f"\n[7/8] Enrichissement encaissement (cols M/N) pour factures payées...")
    payment_updates = []
    for inv in all_invoices:
        num = inv["invoice_number"]
        if (
            num in existing_nums
            and inv["status"] == "paid"
            and existing_m.get(num, "") == ""
        ):
            sheet_row  = existing_nums[num] + 2
            pay_date   = fetch_payment_date(inv["id"])
            time.sleep(0.08)
            if pay_date:
                pay_amt = fmt(inv["currency_amount_before_tax"])
                payment_updates.append((f"{INVOICE_TAB}!M{sheet_row}", [[pay_date]]))
                payment_updates.append((f"{INVOICE_TAB}!N{sheet_row}", [[pay_amt]]))
                print(f"      Ligne {sheet_row} ({num}) → encaissé {pay_date} / {pay_amt} €")

    if payment_updates:
        sheets_batch_put(gtoken, payment_updates)
        print(f"      ✓ {len(payment_updates)//2} encaissement(s) renseigné(s)")
    else:
        print("      Aucun encaissement manquant")

    print(f"\n[8/8] Tracking modifications équipe (colonne J)...")
    if j_pending:
        tracking_updates = []
        for sheet_row in j_pending:
            tracking_updates.append((f"{INVOICE_TAB}!K{sheet_row}", [[now_str]]))
            tracking_updates.append((f"{INVOICE_TAB}!L{sheet_row}", [[USER_EMAIL]]))
        sheets_batch_put(gtoken, tracking_updates)
        print(f"      ✓ {len(j_pending)} ligne(s) tracée(s) → {now_str} / {USER_EMAIL}")
    else:
        print("      Aucune modification équipe non tracée")

    print("\n" + "=" * 62)
    print("  Terminé")
    print(f"  + {added} nouvelle(s) facture(s) ajoutée(s)")
    print(f"  ↻ {len(status_updates)} statut(s) mis à jour")
    print(f"  💰 {len(payment_updates)//2} encaissement(s) renseigné(s)")
    print(f"  ✎ {len(j_pending)} modification(s) équipe tracée(s)")
    if credited_in_sheet:
        print(f"  ✗ {len(credited_in_sheet)} facture(s) créditée(s) → Exclure")
    print("=" * 62)


if __name__ == "__main__":
    main()
