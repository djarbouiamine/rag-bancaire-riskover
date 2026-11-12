"""
compare_sql.py - Tester le modele avec TES requetes SQL.

Principe :
  1. tu ecris une question en francais ;
  2. tu ecris TA requete SQL (SELECT ... FROM ...) : c'est la bonne reponse ;
  3. le modele recoit SEULEMENT la question (jamais ton SQL) et ecrit sa propre requete ;
  4. les deux resultats sont executes sur la base et compares : ✔ meme resultat / ✘ different.
  Les questions peuvent etre enregistrees dans tests/mes_questions.csv pour etre relancees
  plus tard avec bench_text2sql.py.

Usage (depuis rag_bancaire, tunnel SSH ouvert) :
    python src/compare_sql.py                       # mode interactif
    python src/compare_sql.py --model gpt-oss:latest
    python src/compare_sql.py -q "Combien de clients a Sfax ?" -s "SELECT COUNT(*) FROM clients WHERE ville = 'Sfax'"

Dans le mode interactif :
  - la requete SQL peut tenir sur plusieurs lignes : termine-la par ; ou par une ligne vide ;
  - tape  -  a la place du SQL si la base NE PEUT PAS repondre (le modele doit dire HORS_BASE) ;
  - laisse la question vide pour quitter.
"""
import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.text2sql as t2s
from src.bench_text2sql import compare, says_nothing

DEFAULT_SAVE = t2s.BASE_DIR / "tests" / "mes_questions.csv"
REF_MAX_ROWS = 10000


# -----------------------------------------------------------------------------
#  Saisie
# -----------------------------------------------------------------------------
def read_sql() -> str:
    """Lit une requete sur une ou plusieurs lignes (fin : ';' ou ligne vide)."""
    print("Ta requete SQL (termine par ; ou une ligne vide, '-' si la base ne peut pas repondre) :")
    lines = []
    while True:
        line = input("  sql> " if not lines else "  ...> ")
        if not line.strip():
            break
        lines.append(line)
        if line.strip() == "-" or line.rstrip().endswith(";"):
            break
    return "\n".join(lines).strip()


