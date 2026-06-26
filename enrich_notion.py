#!/usr/bin/env python3
"""
Enrichissement Notion → Google Sheet PulpMeUp
Colonnes M (Date création Notion) et N (Date de Livraison)
Tokens lus depuis les variables d'environnement :
  NOTION_TOKEN  — token API Notion
  SA_JSON       — contenu du service_account.json en base64
"""
import json, sys, time, base64, subprocess, urllib.request, urllib.parse, re, os
from datetime import datetime

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID = "75e8be75985f4c78be0653629df96b6a"
SA_FILE      = "/tmp/sa_pulpmeup.json"
SHEET_ID     = "1KO3fnOlxsnEedl8NUH93r9aIUqwc-3zJYrjwsMDx0yc"
INVOICE_TAB  = "(Auto)Liste des factures"
NOTION_BASE  = "https://api.notion.com/v1"
SHEETS_BASE  = "https://sheets.googleapis.com/v4/spreadsheets"

def load_sa():
    if not os.path.exists(SA_FILE):
        sa_b64=os.environ.get("SA_JSON","")
        if not sa_b64: raise SystemExit("Variable SA_JSON manquante.")
        with open(SA_FILE,"w") as f: f.write(base64.b64decode(sa_b64).decode())

def get_google_token():
    load_sa()
    sa=json.load(open(SA_FILE))
    key_file="/tmp/_sa_key_notion.pem"
    with open(key_file,"w") as f: f.write(sa["private_key"])
    def b64url(d):
        if isinstance(d,str): d=d.encode()
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    now=int(time.time())
    h=b64url(json.dumps({"alg":"RS256","typ":"JWT"}))
    p=b64url(json.dumps({"iss":sa["client_email"],"scope":"https://www.googleapis.com/auth/spreadsheets","aud":sa["token_uri"],"iat":now,"exp":now+3600}))
    msg=f"{h}.{p}"
    with open("/tmp/_jwt_notion.txt","w") as f: f.write(msg)
    sig=subprocess.run(["openssl","dgst","-sha256","-sign",key_file,"/tmp/_jwt_notion.txt"],capture_output=True)
    jwt=f"{msg}.{b64url(sig.stdout)}"
    body=urllib.parse.urlencode({"grant_type":"urn:ietf:params:oauth:grant-type:jwt-bearer","assertion":jwt}).encode()
    req=urllib.request.Request(sa["token_uri"],data=body,headers={"Content-Type":"application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read())["access_token"]

def notion_headers():
    return {"Authorization":f"Bearer {NOTION_TOKEN}","Notion-Version":"2022-06-28","Content-Type":"application/json"}

def fetch_all_notion_projects():
    projects=[];cursor=None
    while True:
        body={"page_size":100}
        if cursor: body["start_cursor"]=cursor
        req=urllib.request.Request(f"{NOTION_BASE}/databases/{NOTION_DB_ID}/query",data=json.dumps(body).encode(),headers=notion_headers(),method="POST")
        with urllib.request.urlopen(req) as r: data=json.loads(r.read())
        projects.extend(data.get("results",[]))
        if not data.get("has_more"): break
        cursor=data.get("next_cursor"); time.sleep(0.1)
    return projects

def parse_project(page):
    props=page["properties"]
    title_parts=props.get("Name",{}).get("title",[])
    title=title_parts[0]["plain_text"] if title_parts else ""
    livraison_prop=props.get("Date de Livraison",{}).get("date")
    livraison_iso=livraison_prop["start"] if livraison_prop else ""
    return {"title":title,"created":page["created_time"][:10],"livraison":livraison_iso}

def normalize(s):
    s=re.sub(r"[^\w\s]"," ",s,flags=re.UNICODE)
    return re.sub(r"\s+"," ",s).strip().lower()

def sheets_get(gtoken,range_name):
    url=f"{SHEETS_BASE}/{SHEET_ID}/values/{urllib.parse.quote(range_name)}"
    req=urllib.request.Request(url,headers={"Authorization":f"Bearer {gtoken}"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read()).get("values",[])

def sheets_batch_put(gtoken,updates):
    url=f"{SHEETS_BASE}/{SHEET_ID}/values:batchUpdate"
    body={"valueInputOption":"RAW","data":[{"range":r,"values":v} for r,v in updates]}
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={"Authorization":f"Bearer {gtoken}","Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req) as r: return json.loads(r.read())

def load_mapping(gtoken):
    rows=sheets_get(gtoken,"Mapping Clients!A2:B500")
    return {row[0].strip().upper():row[1].strip() for row in rows if len(row)>=2 and row[0].strip() and row[1].strip()}

def find_project(client_name,projects,mapping=None):
    if mapping:
        mapped=mapping.get(client_name.strip().upper())
        if mapped: client_name=mapped
    needle=normalize(client_name)
    if not needle: return None
    matches=[p for p in projects if re.search(r"\b"+re.escape(needle)+r"\b",normalize(p["title"]))]
    if not matches: matches=[p for p in projects if needle in normalize(p["title"])]
    if not matches: return None
    matches.sort(key=lambda x:x["created"],reverse=True)
    return matches[0]

def iso_to_fr(iso):
    if not iso: return ""
    p=iso.split("-"); return f"{p[2]}/{p[1]}/{p[0]}"

def main():
    print("="*62)
    print("  Enrichissement Notion → Google Sheet — PulpMeUp")
    print(f"  Run : {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("="*62)
    print("\n[1/4] Auth Google..."); gtoken=get_google_token(); print("      ✓ OK")
    print("\n[2/4] Lecture du Sheet...")
    rows=sheets_get(gtoken,f"{INVOICE_TAB}!A2:N2000"); print(f"      {len(rows)} lignes")
    print("\n[3/4] Projets Notion...")
    projects=[parse_project(p) for p in fetch_all_notion_projects()]
    print(f"      {len(projects)} projets")
    mapping=load_mapping(gtoken); print(f"      {len(mapping)} correspondance(s) manuelles")
    print("\n[4/4] Mise à jour M/N...")
    updates=[];matched=0;not_matched=[]
    for i,row in enumerate(rows):
        row=row+[""]*(14-len(row)); client=row[1]; sheet_row=i+2
        if not client: continue
        p=find_project(client,projects,mapping)
        if not p: not_matched.append(f"Ligne {sheet_row} — {client}"); continue
        new_m=iso_to_fr(p["created"]); new_n=iso_to_fr(p["livraison"])
        if new_m!=row[12] or new_n!=row[13]:
            updates+=[(f"{INVOICE_TAB}!M{sheet_row}",[[new_m]]),(f"{INVOICE_TAB}!N{sheet_row}",[[new_n]])]
            print(f"      Ligne {sheet_row} ({client}) → {new_m} | {new_n or '—'}"); matched+=1
    if updates: sheets_batch_put(gtoken,updates)
    print(f"\n{'='*62}")
    print(f"  ✓ {matched} ligne(s) enrichie(s)")
    if not_matched:
        print(f"  ✗ {len(not_matched)} non-matchés (à compléter dans 'Mapping Clients'):")
        for nm in not_matched[:5]: print(f"      {nm}")
        if len(not_matched)>5: print(f"      ... et {len(not_matched)-5} autres")
    print("="*62)

if __name__=="__main__":
    main()
