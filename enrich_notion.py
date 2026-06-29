#!/usr/bin/env python3
"""
Enrichissement Notion → Google Sheet PulpMeUp
──────────────────────────────────────────────
Pour chaque facture du Sheet, retrouve le projet Notion correspondant
et met à jour :
  O  Date création dossier Notion
  P  Date de Livraison (propriété Notion "Date de Livraison")

Note : M et N sont réservés à l'encaissement Pennylane (import_factures.py).

Correspondance : nom client (col B du Sheet) ↔ titre projet Notion
  → le titre Notion doit contenir le nom du client (insensible à la casse)
  → si plusieurs projets matchent, le plus récent est retenu

Usage :
  python3 enrich_notion.py
"""

import json, os, sys, time, base64, subprocess, urllib.request, urllib.parse, re
from datetime import datetime

# ──────────────────────────────────────────────────────────────────
#  CONFIGURATION  (env vars prioritaires, fallback local)
# ──────────────────────────────────────────────────────────────────

NOTION_TOKEN    = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID    = "75e8be75985f4c78be0653629df96b6a"
SA_FILE         = os.environ.get("SA_FILE", "/home/user/pulpmeup/service_account.json")
SHEET_ID        = "1KO3fnOlxsnEedl8NUH93r9aIUqwc-3zJYrjwsMDx0yc"
INVOICE_TAB     = "(Auto)Liste des factures"

NOTION_BASE     = "https://api.notion.com/v1"
SHEETS_BASE     = "https://sheets.googleapis.com/v4/spreadsheets"


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
    key_file = "/tmp/_sa_key_notion.pem"
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
    msg_file = "/tmp/_jwt_msg_notion.txt"
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
#  NOTION API
# ──────────────────────────────────────────────────────────────────

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def fetch_all_notion_projects():
    projects = []
    cursor   = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data_bytes = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{NOTION_BASE}/databases/{NOTION_DB_ID}/query",
            data=data_bytes,
            headers=notion_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        projects.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.1)
    return projects


def parse_project(page):
    props = page["properties"]

    # Titre (Name)
    title_parts = props.get("Name", {}).get("title", [])
    title = title_parts[0]["plain_text"] if title_parts else ""

    # Date de création (toujours disponible)
    created_iso = page["created_time"][:10]

    # Date de Livraison (propriété custom)
    livraison_prop = props.get("Date de Livraison", {}).get("date")
    livraison_iso  = livraison_prop["start"] if livraison_prop else ""

    return {
        "title":     title,
        "created":   created_iso,
        "livraison": livraison_iso,
        "page_id":   page["id"],
    }


def normalize(s):
    """Supprime emojis, ponctuation, met en minuscule pour la comparaison."""
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

# ──────────────────────────────────────────────────────────────────
#  GOOGLE SHEETS API
# ──────────────────────────────────────────────────────────────────

def sheets_get(gtoken, range_name):
    url = f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {gtoken}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()).get("values", [])


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
#  MAPPING MANUEL (onglet "Mapping Clients" du Sheet)
# ──────────────────────────────────────────────────────────────────

def load_mapping(gtoken):
    """Lit l'onglet Mapping Clients : col A = nom Pennylane, col B = nom Notion."""
    rows = sheets_get(gtoken, "Mapping Clients!A2:B500")
    mapping = {}
    for row in rows:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            mapping[row[0].strip().upper()] = row[1].strip()
    return mapping

# ──────────────────────────────────────────────────────────────────
#  CORRESPONDANCE CLIENT ↔ PROJET NOTION
# ──────────────────────────────────────────────────────────────────

def find_project(client_name, projects, mapping=None):
    """
    Cherche le projet Notion dont le titre contient client_name.
    Utilise d'abord le mapping manuel (onglet Mapping Clients), puis
    tente un match automatique sur le titre. Retourne le plus récent.
    """
    # Résolution via mapping manuel
    if mapping:
        mapped_name = mapping.get(client_name.strip().upper())
        if mapped_name:
            client_name = mapped_name

    needle = normalize(client_name)
    if not needle:
        return None

    matches = []
    for p in projects:
        haystack = normalize(p["title"])
        # Match si le nom client apparaît comme mot entier dans le titre
        if re.search(r"\b" + re.escape(needle) + r"\b", haystack):
            matches.append(p)

    if not matches:
        # Fallback : contenu partiel (ex: "ADN" dans "ADN Executive")
        for p in projects:
            haystack = normalize(p["title"])
            if needle in haystack:
                matches.append(p)

    if not matches:
        return None

    # Plus récent en premier
    matches.sort(key=lambda x: x["created"], reverse=True)
    return matches[0]


def iso_to_fr(iso):
    if not iso:
        return ""
    p = iso.split("-")
    return f"{p[2]}/{p[1]}/{p[0]}"

# ──────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print("  Enrichissement Notion → Google Sheet — PulpMeUp")
    print(f"  Run : {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 62)

    print("\n[1/4] Authentification Google...")
    gtoken = get_google_token()
    print("      ✓ Token OK")

    print("\n[2/4] Lecture du Sheet (colonnes A:P)...")
    rows = sheets_get(gtoken, f"{INVOICE_TAB}!A2:P2000")
    print(f"      {len(rows)} lignes lues")

    print("\n[3/4] Récupération des projets Notion...")
    raw_projects = fetch_all_notion_projects()
    projects     = [parse_project(p) for p in raw_projects]
    print(f"      {len(projects)} projets récupérés")

    mapping = load_mapping(gtoken)
    print(f"      {len(mapping)} correspondance(s) manuelle(s) chargée(s)")

    print("\n[4/4] Mise à jour des colonnes O et P (Notion)...")
    updates      = []
    matched      = 0
    not_matched  = []

    for i, row in enumerate(rows):
        row = row + [""] * (16 - len(row))
        client_name = row[1]   # col B
        if not client_name:
            continue

        current_o = row[14]    # col O
        current_p = row[15]    # col P
        sheet_row = i + 2

        project = find_project(client_name, projects, mapping)

        if not project:
            not_matched.append(f"Ligne {sheet_row} — {client_name}")
            continue

        new_o = iso_to_fr(project["created"])
        new_p = iso_to_fr(project["livraison"])

        if new_o != current_o or new_p != current_p:
            updates.append((f"{INVOICE_TAB}!O{sheet_row}", [[new_o]]))
            updates.append((f"{INVOICE_TAB}!P{sheet_row}", [[new_p]]))
            print(f"      Ligne {sheet_row} ({client_name}) → créé {new_o} | livré {new_p or '—'}")
            matched += 1

    if updates:
        sheets_batch_put(gtoken, updates)

    print(f"\n{'=' * 62}")
    print(f"  Terminé")
    print(f"  ✓ {matched} ligne(s) enrichie(s)")
    if not_matched:
        print(f"  ✗ {len(not_matched)} client(s) sans projet Notion trouvé :")
        for nm in not_matched[:10]:
            print(f"      {nm}")
        if len(not_matched) > 10:
            print(f"      ... et {len(not_matched)-10} autres")
    print("=" * 62)


if __name__ == "__main__":
    main()
