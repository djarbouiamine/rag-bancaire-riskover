"""
text2sql.py - Outil text-to-SQL de l'agent RisKover (question en francais -> SELECT -> resultat).

Chaine :
  question -> prompt (regles + M-Schema) -> LLM (Ollama) -> requete SQL
           -> validation (1 seul SELECT, pas de *, tables autorisees)
           -> reecriture norm() (majuscules / accents ignores) + LIMIT 200
           -> verification des valeurs (Examples) -> execution en lecture seule
           -> si erreur : le modele recoit l'erreur et corrige (MAX_RETRIES fois)
           -> journalisation dans logs/queries.log

norm(x) est une fonction Python enregistree dans SQLite :
    norm('  SANTÉ ') == norm('sante') == 'sante'
Donc norm(c.secteur) = norm('SANTE') trouve 'Santé', et norm(c.client_id) = norm('cli_009')
trouve 'CLI_009'.

Usage (depuis rag_bancaire) :
    python src/text2sql.py "Combien de clients a SFAX ?"
    python src/text2sql.py --model qwen2.5-coder:14b
    python src/text2sql.py --show-prompt "Quels credits sont en retard ?"
    python src/text2sql.py --no-answer "..."   (sans redaction de la reponse en francais)
"""
import argparse
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from src.mschema import DbSchema
except ImportError:  # lancement direct depuis src/
    from mschema import DbSchema

import config

OLLAMA_URL = config.OLLAMA_URL
SQL_MODEL = config.SQL_MODEL
ANSWER_MODEL = config.MODEL_NAME
DB_PATH = Path(config.RISKOVER_DB_PATH)
LOG_FILE = Path(config.QUERY_LOG_PATH)

# Limites (modifiables dans config.py, par variable d'environnement ou par option --limit / --show)
MAX_ROWS = config.MAX_ROWS            # lignes max renvoyees par la base (LIMIT ajoute au SQL)
SHOW_ROWS = config.SHOW_ROWS          # lignes affichees a l'ecran
ANSWER_ROWS = config.ANSWER_ROWS      # lignes donnees au modele qui redige la reponse
MAX_RETRIES = 2
HORS_BASE = "HORS_BASE"

# Tables que le modele a le droit de lire (None = toutes les tables de la base)
ALLOWED_TABLES: Optional[List[str]] = None

FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create", "attach",
             "detach", "pragma", "replace", "truncate", "vacuum", "reindex",
             "analyze", "exec", "execute", "grant", "revoke", "load_extension")

_STRING = r"'(?:[^']|'')*'"


