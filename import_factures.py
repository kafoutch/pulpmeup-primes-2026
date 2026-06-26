#!/usr/bin/env python3
"""
Import incrémental des factures Pennylane → Google Sheet PulpMeUp
Tokens lus depuis les variables d'environnement :
  PENNYLANE_TOKEN  — token API Pennylane
  SA_JSON          — contenu du service_account.json en base64
"""
import json, sys, time, base64, subprocess, urllib.request, urllib.parse, os
from datetime import datetime

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
SA_FILE         = "/tmp/sa_pulpmeup.json"
SHEET_ID        = "1KO3fnOlxsnEedl8NUH93r9aIUqwc-3zJYrjwsMDx0yc"
INVOICE_TAB     = "(Auto)Liste des factures"
START_DATE      = "2026-01-01"
USER_EMAIL      = "fxmorre@pulpmeup.com"

PENNYLANE_BASE  = "https://app.pennylane.com/api/external/v2"
SHEETS_BASE     = "https://sheets.googleapis.com/v4/spreadsheets"

MONTH_NAMES = {
    1:"Janvier 2026",2:"Février 2026",3:"Mars 2026",4:"Avril 2026",
    5:"Mai 2026",6:"Juin 2026",7:"Juillet 2026",8:"Août 2026",
    9:"Septembre 2026",10:"Octobre 2026",11:"Novembre 2026",12:"Décembre 2026",
}
STATUS_MAP = {"paid":"Payée","late":"En retard","draft":"Brouillon",
              "pending":"En attente","cancelled":"Annulée","unpaid":"Non payée"}

def load_sa():
    if not os.path.exists(SA_FILE):
        sa_b64 = os.environ.get("SA_JSON","")
        if not sa_b64:
            raise SystemExit("Variable SA_JSON manquante.")
        with open(SA_FILE,"w") as f:
            f.write(base64.b64decode(sa_b64).decode())

