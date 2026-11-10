"""
create_riskover_db.py - Cree la base de test db/riskover.db (donnees 100 % FICTIVES).

Reproduit la structure attendue de la base SQL RisKover (voir projet_tutore.docx) :
plusieurs tables reliees par des cles etrangeres (client_id, entreprise_id, credit_id),
plus une table documents qui fait le lien avec les bases NoSQL / vectorielle (doc_id).

Tables :
  entreprises        (30 lignes)
  clients            (60 lignes)   -> entreprises
  credits            (~100 lignes) -> clients
  garanties          -> credits
  covenants          -> credits
  incidents_paiement -> credits
  documents          -> clients, entreprises

Usage (depuis rag_bancaire) :
    python src/create_riskover_db.py            (refuse d'ecraser une base existante)
    python src/create_riskover_db.py --force    (recree la base de zero)

Le generateur est deterministe (graine fixe) : la meme base est recreee a chaque fois,
ce qui permet de garder des requetes de reference stables dans tests/questions_riskover.csv.
"""
import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "riskover.db"
SEED = 2026

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE entreprises (
    entreprise_id   TEXT PRIMARY KEY,          -- ex. ENT_001
    raison_sociale  TEXT NOT NULL,             -- nom fictif de l'entreprise
    secteur         TEXT NOT NULL,
    ville           TEXT NOT NULL,
    taille          TEXT NOT NULL,             -- TPE, PME, ETI, Grande entreprise
    forme_juridique TEXT NOT NULL,             -- SARL, SA, SUARL...
    date_creation   TEXT NOT NULL              -- AAAA-MM-JJ
);

CREATE TABLE clients (
    client_id      TEXT PRIMARY KEY,           -- ex. CLI_001 (pseudonyme)
    entreprise_id  TEXT REFERENCES entreprises(entreprise_id),
    type_client    TEXT NOT NULL,              -- Particulier, Professionnel, Entreprise
    segment        TEXT NOT NULL,              -- Retail, PME, Corporate, Private
    secteur        TEXT NOT NULL,
    tranche_age    TEXT,                       -- seulement pour les particuliers
    ville          TEXT NOT NULL,
    date_entree    TEXT NOT NULL               -- date d'entree en relation
);

CREATE TABLE credits (
    credit_id    TEXT PRIMARY KEY,             -- ex. CRD_0001
    client_id    TEXT NOT NULL REFERENCES clients(client_id),
    type_credit  TEXT NOT NULL,
    montant      REAL NOT NULL,                -- en dinars (TND)
    taux         REAL NOT NULL,                -- taux annuel en %
    duree_mois   INTEGER NOT NULL,
    date_octroi  TEXT NOT NULL,                -- AAAA-MM-JJ
    statut       TEXT NOT NULL                 -- En cours, Rembourse, En retard, Contentieux
);

CREATE TABLE garanties (
    garantie_id    TEXT PRIMARY KEY,           -- ex. GAR_0001
    credit_id      TEXT NOT NULL REFERENCES credits(credit_id),
    type_garantie  TEXT NOT NULL,
    valeur         REAL NOT NULL               -- en dinars (TND)
);

CREATE TABLE covenants (
    covenant_id       TEXT PRIMARY KEY,        -- ex. COV_0001
    credit_id         TEXT NOT NULL REFERENCES credits(credit_id),
    indicateur        TEXT NOT NULL,
    operateur         TEXT NOT NULL,           -- '<=' ou '>='
    seuil             REAL NOT NULL,
    valeur_constatee  REAL NOT NULL,
    trimestre         TEXT NOT NULL,           -- ex. 2025-T2
    respecte          TEXT NOT NULL            -- Oui / Non
);

CREATE TABLE incidents_paiement (
    incident_id     TEXT PRIMARY KEY,          -- ex. INC_0001
    credit_id       TEXT NOT NULL REFERENCES credits(credit_id),
    date_incident   TEXT NOT NULL,
    jours_retard    INTEGER NOT NULL,
    montant_impaye  REAL NOT NULL,
    regularise      TEXT NOT NULL              -- Oui / Non
);

