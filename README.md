# PulpMeUp Primes 2026

Routine hebdomadaire d'import des factures Pennylane → Google Sheet + enrichissement Notion.

## Variables d'environnement requises

| Variable | Description |
|---|---|
| `PENNYLANE_TOKEN` | Token API Pennylane |
| `NOTION_TOKEN` | Token intégration Notion |
| `SA_JSON` | Contenu du `service_account.json` encodé en base64 |

## Fichiers

- `import_factures.py` — import incrémental Pennylane → Sheet
- `enrich_notion.py` — enrichissement dates Notion → colonnes M/N du Sheet
- `.claude/commands/import-factures.md` — skill `/import-factures`
