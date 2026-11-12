# Text-to-SQL RisKover - Assistant IA pour analyste crédit

Projet tutoré INDP3 - SIA (SUP'COM × RisKover).

L'analyste pose une question en français. Le système écrit une requête SQL, la vérifie,
l'exécute **en lecture seule** sur la base, puis rédige une réponse qui cite sa source.
C'est l'**outil SQL** de l'agent prévu dans le document de recherche (section 5.4) ;
l'outil RAG (documents) sera l'étape suivante.

---

## 1. Modèles retenus et résultats

| Rôle | Modèle (Ollama, serveur GPU SUP'COM) |
|---|---|
| Écrire la requête SQL | **`qwen2.5-coder:14b`** |
| Choisir les tables utiles (schema linking) | **`bge-m3`** (embeddings multilingues) |
| Rédiger la réponse en français | **`qwen3:14b`** |

**Résultats de `qwen2.5-coder:14b`** (11/11/2026, schema linking activé, base `riskover.db`) :

| Jeu de questions | Score | Temps moyen / question |
|---|---|---|
| 25 questions générales (`tests/questions_riskover.csv`) | **25/25** | 1.8 s |
| 15 questions avec jointures, 2 à 4 tables (`tests/questions_joins.csv`) | **15/15** | 2.5 s |
| 12 questions sur une base **jamais vue** (hôpital, autre domaine, noms anglais) | **12/12** | 2.0 s |
| Base de test de 100 tables / 425 000 lignes | **23/25** (5/25 sans schema linking) | 2.9 s |

**Pourquoi ce modèle ?** Comparé à `gpt-oss` (20b) et `qwen3:14b` sur les mêmes questions,
il obtient un score équivalent (à une question près), il est **de 1,7 à 13 fois plus rapide**,
et il n'a jamais inventé de réponse sur les questions pièges (`gpt-oss` a répondu à
« chiffre d'affaires » avec la somme des crédits).

---

## 2. Fonctionnement

```
Question en français
   │
   ├─ 1. Schema linking (bge-m3) : tables utiles + valeurs réelles trouvées dans la question
   ├─ 2. Prompt : 7 règles + description des tables (format M-Schema) + valeurs trouvées
   ├─ 3. qwen2.5-coder écrit la requête SQL
   ├─ 4. Vérifications et corrections automatiques par le code (voir tableau des règles)
   ├─ 5. Exécution en lecture seule  ──► erreur ? le modèle reçoit l'erreur et corrige (3 fois max)
   └─ 6. qwen3 rédige la réponse à partir du résultat, avec la source
```

### Les 7 règles : dans le prompt ET vérifiées par le code

| Règle | Fonction qui la fait respecter |
|---|---|
| 1. Une seule requête SELECT, jamais INSERT/UPDATE/DELETE/DROP | `validate_sql()` + base ouverte en lecture seule + `connect()` |
| 2. Texte : toujours `norm(colonne) = norm('valeur')` (majuscules/accents ignorés) | `apply_norm()` ajoute `norm()` si le modèle l'oublie |
| 3. Colonne avec Examples → valeur de la liste, jamais inventée | `find_unknown_values()` |
| 4. Identifiants comparés avec `norm()` aussi | `apply_norm()` |
| 5. Jointures seulement via les 【Foreign keys】 | `check_joins()` renvoie le bon chemin au modèle |
| 6. Pas de `SELECT *`, `LIMIT 200` | `validate_sql()`, `enforce_limit()`, `hides_ties()` |
| 7. Question impossible → `SELECT 'HORS_BASE' AS erreur;` | `is_hors_base()` |

---

## 3. Rôle de chaque fichier et de chaque fonction

```text
rag_bancaire/
├── config.py                    Réglages
├── requirements.txt             Dépendances (requests)
├── src/
│   ├── create_riskover_db.py    Crée la base de test
│   ├── mschema.py               Décrit la base au modèle
│   ├── schema_linking.py        Choisit les tables et les valeurs utiles
│   ├── text2sql.py              Moteur principal
│   ├── compare_sql.py           Test manuel : ma requête SQL vs celle du modèle
│   └── bench_text2sql.py        Évaluation automatique sur un fichier de questions
└── tests/
    ├── questions_riskover.csv   25 questions + requête de référence
    ├── questions_joins.csv      15 questions avec jointures
    ├── test_text2sql.py         Tests du moteur (sans serveur)
    ├── test_schema_linking.py   Tests du schema linking (sans serveur)
    └── test_compare_sql.py      Tests de l'outil de comparaison (sans serveur)
```
Créés automatiquement à l'exécution (non versionnés) : `db/riskover.db`, `data/cache/`
(vecteurs bge-m3), `logs/queries.log` (journal d'audit).

### `config.py` - réglages centralisés
Chemin de la base, adresse du serveur Ollama (tunnel SSH, port 11435), noms des modèles
(`SQL_MODEL`, `MODEL_NAME`, `EMBED_MODEL`), schema linking (`LINKING_MIN_TABLES`),
limites de lignes (`MAX_ROWS`, `SHOW_ROWS`, `ANSWER_ROWS`). Tout se change aussi par
variable d'environnement.

### `src/create_riskover_db.py` - base de test
Crée `db/riskover.db` : **7 tables reliées par clés étrangères, données 100 % fictives**
(entreprises 30, clients 60, crédits 131, garanties 96, covenants 68, incidents de paiement 55,
documents 50). Graine fixe : la même base est recréée à chaque fois.

| Fonction | Rôle |
|---|---|
| `build()` | Crée les tables et génère les données |
| `main()` | Crée le fichier, vérifie les clés étrangères |

### `src/mschema.py` - description de la base pour le modèle
Le modèle ne voit jamais les données : il reçoit une description texte au format **M-Schema**
(tables, colonnes, descriptions, valeurs possibles, clés étrangères).

| Fonction | Rôle |
|---|---|
| `DbSchema` | Lit la base une fois : tables, colonnes, clés étrangères, colonnes texte |
| `DbSchema.to_mschema(tables)` | Écrit le M-Schema de toute la base ou seulement des tables choisies |
| `DbSchema.examples_for_column()` | Valeurs réelles d'une colonne (≤ 20 valeurs → « Examples ») |
| `DbSchema.table_doc()` / `column_doc()` | Description métier d'une table / colonne |

### `src/schema_linking.py` - choisir les tables et les valeurs utiles
Avant d'appeler le modèle SQL, on sélectionne ce qui est utile à la question.

| Fonction | Rôle |
|---|---|
| `Embedder.embed()` | Transforme des textes en vecteurs avec **bge-m3** (cache disque) |
| `SchemaLinker.score_tables()` | Score de chaque table : similarité de sens (cosinus) + mots communs + bonus table reliée − malus table technique |
| `SchemaLinker.link()` | Garde les meilleures tables et ajoute les tables « pont » pour les relier (plus court chemin de clés étrangères) |
| `ValueIndex.search()` | Retrouve dans la question les **valeurs réelles** de la base, même avec fautes ou pluriels (« contentieu » → `'Contentieux'`) |
| `norm()`, `tokens()`, `keywords()` | Normalisation du texte (minuscules, sans accents, mots utiles) |

### `src/text2sql.py` - moteur principal

| Fonction | Rôle |
|---|---|
| `solve()` | **Chef d'orchestre** : linking → prompt → modèle → vérifications → exécution → corrections → réponse |
| `build_prompt()` | Construit le prompt : 7 règles + exemples + M-Schema + valeurs trouvées + question |
| `ask_llm()` | Appelle le modèle sur le serveur Ollama |
| `extract_sql()` | Extrait la requête de la réponse du modèle |
| `validate_sql()` | Sécurité : un seul SELECT, aucun mot interdit, pas de `SELECT *`, pas de commentaire |
| `apply_norm()` | Ajoute `norm()` aux comparaisons de texte (majuscules / accents ignorés) |
| `fix_empty_values()` | `colonne = ''` → `colonne IS NULL` (« aucun », « sans ») |
| `enforce_limit()` | Ajoute ou réduit le `LIMIT` |
| `check_joins()` + `fk_path()` | Refuse une jointure qui n'est pas une clé étrangère et donne le bon chemin |
| `hides_ties()` | Refuse `LIMIT 1` sur « le plus / le moins » (cacherait les ex aequo) |
| `find_unknown_values()` | Signale une valeur qui n'existe pas dans la base |
| `is_hors_base()` | Détecte la réponse « HORS_BASE » |
| `connect()` | Ouvre la base **en lecture seule**, ajoute `norm()`, bloque tout sauf la lecture |
| `run_select()` | Exécute la requête validée et la journalise |
| `log_query()` | Écrit chaque requête dans `logs/queries.log` (traçabilité) |
| `write_answer()` | Fait rédiger la réponse en français par qwen3, sans inventer de chiffres |
| `get_schema()`, `get_linking_tools()` | Chargent une fois le schéma et les outils de linking |
| `set_limits()`, `main()` | Options `--limit` / `--show` et ligne de commande |

### `src/compare_sql.py` - test manuel
J'écris une question **et ma propre requête SQL** (la bonne réponse) ; le modèle reçoit
**seulement la question** ; les deux résultats sont comparés (✔ / ✘).

| Fonction | Rôle |
|---|---|
| `compare_one()` | Exécute ma requête, interroge le modèle, affiche les deux résultats et le verdict |
| `run_reference()` | Exécute ma requête en lecture seule |
| `verdict_for()` | Même résultat ? (tolère colonnes en plus et arrondis) |
| `save_question()` | Enregistre la question dans `tests/mes_questions.csv` pour le benchmark |

### `src/bench_text2sql.py` - évaluation automatique
Pose toutes les questions d'un fichier, exécute la requête de référence, compare les
**résultats** (pas le texte SQL) et donne le score par modèle et par catégorie (fichier CSV).

| Fonction | Rôle |
|---|---|
| `compare()` | Compare résultat du modèle et résultat attendu (colonnes en plus et arrondis acceptés) |
| `says_nothing()` | Résultat vide ou 0 = « rien trouvé » (questions pièges) |
| `ref_tables()` | Tables de la requête de référence (pour vérifier le schema linking) |
| `main()` | Boucle sur modèles / modes de linking / questions, affiche le résumé |

---

## 4. Installation et utilisation

```powershell
cd rag_bancaire
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python src/create_riskover_db.py          # crée la base (une seule fois)
```

**Fenêtre 1 - tunnel SSH vers le serveur GPU** (à laisser ouverte) :
```powershell
ssh -N -o ServerAliveInterval=60 -L 11435:127.0.0.1:11434 <utilisateur>@<adresse-serveur>
```

**Fenêtre 2 - projet** :
```powershell
python src/text2sql.py                                          # poser des questions
python src/text2sql.py "Combien de clients à SFAX ?"            # une question
python src/compare_sql.py                                       # ma requête SQL vs le modèle
python src/bench_text2sql.py                                    # 25 questions
python src/bench_text2sql.py --file tests/questions_joins.csv   # 15 questions avec jointures
python -m unittest discover -s tests -v                         # 38 tests, sans serveur
```

| Option | Rôle | Défaut |
|---|---|---|
| `--model` | Modèle SQL | `qwen2.5-coder:14b` |
| `--db` | Base SQLite à interroger | `db/riskover.db` |
| `--linking on/off` | Schema linking | activé |
| `--limit N` / `--show N` | Lignes renvoyées / affichées | 200 / 10 |
| `--no-answer` | Sans réponse rédigée | - |

---

## 5. Autre base, sécurité, limites

- **N'importe quelle base SQLite** : `--db chemin/base.db`, sans configuration (testé : 12/12
  sur une base hôpital jamais vue). Si les colonnes ont des noms codés (`MT_ACC`), un fichier
  optionnel `<base>.docs.json` les décrit.
- **Sécurité** : données fictives, base ouverte en lecture seule, toute requête journalisée.
- **Limites** :
  - SQLite uniquement (PostgreSQL / SQL Server : adapter la connexion et `norm()`) ;
  - aucun système text-to-SQL n'est fiable à 100 % : l'analyste vérifie la requête et la source affichées.
