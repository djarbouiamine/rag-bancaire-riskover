"""
bench_text2sql.py - Compare des modeles text-to-SQL (et le schema linking) sur un fichier de questions.

Pour chaque question : l'agent (text2sql.solve) repond, la requete de reference donne
la verite, et on compare les resultats (pas le texte SQL).

Usage (depuis rag_bancaire) :
    # petite base (7 tables)
    python src/bench_text2sql.py --models qwen2.5-coder:14b qwen3:14b gpt-oss:latest

    # grande base (100 tables) : sans puis avec schema linking
    python src/bench_text2sql.py --db db/banque_large.db --file tests/questions_large.csv ^
        --models qwen2.5-coder:14b --linking off on

    python src/bench_text2sql.py --only 3 8 17
"""
import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.text2sql as t2s

DEFAULT_FILE = t2s.BASE_DIR / "tests" / "questions_riskover.csv"


def load_questions(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        lines = [l for l in f if l.strip() and not l.lstrip().startswith("#")]
    return list(csv.DictReader(lines, delimiter=";"))


def norm_rows(rows):
    def cell(x):
        return f"{x:.4f}" if isinstance(x, float) else str(x)
    return sorted(tuple(cell(x) for x in r) for r in rows)


def compare(ref, got):
    if norm_rows(ref) == norm_rows(got):
        return "MATCH"
    # memes lignes, l'agent a renvoye des colonnes en plus : accepte
    if len(ref) == len(got):
        nref, ngot = norm_rows(ref), norm_rows(got)
        if all(any(set(r) <= set(g) for g in ngot) for r in nref):
            return "MATCH (colonnes en plus)"
    return "DIFFERENT"


def ref_tables(sql: str):
    return {m.upper() for m in re.findall(r"\b(?:from|join)\s+(\w+)", sql or "", flags=re.I)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", default=[t2s.SQL_MODEL])
    p.add_argument("--db", default=str(t2s.DB_PATH))
    p.add_argument("--file", default=str(DEFAULT_FILE))
    p.add_argument("--linking", nargs="+", choices=["auto", "on", "off"], default=["auto"],
                   help="Un ou plusieurs modes a comparer, ex. --linking off on")
    p.add_argument("--only", nargs="+")
    args = p.parse_args()

    db = Path(args.db)
    questions = load_questions(Path(args.file))
    if args.only:
        questions = [q for q in questions if q["id"] in args.only]

    out_dir = t2s.BASE_DIR / "benchmark_results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"text2sql_{db.stem}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    fields = ["modele", "linking", "id", "categorie", "question", "attendu", "verdict", "statut",
              "tables_ok", "tables_selectionnees", "valeurs_trouvees", "prompt_tokens",
              "sql_agent", "sql_reference", "lignes_attendues", "lignes_obtenues", "essais",
              "temps_s"]
    summary = {}
    by_cat = defaultdict(lambda: [0, 0])

    print(f"Base : {db} | Questions : {len(questions)}")
    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        for model in args.models:
            for mode in args.linking:
                linking = {"auto": None, "on": True, "off": False}[mode]
                label = f"{model} | linking={mode}"
                print(f"\n=== {label} ===")
                ok = tables_ok_n = tables_tot = 0
                times = []
                for q in questions:
                    t0 = time.time()
                    try:
                        res = t2s.solve(q["question"], model=model, db_path=db,
                                        with_answer=False, linking=linking)
                    except requests.exceptions.ConnectionError:
                        print("Connexion impossible : verifie le tunnel SSH et Ollama.")
                        return
                    except requests.exceptions.HTTPError as e:
                        print(f"Erreur serveur ({e}) : le modele est-il installe ?")
                        break
                    dt = time.time() - t0
                    times.append(dt)
                    ref = []
                    if q["attendu"] == "match":
                        con = t2s.connect(db, guard=False)
                        ref = con.execute(q["sql_reference"]).fetchall()
                        con.close()
                        verdict = compare(ref, res["rows"]) if res["status"] == "ok" \
                            else f"DIFFERENT (statut {res['status']})"
                    else:
                        verdict = "OK (refuse d'inventer)" if (
                            res["status"] in ("hors_base", "valeur_absente")
                            or (res["status"] == "ok" and not res["rows"])) else "A VERIFIER"
                    good = verdict.startswith(("MATCH", "OK"))
                    ok += good
                    cat = q.get("categorie") or "-"
                    by_cat[(label, cat)][0] += good
                    by_cat[(label, cat)][1] += 1

                    # Le linking a-t-il garde les tables de la requete de reference ?
                    sel = {t.upper() for t in res.get("tables_selectionnees", [])}
                    need = ref_tables(q.get("sql_reference"))
                    t_ok = ""
                    if sel and need:
                        t_ok = "oui" if need <= sel else "non: manque " + ",".join(sorted(need - sel))
                        tables_tot += 1
                        tables_ok_n += need <= sel

                    print(f"Q{q['id']:<3} {dt:5.1f}s {'✔' if good else '✘'} {verdict:<28} "
                          f"[{cat}] {q['question']}")
                    if not good:
                        print(f"       SQL : {res['sql'] or res['sql_brut'] or res.get('answer')}")
                        if t_ok.startswith("non"):
                            print(f"       Linking : {t_ok}")
                    w.writerow({
                        "modele": model, "linking": mode, "id": q["id"], "categorie": cat,
                        "question": q["question"], "attendu": q["attendu"], "verdict": verdict,
                        "statut": res["status"], "tables_ok": t_ok,
                        "tables_selectionnees": ",".join(res.get("tables_selectionnees", [])),
                        "valeurs_trouvees": " | ".join(
                            f"{h['table']}.{h['colonne']}='{h['valeur']}'"
                            for h in res.get("valeurs_trouvees", [])),
                        "prompt_tokens": res.get("prompt_tokens_estimes", ""),
                        "sql_agent": res["sql"] or res["sql_brut"],
                        "sql_reference": q["sql_reference"], "lignes_attendues": len(ref),
                        "lignes_obtenues": len(res["rows"]), "essais": res["attempts"],
                        "temps_s": round(dt, 1)})
                    f.flush()
                summary[label] = (ok, len(questions), sum(times) / max(len(times), 1),
                                  f"{tables_ok_n}/{tables_tot}" if tables_tot else "-")

    print("\n=== Resume ===")
    print(f"  {'modele | linking':<45} {'score':>7} {'temps moy':>10} {'tables ok':>10}")
    for m, (ok, n, avg, tok) in summary.items():
        print(f"  {m:<45} {ok:>3}/{n:<3} {avg:>8.1f}s {tok:>10}")
    cats = sorted({c for _, c in by_cat})
    if len(cats) > 1:
        print("\n=== Score par categorie ===")
        print("  " + f"{'':<45}" + "".join(f"{c:>10}" for c in cats))
        for m in summary:
            row = "".join(f"{by_cat[(m, c)][0]:>6}/{by_cat[(m, c)][1]:<3}" for c in cats)
            print(f"  {m:<45}{row}")
    print(f"\nFichier : {out_file}")


if __name__ == "__main__":
    main()