def ask_yes(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("o", "oui", "y", "yes")


# -----------------------------------------------------------------------------
#  Execution de la requete de reference (la tienne)
# -----------------------------------------------------------------------------
def run_reference(sql: str, db: Path):
    """Execute TA requete en lecture seule (memes protections que pour le modele)."""
    sql = sql.strip().rstrip(";").strip()
    ok, msg = t2s.validate_sql(sql)
    # SELECT * est autorise pour toi (c'est seulement interdit au modele)
    if not ok and "SELECT *" not in msg:
        raise ValueError(msg)
    con = t2s.connect(db)                       # lecture seule + norm() disponible
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(REF_MAX_ROWS)
    finally:
        con.close()
    return cols, rows


# -----------------------------------------------------------------------------
#  Affichage
# -----------------------------------------------------------------------------
def show(title: str, sql: str, cols, rows, n_show: int):
    print(f"\n  ── {title} " + "─" * max(0, 60 - len(title)))
    print("  SQL : " + " ".join(sql.split()))
    print(f"  {cols} -> {len(rows)} ligne(s)")
    for r in rows[:n_show]:
        print("     ", r)
    if len(rows) > n_show:
        print(f"      ... {len(rows) - n_show} autre(s) ligne(s)")


def verdict_for(ref_sql: str, ref_rows, res) -> str:
    if ref_sql == "-":   # la base ne peut pas repondre
        if res["status"] in ("hors_base", "valeur_absente") or (
                res["status"] == "ok" and says_nothing(res["rows"])):
            return "✔ OK : le modele refuse d'inventer"
        return "✘ A VERIFIER : le modele a donne une reponse alors que la base ne peut pas repondre"
    if res["status"] != "ok":
        if says_nothing(ref_rows) and res["status"] in ("hors_base", "valeur_absente"):
            return "✔ OK : rien dans la base, et le modele le dit"
        return f"✘ DIFFERENT : le modele n'a pas de resultat (statut {res['status']})"
    v = compare(ref_rows, res["rows"])
    if v.startswith("MATCH"):
        return "✔ MEME RESULTAT" + (f"  ({v[6:].strip(' ()')})" if v != "MATCH" else "")
    return "✘ DIFFERENT"


def save_question(path: Path, question: str, ref_sql: str, categorie: str):
    new = not path.exists()
    if new:
        path.write_text("# Mes questions (creees avec compare_sql.py). Separateur : point-virgule.\n"
                        "id;categorie;question;attendu;sql_reference\n", encoding="utf-8")
    with open(path, encoding="utf-8") as f:
        n = sum(1 for l in f if l.strip() and not l.startswith(("#", "id;")))
    attendu = "absent" if ref_sql == "-" else "match"
    sql = "" if ref_sql == "-" else " ".join(ref_sql.rstrip(";").split())
    with open(path, "a", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter=";").writerow(
            [n + 1, categorie or "-", question.replace(";", ","), attendu, sql])
    print(f"  Enregistree dans {path.name} (question {n + 1}).")


# -----------------------------------------------------------------------------
#  Une comparaison
# -----------------------------------------------------------------------------
def compare_one(question: str, ref_sql: str, args, db: Path) -> bool:
    ref_cols, ref_rows = [], []
    if ref_sql != "-":
        try:
            ref_cols, ref_rows = run_reference(ref_sql, db)
        except (ValueError, sqlite3.Error) as e:
            print(f"  ⚠ Ta requete ne marche pas : {e}")
            return None
        show("TA REQUETE (la bonne reponse)", ref_sql, ref_cols, ref_rows, args.show)
    else:
        print("\n  ── TA REPONSE : la base ne peut pas repondre (attendu : HORS_BASE)")

    t0 = time.time()
    res = t2s.solve(question, model=args.model, db_path=db, with_answer=False,
                    linking={"auto": None, "on": True, "off": False}[args.linking])
    dt = time.time() - t0
    if res["status"] == "ok":
        show(f"LE MODELE ({args.model}, {dt:.1f} s)", res["sql"], res["cols"], res["rows"],
             args.show)
    else:
        print(f"\n  ── LE MODELE ({args.model}, {dt:.1f} s)")
        print(f"  statut : {res['status']}  |  SQL : {' '.join((res['sql'] or res['sql_brut']).split())}")
        if res.get("answer"):
            print(f"  message : {res['answer']}")
    if res.get("tables_selectionnees"):
        print(f"  tables choisies par le linking : {', '.join(res['tables_selectionnees'])}")

    verdict = verdict_for(ref_sql, ref_rows, res)
    print(f"\n  ==> {verdict}\n")
    return verdict.startswith("✔")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("-q", "--question", help="Question (sinon mode interactif)")
    p.add_argument("-s", "--sql", help="Ta requete SQL de reference (ou '-')")
    p.add_argument("--model", default=t2s.SQL_MODEL)
    p.add_argument("--db", default=str(t2s.DB_PATH))
    p.add_argument("--linking", choices=["auto", "on", "off"], default="auto")
    p.add_argument("--show", type=int, default=10, help="Lignes affichees par resultat")
    p.add_argument("--save", default=str(DEFAULT_SAVE),
                   help="Fichier ou enregistrer les questions (pour bench_text2sql.py)")
    args = p.parse_args()
    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"Base introuvable : {db}. Lance d'abord : python src/create_riskover_db.py")

    print(f"Base : {db.name} | Modele : {args.model} | linking : {args.linking}")
    try:
        if args.question:
            compare_one(args.question, args.sql or "-", args, db)
            return

        ok = total = 0
        while True:
            question = input("\nQuestion en francais (vide pour quitter) : ").strip()
            if not question:
                break
            ref_sql = read_sql()
            if not ref_sql:
                print("  (pas de requete : question ignoree)")
                continue
            result = compare_one(question, ref_sql, args, db)
            if result is None:
                continue
            total += 1
            ok += result
            if ask_yes("  Enregistrer cette question pour le benchmark ? (o/n) : "):
                cat = input("  Categorie (ex. jointure, agregat, piege...) : ").strip()
                save_question(Path(args.save), question, ref_sql, cat)
        if total:
            print(f"\n=== Score de la session : {ok}/{total} ===")
            print(f"Pour relancer tes questions enregistrees : "
                  f"python src/bench_text2sql.py --file {Path(args.save).relative_to(t2s.BASE_DIR)}")
    except requests.exceptions.ConnectionError:
        print("\nConnexion impossible au serveur : verifie le tunnel SSH (fenetre 1).")
    except (KeyboardInterrupt, EOFError):
        print()


if __name__ == "__main__":
    main()