def get_google_token():
    load_sa()
    sa = json.load(open(SA_FILE))
    key_file = "/tmp/_sa_key_import.pem"
    with open(key_file,"w") as f: f.write(sa["private_key"])
    def b64url(d):
        if isinstance(d,str): d=d.encode()
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    now=int(time.time())
    h=b64url(json.dumps({"alg":"RS256","typ":"JWT"}))
    p=b64url(json.dumps({"iss":sa["client_email"],"scope":"https://www.googleapis.com/auth/spreadsheets","aud":sa["token_uri"],"iat":now,"exp":now+3600}))
    msg=f"{h}.{p}"
    msg_file="/tmp/_jwt_msg_import.txt"
    with open(msg_file,"w") as f: f.write(msg)
    sig=subprocess.run(["openssl","dgst","-sha256","-sign",key_file,msg_file],capture_output=True)
    jwt=f"{msg}.{b64url(sig.stdout)}"
    body=urllib.parse.urlencode({"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer","assertion":jwt}).encode()
    req=urllib.request.Request(sa["token_uri"],data=body,headers={"Content-Type":"application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["access_token"]

def pl_get(path):
    req=urllib.request.Request(PENNYLANE_BASE+path,headers={"Authorization":f"Bearer {PENNYLANE_TOKEN}"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read())

def fetch_all_invoices():
    invoices=[]
    filter_param=urllib.parse.quote(f'[{{"field":"date","operator":"gteq","value":"{START_DATE}"}}]')
    cursor=None
    while True:
        path=f"/customer_invoices?filter={filter_param}&sort=date&direction=asc&per_page=100"
        if cursor: path+=f"&cursor={urllib.parse.quote(cursor)}"
        data=pl_get(path)
        invoices.extend(data.get("items",[]))
        if not data.get("has_more"): break
        cursor=data.get("next_cursor")
        time.sleep(0.15)
    return invoices

def sheets_get(gtoken,range_name):
    url=f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}"
    req=urllib.request.Request(url,headers={"Authorization":f"Bearer {gtoken}"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read()).get("values",[])

def sheets_append(gtoken,range_name,values):
    url=f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    data=json.dumps({"values":values}).encode()
    req=urllib.request.Request(url,data=data,headers={"Authorization":f"Bearer {gtoken}","Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req) as r: return json.loads(r.read())

def sheets_batch_put(gtoken,updates):
    url=f"{SHEETS_BASE}/{SHEET_ID}/values:batchUpdate"
    body={"valueInputOption":"RAW","data":[{"range":r,"values":v} for r,v in updates]}
    data=json.dumps(body).encode()
    req=urllib.request.Request(url,data=data,headers={"Authorization":f"Bearer {gtoken}","Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req) as r: return json.loads(r.read())

def fmt(val):
    try: return round(float(val),2)
    except: return ""

def date_iso_to_fr(iso):
    p=iso.split("-"); return f"{p[2]}/{p[1]}/{p[0]}"

def month_from_fr_date(date_fr):
    try: return MONTH_NAMES.get(int(date_fr.split("/")[1]),"")
    except: return ""

def detect_invoice_type(line_items,inv_label):
    labels=" ".join(l.get("label","").lower() for l in line_items)
    all_labels=labels+" "+inv_label.lower()
    has_negative=any(float(l.get("currency_amount_before_tax",0))<0 for l in line_items)
    is_acompte=any(k in all_labels for k in ("acompte","deposit","avance","à-valoir","a-valoir"))
    is_partielle=any(k in all_labels for k in ("partiel","partielle","situation","intermédiaire","progress","tranche"))
    if is_acompte: return "Acompte"
    if has_negative: return "Clôture"
    if is_partielle: return "Partielle"
    return ""

def auto_prise_en_compte(inv_type,inv_status,date_fr):
    if inv_type in ("Acompte","Partielle") or inv_status=="cancelled": return "Exclure"
    return month_from_fr_date(date_fr)

def enrich(inv,customer_cache,quote_cache):
    cid=inv["customer"]["id"]
    if cid not in customer_cache:
        customer_cache[cid]=pl_get(f"/customers/{cid}").get("name","N/A"); time.sleep(0.08)
    cats=pl_get(f"/customer_invoices/{inv['id']}/categories")
    cat_str=", ".join(c["label"] for c in cats.get("items",[])or"N/A"); time.sleep(0.08)
    lines=pl_get(f"/customer_invoices/{inv['id']}/invoice_lines")
    line_items=lines.get("items",[])
    inv_type=detect_invoice_type(line_items,inv.get("label","")); time.sleep(0.08)
    quote_ht=""
    if inv.get("quote"):
        qid=inv["quote"]["id"]
        if qid not in quote_cache:
            quote_cache[qid]=pl_get(f"/quotes/{qid}").get("currency_amount_before_tax",""); time.sleep(0.08)
        quote_ht=fmt(quote_cache[qid])
    statut=STATUS_MAP.get(inv["status"],inv["status"])
    date_fr=date_iso_to_fr(inv["date"])
    pec=auto_prise_en_compte(inv_type,inv["status"],date_fr)
    return [date_fr,customer_cache[cid],inv["invoice_number"],cat_str,fmt(inv["currency_amount_before_tax"]),statut,inv_type,quote_ht,pec]

def main():
    global USER_EMAIL
    if "--email" in sys.argv:
        idx=sys.argv.index("--email")
        if idx+1<len(sys.argv): USER_EMAIL=sys.argv[idx+1]
    now_str=datetime.now().strftime("%d/%m/%Y %H:%M")
    print("="*62)
    print("  Import incrémental Pennylane → Google Sheet — PulpMeUp")
    print(f"  Run : {now_str}  |  Opérateur : {USER_EMAIL}")
    print("="*62)
    print("\n[1/7] Authentification Google..."); gtoken=get_google_token(); print("      ✓ Token OK")
    print(f"\n[2/7] Lecture des factures existantes...")
    existing_rows=sheets_get(gtoken,f"{INVOICE_TAB}!A2:L2000")
    existing_nums={};existing_status={};j_pending=[]
    for i,row in enumerate(existing_rows):
        row=row+[""]*(12-len(row)); num=row[2]
        if num: existing_nums[num]=i; existing_status[num]=row[5]
        if row[9] and not row[10]: j_pending.append(i+2)
    print(f"      {len(existing_nums)} factures présentes | {len(j_pending)} modif équipe non tracée(s)")
    print(f"\n[3/7] Récupération Pennylane (depuis {START_DATE})...")
    all_invoices=fetch_all_invoices(); print(f"      {len(all_invoices)} factures récupérées")
    print("\n[4/7] Détection des avoirs...")
    avoir_ids=set();credited_ids=set();credited_in_sheet={};all_by_id={inv["id"]:inv for inv in all_invoices}
    for inv in all_invoices:
        if inv.get("credited_invoice"):
            avoir_ids.add(inv["id"]); orig_id=inv["credited_invoice"]["id"]; orig_inv=all_by_id.get(orig_id)
            if orig_inv:
                orig_num=orig_inv["invoice_number"]; credited_ids.add(orig_id)
                if orig_num in existing_nums: credited_in_sheet[orig_num]=existing_nums[orig_num]
    to_skip=avoir_ids|credited_ids
    print(f"      Avoirs : {len(avoir_ids)}  |  Factures créditées : {len(credited_ids)}")
    new_invoices=[inv for inv in all_invoices if inv["invoice_number"] not in existing_nums and inv["id"] not in to_skip]
    print(f"\n[5/7] Nouvelles factures : {len(new_invoices)}")
    added=0
    if new_invoices:
        customer_cache,quote_cache,new_rows={},{},[]
        for i,inv in enumerate(new_invoices):
            new_rows.append(enrich(inv,customer_cache,quote_cache))
            if (i+1)%10==0: print(f"      {i+1}/{len(new_invoices)} traitées...")
        result=sheets_append(gtoken,f"{INVOICE_TAB}!A:I",new_rows)
        added=result.get("updates",{}).get("updatedRows",len(new_rows)); print(f"      ✓ {added} ligne(s) ajoutée(s)")
    print("\n[6/7] Mise à jour des statuts...")
    status_updates=[]
    for inv in all_invoices:
        num=inv["invoice_number"]; new_st=STATUS_MAP.get(inv["status"],inv["status"])
        if num in existing_status and existing_status[num]!=new_st:
            sheet_row=existing_nums[num]+2; status_updates.append((f"{INVOICE_TAB}!F{sheet_row}",[[new_st]]))
            print(f"      Ligne {sheet_row} ({num}) : {existing_status[num]} → {new_st}")
    if status_updates: sheets_batch_put(gtoken,status_updates); print(f"      ✓ {len(status_updates)} statut(s) mis à jour")
    else: print("      Aucun statut à modifier")
    if credited_in_sheet:
        avoir_updates=[(f"{INVOICE_TAB}!I{row_idx+2}",[["Exclure"]]) for _,row_idx in credited_in_sheet.items()]
        sheets_batch_put(gtoken,avoir_updates)
    print("\n[7/7] Tracking modifications équipe...")
    if j_pending:
        tracking_updates=[]
        for sheet_row in j_pending:
            tracking_updates+=[(f"{INVOICE_TAB}!K{sheet_row}",[[now_str]]),(f"{INVOICE_TAB}!L{sheet_row}",[[USER_EMAIL]])]
        sheets_batch_put(gtoken,tracking_updates); print(f"      ✓ {len(j_pending)} ligne(s) tracée(s)")
    else: print("      Aucune modification non tracée")
    print("\n"+"="*62)
    print(f"  + {added} facture(s)  ↻ {len(status_updates)} statut(s)  ✎ {len(j_pending)} modif(s)")
    if credited_in_sheet: print(f"  ✗ {len(credited_in_sheet)} facture(s) créditée(s) → Exclure")
    print("="*62)

if __name__=="__main__":
    main()