CREATE TABLE documents (
    doc_id         TEXT PRIMARY KEY,           -- identifiant commun avec NoSQL / vectorielle
    client_id      TEXT REFERENCES clients(client_id),
    entreprise_id  TEXT REFERENCES entreprises(entreprise_id),
    nom_fichier    TEXT NOT NULL,
    type_doc       TEXT NOT NULL,
    date_doc       TEXT NOT NULL,
    lie            TEXT NOT NULL               -- Oui / Non (rapprochement avec la base)
);
"""

SECTEURS = ["Services", "Commerce", "Industrie", "Santé", "Enseignement",
            "Informatique", "BTP", "Agroalimentaire", "Transport", "Finance",
            "Tourisme", "Textile"]
VILLES = ["Tunis", "Sfax", "Sousse", "Nabeul", "Bizerte", "Monastir",
          "Gabès", "Kairouan", "Ariana", "Ben Arous"]
TAILLES = ["TPE", "PME", "ETI", "Grande entreprise"]
FORMES = ["SARL", "SA", "SUARL"]
NOMS_A = ["Atlas", "Carthage", "Medina", "Oasis", "Jasmin", "Olivier", "Sahel",
          "Horizon", "Phénix", "Zitouna", "Kerkennah", "Dougga", "Tabarka",
          "Djerba", "Hannibal"]
NOMS_B = ["Industries", "Services", "Distribution", "Technologies", "Négoce",
          "Logistique", "Conseil", "Construction"]
AGES = ["18-25", "26-35", "36-50", "51-65", "65+"]
TYPES_CREDIT = ["Crédit immobilier", "Crédit à la consommation", "Crédit d'investissement",
                "Crédit de trésorerie", "Leasing"]
STATUTS = ["En cours", "Remboursé", "En retard", "Contentieux"]
TYPES_GARANTIE = ["Hypothèque", "Nantissement", "Caution personnelle",
                  "Caution bancaire", "Gage sur véhicule"]
INDICATEURS = [("Dette nette / EBITDA", "<=", 3.5),
               ("Ratio de liquidité", ">=", 1.2),
               ("Gearing", "<=", 1.5),
               ("DSCR", ">=", 1.25)]
TYPES_DOC = ["Rapport annuel", "Bilan", "Rapport trimestriel", "Email", "Note interne",
             "Rapport de visite"]


def rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def build(con: sqlite3.Connection) -> dict:
    rng = random.Random(SEED)
    con.executescript(SCHEMA_SQL)
    counts = {}

    # --- entreprises ---------------------------------------------------------
    entreprises = []
    used_names = set()
    for i in range(30):
        while True:
            nom = f"{rng.choice(NOMS_A)} {rng.choice(NOMS_B)}"
            if nom not in used_names:
                used_names.add(nom)
                break
        ent = (f"ENT_{i + 1:03d}", nom, SECTEURS[i % len(SECTEURS)],
               VILLES[i % len(VILLES)], rng.choice(TAILLES), rng.choice(FORMES),
               rand_date(rng, date(1985, 1, 1), date(2022, 12, 31)).isoformat())
        entreprises.append(ent)
    con.executemany("INSERT INTO entreprises VALUES (?,?,?,?,?,?,?)", entreprises)

    # --- clients -------------------------------------------------------------
    clients = []
    for i in range(60):
        cid = f"CLI_{i + 1:03d}"
        if i < 30:   # un client "Entreprise" par entreprise
            ent = entreprises[i]
            seg = "Corporate" if ent[4] in ("ETI", "Grande entreprise") else "PME"
            clients.append((cid, ent[0], "Entreprise", seg, ent[2], None, ent[3],
                            rand_date(rng, date(2005, 1, 1), date(2024, 12, 31)).isoformat()))
        elif i < 42:  # professionnels (rattaches a une entreprise)
            ent = rng.choice(entreprises)
            clients.append((cid, ent[0], "Professionnel", "PME", ent[2],
                            rng.choice(AGES[1:4]), rng.choice(VILLES),
                            rand_date(rng, date(2008, 1, 1), date(2025, 6, 30)).isoformat()))
        else:         # particuliers
            seg = "Private" if i % 6 == 0 else "Retail"
            clients.append((cid, None, "Particulier", seg, rng.choice(SECTEURS),
                            AGES[i % len(AGES)], VILLES[i % len(VILLES)],
                            rand_date(rng, date(2010, 1, 1), date(2025, 6, 30)).isoformat()))
    con.executemany("INSERT INTO clients VALUES (?,?,?,?,?,?,?,?)", clients)

    # --- credits -------------------------------------------------------------
    credits = []
    n = 0
    for c in clients:
        for _ in range(rng.choice([1, 1, 2, 2, 3])):
            n += 1
            if c[2] == "Particulier":
                tc = rng.choice(["Crédit immobilier", "Crédit à la consommation"])
                montant = rng.choice([15_000, 30_000, 60_000, 120_000, 250_000])
            else:
                tc = rng.choice(["Crédit d'investissement", "Crédit de trésorerie", "Leasing"])
                montant = rng.choice([100_000, 250_000, 500_000, 1_000_000, 2_500_000])
            statut = rng.choices(STATUTS, weights=[60, 20, 14, 6])[0]
            credits.append((f"CRD_{n:04d}", c[0], tc, float(montant),
                            round(rng.uniform(7.5, 13.5), 2),
                            rng.choice([12, 24, 36, 60, 84, 120, 180, 240]),
                            rand_date(rng, date(2018, 1, 1), date(2025, 9, 30)).isoformat(),
                            statut))
    con.executemany("INSERT INTO credits VALUES (?,?,?,?,?,?,?,?)", credits)

    # --- garanties -----------------------------------------------------------
    garanties = []
    n = 0
    for cr in credits:
        if rng.random() < 0.75:
            n += 1
            tg = ("Hypothèque" if cr[2] == "Crédit immobilier"
                  else rng.choice(TYPES_GARANTIE[1:]))
            garanties.append((f"GAR_{n:04d}", cr[0], tg,
                              round(cr[3] * rng.uniform(0.5, 1.6), -2)))
    con.executemany("INSERT INTO garanties VALUES (?,?,?,?)", garanties)

    # --- covenants (credits entreprises / professionnels) --------------------
    covenants = []
    n = 0
    pro_credits = [cr for cr in credits if cr[2] in ("Crédit d'investissement",
                                                     "Crédit de trésorerie", "Leasing")]
    for cr in pro_credits:
        if rng.random() < 0.6:
            for trimestre in rng.sample(["2025-T1", "2025-T2", "2025-T3"], k=rng.choice([1, 2])):
                n += 1
                ind, op, seuil = rng.choice(INDICATEURS)
                ecart = rng.uniform(-0.35, 0.25) * seuil
                val = round(seuil + ecart, 2)
                ok = val <= seuil if op == "<=" else val >= seuil
                covenants.append((f"COV_{n:04d}", cr[0], ind, op, seuil, val,
                                  trimestre, "Oui" if ok else "Non"))
    con.executemany("INSERT INTO covenants VALUES (?,?,?,?,?,?,?,?)", covenants)

    # --- incidents de paiement (credits en retard / contentieux surtout) -----
    incidents = []
    n = 0
    for cr in credits:
        p = {"En retard": 1.0, "Contentieux": 1.0, "En cours": 0.12, "Remboursé": 0.05}[cr[7]]
        if rng.random() < p:
            for _ in range(rng.choice([1, 1, 2, 3]) if cr[7] != "En cours" else 1):
                n += 1
                d = rand_date(rng, date.fromisoformat(cr[6]), date(2025, 10, 31))
                retard = rng.choice([15, 30, 45, 60, 90, 120, 180]) if cr[7] == "Contentieux" \
                    else rng.choice([5, 10, 15, 30, 45, 60])
                regul = "Non" if cr[7] in ("En retard", "Contentieux") and rng.random() < 0.7 else "Oui"
                incidents.append((f"INC_{n:04d}", cr[0], d.isoformat(), retard,
                                  round(cr[3] * rng.uniform(0.005, 0.05), 2), regul))
    con.executemany("INSERT INTO incidents_paiement VALUES (?,?,?,?,?,?)", incidents)

    # --- documents (lien avec NoSQL / vectorielle) ---------------------------
    documents = []
    for i in range(50):
        if i < 45:
            c = rng.choice(clients)
            cli, ent = c[0], c[1]
            lie = "Oui"
        else:  # documents non rapproches (voir 3.1 du document de recherche)
            cli, ent, lie = None, None, "Non"
        td = rng.choice(TYPES_DOC)
        d = rand_date(rng, date(2023, 1, 1), date(2025, 10, 31))
        ext = ".eml" if td == "Email" else rng.choice([".pdf", ".pdf", ".docx"])
        nom = f"{td.lower().replace(' ', '_')}_{(cli or 'inconnu').lower()}_{d:%Y%m}{ext}"
        documents.append((f"DOC_{i + 1:04d}", cli, ent, nom, td, d.isoformat(), lie))
    con.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?)", documents)

    for t in ("entreprises", "clients", "credits", "garanties", "covenants",
              "incidents_paiement", "documents"):
        counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="Recree la base de zero")
    p.add_argument("--path", default=str(DB_PATH), help="Chemin de la base a creer")
    args = p.parse_args()

    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not args.force:
            raise SystemExit(f"{path} existe deja. Utilise --force pour la recreer.")
        path.unlink()

    con = sqlite3.connect(path)
    try:
        counts = build(con)
        con.commit()
        problems = con.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise SystemExit(f"Cles etrangeres invalides : {problems[:5]}")
    finally:
        con.close()

    print(f"Base creee : {path}")
    for t, n in counts.items():
        print(f"  {t:<20} {n:>4} lignes")


if __name__ == "__main__":
    main()
