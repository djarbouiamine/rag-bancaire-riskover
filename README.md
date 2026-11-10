# RAG Bancaire - Assistant IA pour Analyste Crédit (RisKover)

Projet tutoré INDP3 - SIA (SUP'COM × RisKover) : agent IA d'aide à la décision bancaire.

## Objectif
1. **Étape 1 (actuelle) - Text-to-SQL** : l'analyste pose une question en français, le modèle écrit une requête SQL, elle est validée puis exécutée en lecture seule sur la base RisKover, et une réponse est rédigée avec sa source.
2. **Étape 2 (future) - RAG + agent** : recherche dans les documents (rapports, emails, notes) et agent qui choisit l'outil (SQL ou RAG).

---

## Architecture

- **PC Windows** : code Python, base SQLite de test `db/riskover.db`, journal `logs/queries.log`.
- **Serveur GPU SUP'COM** : modèles servis par **Ollama**, joints par un **tunnel SSH** sur `http://localhost:11435`.

| Rôle | Modèle | Variable |
|---|---|---|
| Écrire le SQL | `qwen2.5-coder:14b` | `SQL_MODEL` |
| Rédiger la réponse (et l'agent plus tard) | `qwen3:14b` | `MODEL_NAME` |

---

## Structure du projet

```text
rag_bancaire/
├── config.py                    # Réglages : chemins, URL Ollama, noms des modèles
├── requirements.txt             # Dépendances Python
├── db/
│   └── riskover.db              # Base de test (7 tables, données fictives)
├── logs/
│   └── queries.log              # Journal d'audit de toutes les requêtes SQL
├── src/
│   ├── create_riskover_db.py    # Crée la petite base riskover.db (7 tables)
│   ├── create_large_db.py       # Crée la grande base banque_large.db (100 tables)
│   ├── mschema.py               # Décrit la base au modèle (format M-Schema)
│   ├── schema_linking.py        # Grandes bases : choix des tables (bge-m3) + recherche des valeurs
│   ├── text2sql.py              # Moteur : question → SQL validé → résultat → réponse
│   └── bench_text2sql.py        # Compare des modèles (et le linking) sur les questions de test
├── tests/
│   ├── questions_riskover.csv   # 25 questions, petite base
│   ├── questions_large.csv      # 25 questions par catégorie, grande base
│   ├── test_text2sql.py         # 18 tests hors ligne (faux modèle)
│   └── test_schema_linking.py   # 7 tests hors ligne (faux embeddings)
├── benchmark_results/           # Résultats des benchmarks (CSV)
├── data/raw, data/clean, docs/  # Pour l'étape RAG
```

---

## Installation

```powershell
cd rag_bancaire
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Utilisation

**Fenêtre 1 - tunnel SSH** (à laisser ouverte) :
```powershell
ssh -N -L 11435:127.0.0.1:11434 <utilisateur>@<adresse-serveur>
```

**Fenêtre 2 - projet** :
```powershell
python src/create_riskover_db.py                      # une seule fois (--force pour recréer)
python -m unittest tests.test_text2sql -v             # tests sans serveur
python src/text2sql.py "Combien de clients à SFAX ?"  # une question
python src/text2sql.py                                # mode interactif
python src/bench_text2sql.py --models qwen2.5-coder:14b qwen3:14b gpt-oss:latest
```

---

## Grandes bases : schema linking

Sur une base de 100+ tables, le schéma complet (~22 000 tokens) ne tient pas dans le prompt.
`src/schema_linking.py` envoie au modèle **seulement les tables utiles** (choisies avec les
embeddings **bge-m3**) et les **valeurs réelles** trouvées dans la question (fautes et pluriels
acceptés). Activé automatiquement à partir de 20 tables (`LINKING_MIN_TABLES`).

```powershell
python src/create_large_db.py                 # base de test : 100 tables, 425 000 lignes
python src/schema_linking.py "credits en contentieu a sfax medina"
python src/text2sql.py --db db/banque_large.db "combien de credits en contentieu a sfax medina ?"
python src/bench_text2sql.py --db db/banque_large.db --file tests/questions_large.csv `
       --models qwen2.5-coder:14b --linking off on
```

Résultats (10/11/2026, 25 questions, base de 100 tables) :

| Modèle | Sans linking | Avec linking | Temps moyen (avec) |
|---|---|---|---|
| qwen2.5-coder:14b | 5/25 | **23/25** | 2.9 s |
| gpt-oss (20b) | 5/25 | **23/25** | 4.8 s |

---

## Tests

```powershell
python -m unittest discover -s tests -v       # 25 tests hors ligne (aucun serveur nécessaire)
```

---

## Règles du text-to-SQL

1. Une seule requête SELECT, jamais INSERT/UPDATE/DELETE/DROP.
2. Texte : toujours `norm(colonne) = norm('valeur')` (ignore majuscules/accents).
3. Colonne avec Examples → valeur de la liste, jamais inventée.
4. Identifiants comparés avec `norm()` aussi.
5. Jointures seulement via les 【Foreign keys】, colonnes préfixées par l'alias.
6. Pas de `SELECT *`, `LIMIT 200`.
7. Question impossible → `SELECT 'HORS_BASE' AS erreur;`

Ces règles sont dans le prompt **et** vérifiées par le code : `norm()` est ajoutée si le modèle l'oublie, `LIMIT 200` est imposé, les valeurs inconnues sont renvoyées au modèle avec la liste réelle.

## Sécurité
- **Données 100 % fictives**, clients pseudonymisés (`CLI_001`…).
- **Lecture seule** : SQLite ouvert en `?mode=ro` + autorisation SQLite qui refuse tout sauf la lecture des tables autorisées.
- **Traçabilité** : chaque requête est horodatée dans `logs/queries.log`.