# =============================================================================
#  norm() : comparaison sans majuscules ni accents
# =============================================================================
def norm(x):
    """Minuscules, sans accents, sans espaces en trop. None reste None."""
    if x is None:
        return None
    s = unicodedata.normalize("NFD", str(x).strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def connect(db_path: Path = None, allowed_tables: Optional[List[str]] = None,
            guard: bool = True) -> sqlite3.Connection:
    """Connexion en LECTURE SEULE avec norm() enregistree.
    guard=True : un authorizer SQLite refuse tout sauf la lecture des tables autorisees."""
    db_path = Path(db_path or DB_PATH)
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.create_function("norm", 1, norm, deterministic=True)
    if guard:
        allowed = {t.lower() for t in allowed_tables} if allowed_tables else None

        def authorizer(action, arg1, arg2, dbname, source):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                          sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                table = (arg1 or "").lower()
                if table.startswith("sqlite_"):
                    return sqlite3.SQLITE_DENY
                if allowed is None or table in allowed:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        con.set_authorizer(authorizer)
    return con


# =============================================================================
#  Prompt
# =============================================================================
RULES = """\
1. Une seule requête SELECT, jamais INSERT/UPDATE/DELETE/DROP.
2. Texte : TOUJOURS norm(colonne) = norm('valeur')  (ignore majuscules/accents)
3. Colonne avec Examples → utiliser une valeur de la liste, jamais inventée.
4. Identifiants comparés avec norm() aussi.
5. Jointures seulement via les 【Foreign keys】, colonnes préfixées par l'alias.
6. Pas de SELECT *, LIMIT 200.
7. Question impossible → SELECT 'HORS_BASE' AS erreur;"""

EXAMPLES = """\
(exemples sur une petite base clients / credits / garanties : adapte-les aux tables du schéma)
Question : Combien de clients à SFAX ?
```sql
SELECT COUNT(c.client_id) AS nb_clients FROM clients AS c WHERE norm(c.ville) = norm('Sfax') LIMIT 200;
```
Question : crédits du client cli_007 avec leur garantie
```sql
SELECT cr.credit_id, cr.type_credit, cr.montant, g.type_garantie, g.valeur FROM credits AS cr LEFT JOIN garanties AS g ON g.credit_id = cr.credit_id WHERE norm(cr.client_id) = norm('CLI_007') LIMIT 200;
```
Question : Quel est le chiffre d'affaires des clients ?
```sql
SELECT 'HORS_BASE' AS erreur;
```"""


def build_prompt(question: str, mschema: str, dialect: str = "SQLite",
                 values: Optional[List[Dict]] = None) -> str:
    found = ""
    if values:
        found = ("【Valeurs trouvées dans la base】 (valeurs réelles proches des mots de la "
                 "question, à utiliser telles quelles si elles correspondent au sens)\n"
                 + "\n".join(f"- {v['table']}.{v['colonne']} = '{v['valeur']}'  "
                             f"(question : « {v['texte_question']} »)" for v in values)
                 + "\n\n")
    rules = RULES.replace("LIMIT 200", f"LIMIT {MAX_ROWS}")
    examples = EXAMPLES.replace("LIMIT 200", f"LIMIT {MAX_ROWS}")
    return (
        f"Tu es un expert {dialect}. Lis le schéma de la base (format M-Schema) puis écris "
        f"UNE requête {dialect} qui répond à la question de l'analyste.\n"
        "Réponds uniquement avec la requête dans un bloc ```sql```, sans explication.\n\n"
        f"【Règles】\n{rules}\n"
        "- norm() est une fonction disponible dans la base : minuscules + sans accents. "
        "Utilise-la seulement pour COMPARER (WHERE, ON, HAVING), jamais sur les colonnes "
        "affichées après SELECT : les valeurs doivent rester telles qu'en base.\n"
        "- Ajoute seulement les filtres demandés dans la question, n'en invente pas.\n"
        "- Pour « le plus / le moins », renvoie toutes les égalités (sous-requête MAX/MIN), "
        "pas LIMIT 1.\n"
        "- Les dates sont du texte AAAA-MM-JJ ; utilise strftime() ou des comparaisons de texte.\n\n"
        f"【Exemples】\n{examples}\n\n"
        f"【Schéma de la base】\n{mschema}\n\n"
        f"{found}"
        f"【Question】\n{question}\n"
    )


# =============================================================================
#  Extraction, validation et reecriture du SQL
# =============================================================================
def extract_sql(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    m = re.search(r"```(?:sql|sqlite)?\s*(.*?)```", text, flags=re.S | re.I)
    if m:
        text = m.group(1)
    m = re.search(r"\b(with|select)\b.*", text, flags=re.S | re.I)
    sql = (m.group(0) if m else text).strip()
    return sql.rstrip().rstrip(";").strip()


def _mask_strings(sql: str) -> str:
    """Remplace le contenu des chaines par '' pour analyser la structure sans faux positifs."""
    return re.sub(_STRING, "''", sql)


def is_hors_base(sql: str) -> bool:
    return bool(re.match(r"^\s*select\s+'HORS_BASE'", sql, flags=re.I))


def validate_sql(sql: str) -> Tuple[bool, str]:
    """Regles 1 et 6 : un seul SELECT, aucun mot interdit, pas de SELECT *."""
    if not sql.strip():
        return False, "Requête vide."
    masked = _mask_strings(sql)
    if ";" in masked:
        return False, "Une seule requête est autorisée (point-virgule intermédiaire)."
    if "--" in masked or "/*" in masked:
        return False, "Les commentaires SQL sont interdits."
    first = re.match(r"^\s*([A-Za-z]+)", masked)
    if not first or first.group(1).lower() not in ("select", "with"):
        return False, "Seules les requêtes SELECT sont autorisées."
    low = masked.lower()
    for w in FORBIDDEN:
        if re.search(rf"\b{w}\b", low):
            return False, f"Mot-clé interdit : {w.upper()}."
    if re.search(r"\bselect\s+(?:distinct\s+|all\s+)?\*|\w\.\*", low):
        return False, "SELECT * est interdit : liste seulement les colonnes utiles."
    return True, ""


def enforce_limit(sql: str, max_rows: Optional[int] = None) -> str:
    """Regle 6 : LIMIT final ajoute s'il manque, ramene a max_rows s'il est plus grand.
    Un LIMIT plus petit ecrit par le modele (ex. « les 5 premiers » -> LIMIT 5) est garde."""
    max_rows = max_rows or MAX_ROWS
    m = re.search(r"\blimit\s+(\d+)(\s+offset\s+\d+)?\s*$", _mask_strings(sql), flags=re.I)
    if not m:
        return f"{sql} LIMIT {max_rows}"
    if int(m.group(1)) > max_rows:
        return sql[:m.start(1)] + str(max_rows) + sql[m.end(1):]
    return sql


def apply_norm(sql: str, text_columns) -> str:
    """Regles 2 et 4 : si le modele oublie norm(), on l'ajoute.
    colonne_texte = 'v'        -> norm(colonne_texte) = norm('v')
    colonne_texte IN ('a','b') -> norm(colonne_texte) IN (norm('a'), norm('b'))
    colonne_texte LIKE '%v%'   -> norm(colonne_texte) LIKE norm('%v%')
    norm(col) = 'v'            -> norm(col) = norm('v')
    Seules les colonnes TEXT sont concernees (jamais les montants ou les dates numeriques)."""
    text_columns = {c.lower() for c in text_columns}
    col_re = r"(?<![\w.'])((?:\w+\.)?\w+)"

    def is_text(col: str) -> bool:
        return col.split(".")[-1].lower() in text_columns

    def wrap_list(items: str) -> str:
        vals = re.findall(_STRING, items)
        return ", ".join(f"norm({v})" for v in vals)

    # col = 'v' / col != 'v' / col <> 'v'
    def eq(m):
        col, op, val = m.group(1), m.group(2), m.group(3)
        return f"norm({col}) {op} norm({val})" if is_text(col) else m.group(0)
    sql = re.sub(col_re + r"\s*(=|!=|<>)\s*(" + _STRING + ")", eq, sql)

    # col [NOT] IN ('a', 'b')
    def inn(m):
        col, op, items = m.group(1), m.group(2), m.group(3)
        if not is_text(col):
            return m.group(0)
        return f"norm({col}) {op.upper()} ({wrap_list(items)})"
    sql = re.sub(col_re + r"\s+((?:not\s+)?in)\s*\(\s*(" + _STRING + r"(?:\s*,\s*" + _STRING
                 + r")*)\s*\)", inn, sql, flags=re.I)

    # col [NOT] LIKE 'v'
    def like(m):
        col, op, val = m.group(1), m.group(2), m.group(3)
        return f"norm({col}) {op.upper()} norm({val})" if is_text(col) else m.group(0)
    sql = re.sub(col_re + r"\s+((?:not\s+)?like)\s+(" + _STRING + ")", like, sql, flags=re.I)

    # norm(col) = 'v' (valeur non normalisee)
    sql = re.sub(r"(norm\([^()]*\))\s*(=|!=|<>)\s*(" + _STRING + ")",
                 lambda m: f"{m.group(1)} {m.group(2)} norm({m.group(3)})", sql, flags=re.I)
    sql = re.sub(r"(norm\([^()]*\))\s+((?:not\s+)?in)\s*\(\s*(" + _STRING + r"(?:\s*,\s*"
                 + _STRING + r")*)\s*\)",
                 lambda m: f"{m.group(1)} {m.group(2).upper()} ({wrap_list(m.group(3))})",
                 sql, flags=re.I)
    return sql


def find_unknown_values(sql: str, schema: DbSchema) -> List[Dict]:
    """Regle 3 : chaque valeur comparee a une colonne avec Examples doit exister dans la liste."""
    pairs: List[Tuple[str, str]] = []
    for m in re.finditer(r"norm\(\s*((?:\w+\.)?\w+)\s*\)\s*(?:=|!=|<>)\s*norm\((" + _STRING
                         + r")\)", sql, flags=re.I):
        pairs.append((m.group(1), m.group(2)))
    for m in re.finditer(r"norm\(\s*((?:\w+\.)?\w+)\s*\)\s+(?:not\s+)?in\s*\(((?:\s*norm\("
                         + _STRING + r"\)\s*,?)+)\)", sql, flags=re.I):
        for v in re.findall(_STRING, m.group(2)):
            pairs.append((m.group(1), v))

    unknown = []
    for col, lit in pairs:
        value = lit[1:-1].replace("''", "'")
        allowed = schema.examples_for_column(col)
        if allowed and norm(value) not in {norm(a) for a in allowed}:
            unknown.append({"colonne": col.split(".")[-1], "valeur": value,
                            "valeurs_possibles": allowed})
    return unknown


# =============================================================================
#  Execution et journalisation
# =============================================================================
def log_query(sql: str, status: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean = " ".join(sql.split())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} | T2S {status} | {clean}\n")


def run_select(sql: str, db_path: Path = None, allowed_tables=None):
    """Valide puis execute un SELECT en lecture seule. Renvoie (colonnes, lignes).
    Leve ValueError (refus de securite) ou sqlite3.Error (erreur SQL)."""
    ok, msg = validate_sql(sql)
    if not ok:
        log_query(sql, f"REFUSEE ({msg})")
        raise ValueError(msg)
    con = connect(db_path, allowed_tables if allowed_tables is not None else ALLOWED_TABLES)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS)
    except sqlite3.Error as e:
        log_query(sql, f"ERREUR ({e})")
        raise
    finally:
        con.close()
    log_query(sql, f"OK ({len(rows)} lignes)")
    return cols, rows


# =============================================================================
#  Appel du modele
# =============================================================================
NUM_CTX = 8192   # taille de contexte donnee au modele (en tokens)


def ask_llm(model: str, messages: list) -> str:
    r = requests.post(f"{OLLAMA_URL}/api/chat",
                      json={"model": model, "messages": messages, "stream": False,
                            "options": {"temperature": 0, "num_ctx": NUM_CTX}},
                      timeout=300)
    r.raise_for_status()
    text = r.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


# =============================================================================
#  Boucle principale
# =============================================================================
_schema_cache: Dict[str, DbSchema] = {}


def get_schema(db_path: Path = None) -> DbSchema:
    db_path = Path(db_path or DB_PATH)
    key = f"{db_path}:{db_path.stat().st_mtime if db_path.exists() else 0}"
    if key not in _schema_cache:
        _schema_cache[key] = DbSchema(db_path, allowed_tables=ALLOWED_TABLES)
    return _schema_cache[key]


_linking_cache: Dict[str, tuple] = {}


def get_linking_tools(db_path: Path = None, embed_fn=None):
    """(SchemaLinker, ValueIndex) pour une base : construits une fois, embeddings en cache."""
    try:
        from src.schema_linking import Embedder, SchemaLinker, ValueIndex
    except ImportError:
        from schema_linking import Embedder, SchemaLinker, ValueIndex
    db_path = Path(db_path or DB_PATH)
    key = str(db_path)
    if key not in _linking_cache:
        schema = get_schema(db_path)
        emb = Embedder(OLLAMA_URL, config.EMBED_MODEL, embed_fn=embed_fn,
                       cache_path=None if embed_fn else
                       config.DATA_DIR / "cache" / f"emb_{db_path.stem}.json")
        _linking_cache[key] = (SchemaLinker(schema, emb), ValueIndex(schema))
    return _linking_cache[key]


def solve(question: str, model: str = None, db_path: Path = None,
          with_answer: bool = True, verbose: bool = False,
          llm: Callable[[str, list], str] = None,
          linking: Optional[bool] = None, embed_fn=None) -> Dict:
    """Question -> SQL -> resultat.

    linking : None = automatique (active si la base a >= LINKING_MIN_TABLES tables),
              True / False pour forcer. Avec linking, seules les tables utiles et les
              valeurs trouvees dans la question sont envoyees au modele.

    status : 'ok'             requete executee (rows peut etre vide)
             'hors_base'      le modele dit que la base ne peut pas repondre (regle 7)
             'valeur_absente' une valeur demandee n'existe pas dans la base
             'echec'          aucune requete valide apres les corrections
    """
    model = model or SQL_MODEL
    llm = llm or ask_llm
    db_path = Path(db_path or DB_PATH)
    schema = get_schema(db_path)

    def log(*a):
        if verbose:
            print(*a)

    res = {"status": "echec", "question": question, "sql": "", "sql_brut": "",
           "cols": [], "rows": [], "answer": "", "attempts": 0, "erreurs": [],
           "tables_selectionnees": [], "valeurs_trouvees": []}

    use_linking = linking if linking is not None else \
        len(schema.tables) >= config.LINKING_MIN_TABLES
    if use_linking:
        linker, value_index = get_linking_tools(db_path, embed_fn)
        hits = value_index.search(question)
        tables = linker.link(question, extra_tables=[h["table"] for h in hits])
        res.update(tables_selectionnees=tables, valeurs_trouvees=hits)
        log(f"[Linking] tables : {tables}")
        for h in hits:
            log(f"[Valeur] {h['table']}.{h['colonne']} = '{h['valeur']}' <- '{h['texte_question']}'")
        prompt = build_prompt(question, schema.to_mschema(tables), values=hits)
    else:
        prompt = build_prompt(question, schema.to_mschema())
    res["prompt_tokens_estimes"] = len(prompt) // 3
    messages = [{"role": "user", "content": prompt}]
    values_feedback_done = empty_feedback_done = False

    for attempt in range(MAX_RETRIES + 1):
        raw = llm(model, messages)
        res["attempts"] = attempt + 1
        sql = extract_sql(raw)
        res["sql_brut"] = sql
        log(f"[SQL modele] {sql}")

        def retry(msg: str):
            res["erreurs"].append(msg)
            log(f"[Correction demandee] {msg}")
            messages.extend([{"role": "assistant", "content": raw},
                             {"role": "user", "content": msg}])

        # Regle 7 : question hors base
        if is_hors_base(sql):
            log_query(sql, "HORS_BASE")
            res.update(status="hors_base", sql=sql,
                       answer="Cette information n'existe pas dans la base RisKover.")
            return res

        ok, msg = validate_sql(sql)
        if not ok:
            log_query(sql, f"REFUSEE ({msg})")
            retry(f"Requête refusée : {msg} Corrige-la en respectant les règles.")
            continue

        sql = enforce_limit(apply_norm(sql, schema.text_columns))
        res["sql"] = sql

        unknown = find_unknown_values(sql, schema)
        if unknown:
            detail = "; ".join(f"'{u['valeur']}' n'existe pas dans {u['colonne']} "
                               f"(valeurs possibles : {', '.join(u['valeurs_possibles'])})"
                               for u in unknown)
            if not values_feedback_done:
                values_feedback_done = True
                retry(f"{detail}. Choisis une valeur de la liste qui correspond au sens de "
                      "la question. Si aucune ne correspond, réponds SELECT 'HORS_BASE' AS erreur;")
                continue
            log_query(sql, f"VALEUR_ABSENTE ({detail})")
            res.update(status="valeur_absente", valeurs_absentes=unknown,
                       answer="Valeur absente de la base : " + detail + ".")
            return res

        try:
            cols, rows = run_select(sql, db_path)
        except ValueError as e:
            retry(f"Requête refusée : {e} Corrige-la.")
            continue
        except sqlite3.Error as e:
            hint = ""
            if "no such" in str(e) or "not authorized" in str(e):
                hint = " Utilise uniquement les tables et colonnes du schéma, écrites à l'identique."
            retry(f"Erreur SQLite : {e}.{hint} Corrige la requête.")
            continue

        res.update(status="ok", cols=cols, rows=rows)
        if not rows and not empty_feedback_done and attempt < MAX_RETRIES:
            # Un resultat vide peut etre juste, on laisse au modele une seule verification
            empty_feedback_done = True
            retry("Le résultat est vide. Vérifie les tables, les jointures et les valeurs. "
                  "Si la requête est correcte, renvoie-la identique.")
            continue
        break

    if res["status"] != "ok":
        res["answer"] = ("Je n'ai pas réussi à écrire une requête valide. Dernière erreur : "
                         + (res["erreurs"][-1] if res["erreurs"] else "inconnue"))
        return res

    log(f"[SQL execute] {res['sql']}")
    n = len(res["rows"])
    log(f"[Resultat] {res['cols']} -> {n} ligne(s)"
        + (f"  (limite atteinte : {MAX_ROWS}, utilise --limit pour en voir plus)"
           if n >= MAX_ROWS else ""))
    for row in res["rows"][:SHOW_ROWS]:
        log("   ", row)
    if n > SHOW_ROWS:
        log(f"    ... {n - SHOW_ROWS} autre(s) ligne(s) non affichee(s) (option --show)")

    if with_answer:
        res["answer"] = write_answer(question, res, llm)
    return res


def write_answer(question: str, res: Dict, llm=None) -> str:
    llm = llm or ask_llm
    return llm(ANSWER_MODEL, [
        {"role": "system", "content":
            "Réponds en français, clairement et brièvement. Utilise seulement le résultat "
            "fourni, n'invente aucun chiffre. Si le résultat est vide, dis que l'information "
            "n'existe pas dans la base. Cite la source : « base SQL RisKover, table(s) … ». "
            "Ne propose jamais d'exécuter du SQL."},
        {"role": "user", "content":
            f"Question : {question}\nRequête : {res['sql']}\n"
            f"Colonnes : {res['cols']}\nNombre total de lignes : {len(res['rows'])}\n"
            f"Résultat : {res['rows'][:ANSWER_ROWS]}"}])


def set_limits(max_rows: Optional[int] = None, show_rows: Optional[int] = None) -> None:
    """Change les limites pour toute la session (utilise par --limit / --show)."""
    global MAX_ROWS, SHOW_ROWS
    if max_rows:
        if max_rows < 1:
            raise SystemExit("--limit doit etre >= 1")
        MAX_ROWS = max_rows
    if show_rows is not None:
        SHOW_ROWS = max(0, show_rows)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("question", nargs="?", help="Question (sinon mode interactif)")
    p.add_argument("--model", default=SQL_MODEL, help="Modele text-to-SQL")
    p.add_argument("--no-answer", action="store_true", help="Sans reponse en francais")
    p.add_argument("--show-prompt", action="store_true", help="Affiche le prompt envoye")
    p.add_argument("--db", default=str(DB_PATH), help="Base SQLite a interroger")
    p.add_argument("--linking", choices=["auto", "on", "off"], default="auto",
                   help="Schema linking (auto : active pour les grandes bases)")
    p.add_argument("--limit", type=int, default=MAX_ROWS,
                   help=f"Lignes max renvoyees par la base (defaut {MAX_ROWS})")
    p.add_argument("--show", type=int, default=SHOW_ROWS,
                   help=f"Lignes affichees a l'ecran (defaut {SHOW_ROWS})")
    args = p.parse_args()
    set_limits(args.limit, args.show)
    db = Path(args.db)
    linking = {"auto": None, "on": True, "off": False}[args.linking]

    print(f"Modele SQL : {args.model} | Modele reponse : {ANSWER_MODEL} | Base : {db} | "
          f"Limite : {MAX_ROWS} lignes (affichees : {SHOW_ROWS})")
    questions = [args.question] if args.question else iter(
        lambda: input("\nQuestion (vide pour quitter) : ").strip(), "")
    for q in questions:
        r = solve(q, model=args.model, db_path=db, with_answer=not args.no_answer,
                  verbose=True, linking=linking)
        if args.show_prompt:
            print(f"[Prompt ~{r['prompt_tokens_estimes']} tokens]")
        print(f"[Statut] {r['status']}")
        if r["answer"]:
            print(f"[Reponse] {r['answer']}")


if __name__ == "__main__":
    main()
