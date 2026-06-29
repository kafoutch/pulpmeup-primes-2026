Lance l'import des factures Pennylane vers le Google Sheet PulpMeUp.

## Instructions

1. Vérifie que `/home/user/pulpmeup/import_factures.py` existe.
   S'il n'existe pas, écris-le avec le contenu complet en bas de ce fichier.

2. Choix du mode :
   - **Mode normal** (run hebdo, incrémental) :
     ```bash
     python3 /home/user/pulpmeup/import_factures.py
     ```
   - **Mode rebuild** (repart de zéro, efface tout et réimporte depuis 2026-01-01) :
     ```bash
     python3 /home/user/pulpmeup/import_factures.py --rebuild
     ```

3. Affiche le résumé final (factures ajoutées, statuts mis à jour, encaissements renseignés).

4. En cas d'erreur :
   - **401 Pennylane** → token expiré, demander le nouveau token et mettre à jour `PENNYLANE_TOKEN` dans le script
   - **service_account.json introuvable** → le fichier doit être à `/home/user/pulpmeup/service_account.json`
   - **Autre erreur** → afficher le message complet et proposer un diagnostic

---

## Contenu complet du script `/home/user/pulpmeup/import_factures.py`

```python
#!/usr/bin/env python3
"""
Import des factures Pennylane → Google Sheet PulpMeUp
──────────────────────────────────────────────────────
Colonnes du Sheet "(Auto)Liste des factures" :
  A  Date d'émission
  B  Nom du client
  C  N° de facture
  D  Montant devis HT (€)       ← devis lié, vide si aucun
  E  Catégorie
  F  Statut paiement
  G  Montant Encaissé HT (€)    ← montant HT une fois payée
  H  Date Encaissement           ← date transaction Pennylane

Modes :
  python3 import_factures.py             → incrémental (nouvelles + màj)
  python3 import_factures.py --rebuild   → efface tout et réimporte
"""

import json, os, sys, time, base64, subprocess, urllib.request, urllib.parse
from datetime import datetime

# ──────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ──────────────────────────────────────────────────────────────────

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN",
                                 "5dbuWydQVNp2s5AfXfK7PDnokU6v0FDiA90q-gymamU")
SA_FILE         = os.environ.get("SA_FILE",
                                 "/home/user/pulpmeup/service_account.json")
SHEET_ID        = "1KO3fnOlxsnEedl8NUH93r9aIUqwc-3zJYrjwsMDx0yc"
INVOICE_TAB     = "(Auto)Liste des factures"
START_DATE      = "2026-01-01"

PENNYLANE_BASE  = "https://app.pennylane.com/api/external/v2"
SHEETS_BASE     = "https://sheets.googleapis.com/v4/spreadsheets"

HEADERS = [
    "Date d'émission", "Nom du client", "N° de facture",
    "Montant devis HT (€)", "Catégorie", "Statut paiement",
    "Montant Encaissé HT (€)", "Date Encaissement",
]

STATUS_MAP = {
    "paid":      "Payée",
    "late":      "En retard",
    "draft":     "Brouillon",
    "pending":   "En attente",
    "cancelled": "Annulée",
    "unpaid":    "Non payée",
}

# ──────────────────────────────────────────────────────────────────
#  SERVICE ACCOUNT
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
#  AUTH GOOGLE
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
    """Date FR de la transaction d'encaissement la plus récente, ou ''."""
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


def sheets_clear(gtoken, range_name):
    url = f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}:clear"
    req = urllib.request.Request(url, data=b"{}", headers={
        "Authorization": f"Bearer {gtoken}",
        "Content-Type":  "application/json",
    }, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


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
        "data": [{"range": r, "values": v} for r, v in updates],
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

# ──────────────────────────────────────────────────────────────────
#  ENRICHISSEMENT D'UNE FACTURE → 8 colonnes
# ──────────────────────────────────────────────────────────────────

def enrich(inv, customer_cache, quote_cache):
    cid = inv["customer"]["id"]
    if cid not in customer_cache:
        customer_cache[cid] = pl_get(f"/customers/{cid}").get("name", "N/A")
        time.sleep(0.08)

    cats    = pl_get(f"/customer_invoices/{inv['id']}/categories")
    cat_str = ", ".join(c["label"] for c in cats.get("items", [])) or "N/A"
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

    encaiss_amount = ""
    encaiss_date   = ""
    if inv["status"] == "paid":
        encaiss_amount = fmt(inv["currency_amount_before_tax"])
        encaiss_date   = fetch_payment_date(inv["id"])
        time.sleep(0.08)

    return [date_fr, customer_cache[cid], inv["invoice_number"],
            quote_ht, cat_str, statut,
            encaiss_amount, encaiss_date]

# ──────────────────────────────────────────────────────────────────
#  REBUILD — efface tout et réimporte depuis START_DATE
# ──────────────────────────────────────────────────────────────────

def run_rebuild(gtoken):
    print(f"\n[REBUILD] Effacement de l'onglet '{INVOICE_TAB}'...")
    sheets_clear(gtoken, f"{INVOICE_TAB}!A1:Z2000")

    print("[REBUILD] Écriture des en-têtes...")
    sheets_put(gtoken, f"{INVOICE_TAB}!A1:H1", [HEADERS])

    print(f"[REBUILD] Récupération depuis Pennylane (depuis {START_DATE})...")
    all_invoices = fetch_all_invoices()
    print(f"         {len(all_invoices)} factures brutes")

    avoir_ids = {inv["id"] for inv in all_invoices if inv.get("credited_invoice")}
    invoices  = [inv for inv in all_invoices if inv["id"] not in avoir_ids]
    print(f"         {len(avoir_ids)} avoir(s) exclu(s) → {len(invoices)} à importer")

    print("[REBUILD] Enrichissement (clients, catégories, devis, paiements)...")
    customer_cache, quote_cache, rows = {}, {}, []
    for i, inv in enumerate(invoices):
        rows.append(enrich(inv, customer_cache, quote_cache))
        if (i + 1) % 10 == 0:
            print(f"         {i+1}/{len(invoices)} traitées...")

    print(f"[REBUILD] Écriture de {len(rows)} lignes...")
    for start in range(0, len(rows), 200):
        chunk = rows[start:start+200]
        sheets_append(gtoken, f"{INVOICE_TAB}!A:H", chunk)
        if len(rows) > 200:
            print(f"         bloc {start+1}–{start+len(chunk)} envoyé")

    print(f"[REBUILD] ✓ {len(rows)} lignes importées.")
    return len(rows)

# ──────────────────────────────────────────────────────────────────
#  INCREMENTAL — nouvelles factures + màj statut + màj encaissement
# ──────────────────────────────────────────────────────────────────

def run_incremental(gtoken):
    print(f"\n[1/4] Lecture des factures existantes...")
    existing_rows = sheets_get(gtoken, f"{INVOICE_TAB}!A2:H2000")

    existing_nums   = {}
    existing_status = {}
    existing_g      = {}

    for i, row in enumerate(existing_rows):
        row = row + [""] * (8 - len(row))
        num = row[2]
        if num:
            existing_nums[num]   = i
            existing_status[num] = row[5]
            existing_g[num]      = row[6]

    print(f"      {len(existing_nums)} factures présentes")

    print(f"\n[2/4] Récupération depuis Pennylane (depuis {START_DATE})...")
    all_invoices = fetch_all_invoices()
    avoir_ids    = {inv["id"] for inv in all_invoices if inv.get("credited_invoice")}
    invoices     = [inv for inv in all_invoices if inv["id"] not in avoir_ids]
    print(f"      {len(invoices)} factures (hors avoirs)")

    new_invoices = [inv for inv in invoices if inv["invoice_number"] not in existing_nums]
    print(f"\n[3/4] Nouvelles factures : {len(new_invoices)}")

    added = 0
    if new_invoices:
        customer_cache, quote_cache, new_rows = {}, {}, []
        for i, inv in enumerate(new_invoices):
            new_rows.append(enrich(inv, customer_cache, quote_cache))
            if (i + 1) % 10 == 0:
                print(f"      {i+1}/{len(new_invoices)} traitées...")
        result = sheets_append(gtoken, f"{INVOICE_TAB}!A:H", new_rows)
        added  = result.get("updates", {}).get("updatedRows", len(new_rows))
        print(f"      ✓ {added} ligne(s) ajoutée(s)")

    print(f"\n[4/4] Mises à jour (statut + encaissement)...")
    updates = []
    for inv in invoices:
        num    = inv["invoice_number"]
        new_st = STATUS_MAP.get(inv["status"], inv["status"])
        if num not in existing_nums:
            continue
        sheet_row = existing_nums[num] + 2

        if existing_status[num] != new_st:
            updates.append((f"{INVOICE_TAB}!F{sheet_row}", [[new_st]]))

        if inv["status"] == "paid" and existing_g.get(num, "") == "":
            pay_date = fetch_payment_date(inv["id"])
            time.sleep(0.08)
            if pay_date:
                updates.append((f"{INVOICE_TAB}!G{sheet_row}",
                                 [[fmt(inv["currency_amount_before_tax"])]]))
                updates.append((f"{INVOICE_TAB}!H{sheet_row}", [[pay_date]]))

    if updates:
        sheets_batch_put(gtoken, updates)
    print(f"      ✓ {len(updates)} cellule(s) mise(s) à jour")
    return added, len(updates)

# ──────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    rebuild = "--rebuild" in sys.argv
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    print("=" * 62)
    print("  Import Pennylane → Google Sheet — PulpMeUp")
    print(f"  Run : {now_str}  |  Mode : {'REBUILD' if rebuild else 'incrémental'}")
    print("=" * 62)

    print("\nAuthentification Google...")
    gtoken = get_google_token()
    print("✓ Token OK")

    if rebuild:
        total = run_rebuild(gtoken)
        print("\n" + "=" * 62)
        print(f"  ✓ REBUILD terminé — {total} factures importées")
        print("=" * 62)
    else:
        added, updated = run_incremental(gtoken)
        print("\n" + "=" * 62)
        print(f"  + {added} nouvelle(s) facture(s)")
        print(f"  ↻ {updated} cellule(s) mise(s) à jour")
        print("=" * 62)


if __name__ == "__main__":
    main()
```
