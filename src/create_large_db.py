"""
create_large_db.py - Cree une GRANDE base de test db/banque_large.db (donnees 100 % FICTIVES).

But : verifier si le text-to-SQL tient sur une base qui ressemble a un vrai systeme bancaire :
  - ~110 tables (credits, tiers, comptes + RH, comptabilite, monetique, marketing, IT...)
  - noms de colonnes codes (MT_ACC, CD_STAT, DT_OCT...) comme dans les vrais SI bancaires
  - tables pieges qui se ressemblent (CRD_DOSSIER, CRD_DOSSIER_HIST, CRD_DOSSIER_ARCH, STG_...)
  - colonnes avec des centaines / milliers de valeurs (localites, raisons sociales)
  - ~400 000 lignes

Les descriptions des tables et colonnes (ce qu'une banque ecrirait dans son dictionnaire de
donnees) sont enregistrees a cote : db/banque_large.docs.json (lu par mschema.py).

Usage (depuis rag_bancaire) :
    python src/create_large_db.py            (refuse d'ecraser une base existante)
    python src/create_large_db.py --force
"""
import argparse
import json
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "banque_large.db"
SEED = 7

# =============================================================================
#  Referentiels
# =============================================================================
GOUVERNORATS = ["Tunis", "Ariana", "Ben Arous", "Manouba", "Nabeul", "Zaghouan", "Bizerte",
                "Béja", "Jendouba", "Le Kef", "Siliana", "Sousse", "Monastir", "Mahdia", "Sfax",
                "Kairouan", "Kasserine", "Sidi Bouzid", "Gabès", "Médenine", "Tataouine",
                "Gafsa", "Tozeur", "Kébili"]
QUARTIERS = ["Centre", "Nord", "Sud", "Médina", "Cité El Khadra", "Zone Industrielle",
             "El Menzah", "Ennasr", "Hay Ettadhamen", "Cité Ezzouhour", "Route de la Plage",
             "El Manar"]
SECTEURS = [("AGR", "Agriculture"), ("AGRO", "Agroalimentaire"), ("BTP", "Bâtiment et travaux publics"),
            ("COM", "Commerce de détail"), ("CGR", "Commerce de gros"), ("TEX", "Textile et habillement"),
            ("CHI", "Chimie"), ("PHA", "Pharmacie"), ("SAN", "Santé"), ("ENS", "Enseignement"),
            ("INF", "Informatique et télécoms"), ("TRA", "Transport et logistique"),
            ("TOU", "Tourisme et hôtellerie"), ("IMM", "Immobilier"), ("FIN", "Services financiers"),
            ("ENE", "Énergie"), ("MEC", "Industrie mécanique"), ("ELE", "Industrie électrique"),
            ("PEC", "Pêche"), ("ART", "Artisanat"), ("SER", "Services aux entreprises"),
            ("ADM", "Administration"), ("MED", "Médias et communication"), ("AUT", "Automobile"),
            ("MIN", "Mines et phosphates")]
SEGMENTS = [("PART", "Particuliers"), ("PRO", "Professionnels"), ("TPE", "Très petites entreprises"),
            ("PME", "Petites et moyennes entreprises"), ("GE", "Grandes entreprises"),
            ("INST", "Institutionnels")]
PRODUITS = [("CI", "Crédit immobilier"), ("CC", "Crédit à la consommation"),
            ("CINV", "Crédit d'investissement"), ("CT", "Crédit de trésorerie"), ("LS", "Leasing"),
            ("ESC", "Escompte commercial"), ("AVM", "Avance sur marchandises"),
            ("CAS", "Crédit agricole saisonnier"), ("EPS", "Engagement par signature")]
STATUTS = [("EC", "En cours"), ("SO", "Soldé"), ("RET", "En retard"), ("CTX", "Contentieux"),
           ("RES", "Restructuré"), ("ANN", "Annulé")]
TYPES_GAR = [("HYP", "Hypothèque"), ("NFC", "Nantissement sur fonds de commerce"),
             ("CAP", "Caution personnelle"), ("CAB", "Caution bancaire"),
             ("GAV", "Gage sur véhicule"), ("DAT", "Dépôt à terme nanti"),
             ("SOT", "Garantie SOTUGAR")]
DEVISES = [("TND", "Dinar tunisien"), ("EUR", "Euro"), ("USD", "Dollar américain")]
ETATS_CPT = [("ACT", "Actif"), ("BLQ", "Bloqué"), ("CLO", "Clôturé"), ("DOR", "Dormant")]
FORMES = ["SARL", "SA", "SUARL", "SNC"]
INDICATEURS = [("Dette nette / EBITDA", "<=", 3.5), ("Ratio de liquidité", ">=", 1.2),
               ("Gearing", "<=", 1.5), ("DSCR", ">=", 1.25), ("Fonds propres minimum", ">=", 500000)]
TYPES_DOC = ["Rapport annuel", "Bilan", "Rapport trimestriel", "Email", "Note interne",
             "Rapport de visite", "Procès-verbal"]
RS_A = ["Atlas", "Carthage", "Medina", "Oasis", "Jasmin", "Olivier", "Sahel", "Horizon",
        "Phénix", "Zitouna", "Kerkennah", "Dougga", "Tabarka", "Djerba", "Hannibal", "Elissa",
        "Salammbô", "Byrsa", "Kairouan", "Ifriqiya", "Numidia", "Thapsus", "Utique", "Bulla",
        "Sufetula", "Hadrumète", "Thysdrus", "Capsa", "Tacape", "Gightis", "Zarzis", "Chenini",
        "Matmata", "Douz", "Nefta", "Chott", "Ichkeul", "Zembra", "Cap Bon", "Korbous"]
RS_B = ["Industries", "Services", "Distribution", "Technologies", "Négoce", "Logistique",
        "Conseil", "Construction", "Agro", "Textiles", "Pharma", "Énergie", "Immobilier",
        "Transport", "Tourisme", "Mécanique", "Électrique", "Plastiques", "Emballage",
        "Céramique", "Marbre", "Huiles", "Dattes", "Pêche", "Médical", "Informatique",
        "Télécom", "Import Export", "Automobiles", "Bois"]
RS_C = ["", " Tunisie", " Méditerranée", " International", " & Fils", " Group"]


def d(rng, a: date, b: date) -> str:
    return (a + timedelta(days=rng.randint(0, (b - a).days))).isoformat()


# =============================================================================
#  Tables metier (avec leur dictionnaire de donnees)
# =============================================================================
CORE = {
    "REF_GOUVERNORAT": ("Référentiel des gouvernorats (régions) de Tunisie", [
        ("CD_GOUV", "TEXT PRIMARY KEY", "code du gouvernorat"),
        ("LB_GOUV", "TEXT", "nom du gouvernorat")]),
    "REF_LOCALITE": ("Référentiel des localités (ville + quartier)", [
        ("CD_LOC", "TEXT PRIMARY KEY", "code de la localité"),
        ("LB_LOC", "TEXT", "nom de la localité, ex. 'Sfax Médina'"),
        ("CD_GOUV", "TEXT REFERENCES REF_GOUVERNORAT(CD_GOUV)", "gouvernorat de la localité")]),
    "REF_AGENCE": ("Référentiel des agences bancaires", [
        ("CD_AGC", "TEXT PRIMARY KEY", "code de l'agence"),
        ("LB_AGC", "TEXT", "nom de l'agence"),
        ("CD_LOC", "TEXT REFERENCES REF_LOCALITE(CD_LOC)", "localité de l'agence")]),
    "REF_SECTEUR": ("Référentiel des secteurs d'activité", [
        ("CD_SECT", "TEXT PRIMARY KEY", "code du secteur"),
        ("LB_SECT", "TEXT", "libellé du secteur d'activité")]),
    "REF_SEGMENT": ("Référentiel des segments de clientèle", [
        ("CD_SEGMENT", "TEXT PRIMARY KEY", "code du segment"),
        ("LB_SEGMENT", "TEXT", "libellé du segment")]),
    "REF_PRODUIT_CRD": ("Référentiel des produits de crédit", [
        ("CD_PROD", "TEXT PRIMARY KEY", "code du produit de crédit"),
        ("LB_PROD", "TEXT", "libellé du produit (type de crédit)")]),
    "REF_STATUT_DOSSIER": ("Référentiel des statuts d'un dossier de crédit", [
        ("CD_STAT", "TEXT PRIMARY KEY", "code du statut"),
        ("LB_STAT", "TEXT", "libellé du statut du crédit")]),
    "REF_TYPE_GARANTIE": ("Référentiel des types de garantie", [
        ("CD_TYPE_GAR", "TEXT PRIMARY KEY", "code du type de garantie"),
        ("LB_TYPE_GAR", "TEXT", "libellé du type de garantie")]),
    "REF_DEVISE": ("Référentiel des devises", [
        ("CD_DEVISE", "TEXT PRIMARY KEY", "code ISO de la devise"),
        ("LB_DEVISE", "TEXT", "nom de la devise")]),
    "REF_ETAT_COMPTE": ("Référentiel des états d'un compte", [
        ("CD_ETAT", "TEXT PRIMARY KEY", "code de l'état du compte"),
        ("LB_ETAT", "TEXT", "libellé de l'état du compte")]),
    "TIE_PERSONNE": ("Tiers : tous les clients de la banque (personnes physiques et morales), pseudonymisés", [
        ("ID_TIERS", "TEXT PRIMARY KEY", "identifiant du client (tiers), format T0000001"),
        ("CD_TYPE_TIERS", "TEXT", "PP = personne physique (particulier), PM = personne morale (entreprise)"),
        ("CD_SEGMENT", "TEXT REFERENCES REF_SEGMENT(CD_SEGMENT)", "segment du client"),
        ("CD_SECT", "TEXT REFERENCES REF_SECTEUR(CD_SECT)", "secteur d'activité du client"),
        ("CD_LOC", "TEXT REFERENCES REF_LOCALITE(CD_LOC)", "localité de résidence ou du siège"),
        ("CD_AGC", "TEXT REFERENCES REF_AGENCE(CD_AGC)", "agence gestionnaire du client"),
        ("DT_ENTREE", "TEXT", "date d'entrée en relation, AAAA-MM-JJ"),
        ("TR_AGE", "TEXT", "tranche d'âge (personnes physiques seulement)")]),
    "TIE_ENTREPRISE": ("Informations complémentaires des clients entreprises (personnes morales)", [
        ("ID_TIERS", "TEXT PRIMARY KEY REFERENCES TIE_PERSONNE(ID_TIERS)", "identifiant du client entreprise"),
        ("LB_RAISON_SOC", "TEXT", "raison sociale (nom) de l'entreprise"),
        ("CD_FORME_JUR", "TEXT", "forme juridique"),
        ("NB_EFFECTIF", "INTEGER", "nombre de salariés"),
        ("DT_CREATION", "TEXT", "date de création de l'entreprise, AAAA-MM-JJ")]),
    "CPT_COMPTE": ("Comptes bancaires des clients", [
        ("NO_CPT", "TEXT PRIMARY KEY", "numéro de compte"),
        ("ID_TIERS", "TEXT REFERENCES TIE_PERSONNE(ID_TIERS)", "titulaire du compte"),
        ("CD_DEVISE", "TEXT REFERENCES REF_DEVISE(CD_DEVISE)", "devise du compte"),
        ("CD_ETAT", "TEXT REFERENCES REF_ETAT_COMPTE(CD_ETAT)", "état du compte"),
        ("DT_OUV", "TEXT", "date d'ouverture, AAAA-MM-JJ"),
        ("MT_SOLDE", "REAL", "solde actuel du compte")]),
    "CRD_DOSSIER": ("Dossiers de crédit ACTUELS (table de référence des crédits)", [
        ("ID_DOS", "TEXT PRIMARY KEY", "identifiant du dossier de crédit, format D00000001"),
        ("ID_TIERS", "TEXT REFERENCES TIE_PERSONNE(ID_TIERS)", "client emprunteur"),
        ("CD_PROD", "TEXT REFERENCES REF_PRODUIT_CRD(CD_PROD)", "produit de crédit (type de crédit)"),
        ("CD_STAT", "TEXT REFERENCES REF_STATUT_DOSSIER(CD_STAT)", "statut actuel du crédit"),
        ("CD_AGC", "TEXT REFERENCES REF_AGENCE(CD_AGC)", "agence qui a accordé le crédit"),
        ("MT_ACC", "REAL", "montant accordé en dinars (TND)"),
        ("TX_NOM", "REAL", "taux nominal annuel en %"),
        ("NB_DUREE_M", "INTEGER", "durée du crédit en mois"),
        ("DT_OCT", "TEXT", "date d'octroi du crédit, AAAA-MM-JJ")]),
    "CRD_ECHEANCE": ("Échéancier : chaque échéance (mensualité) d'un crédit", [
        ("ID_DOS", "TEXT REFERENCES CRD_DOSSIER(ID_DOS)", "dossier de crédit"),
        ("NO_ECH", "INTEGER", "numéro de l'échéance"),
        ("DT_ECH", "TEXT", "date d'échéance, AAAA-MM-JJ"),
        ("MT_ECH", "REAL", "montant dû de l'échéance"),
        ("MT_PAYE", "REAL", "montant effectivement payé")]),
    "CRD_IMPAYE": ("Incidents de paiement / impayés sur un crédit", [
        ("ID_IMP", "TEXT PRIMARY KEY", "identifiant de l'impayé"),
        ("ID_DOS", "TEXT REFERENCES CRD_DOSSIER(ID_DOS)", "dossier de crédit concerné"),
        ("DT_IMP", "TEXT", "date de l'impayé, AAAA-MM-JJ"),
        ("NB_JR_RET", "INTEGER", "nombre de jours de retard"),
        ("MT_IMP", "REAL", "montant impayé en TND"),
        ("FL_REGUL", "TEXT", "O si l'impayé est régularisé, N sinon")]),
    "CRD_GARANTIE": ("Garanties (sûretés) adossées à un crédit", [
        ("ID_GAR", "TEXT PRIMARY KEY", "identifiant de la garantie"),
        ("ID_DOS", "TEXT REFERENCES CRD_DOSSIER(ID_DOS)", "crédit garanti"),
        ("CD_TYPE_GAR", "TEXT REFERENCES REF_TYPE_GARANTIE(CD_TYPE_GAR)", "type de garantie"),
        ("MT_VAL", "REAL", "valeur de la garantie en TND")]),
    "CRD_COVENANT": ("Covenants financiers d'un crédit, mesurés par trimestre", [
        ("ID_COV", "TEXT PRIMARY KEY", "identifiant du covenant"),
        ("ID_DOS", "TEXT REFERENCES CRD_DOSSIER(ID_DOS)", "crédit concerné"),
        ("LB_IND", "TEXT", "indicateur financier suivi"),
        ("CD_OPER", "TEXT", "sens du seuil (<= ou >=)"),
        ("VL_SEUIL", "REAL", "seuil contractuel"),
        ("VL_CONST", "REAL", "valeur constatée sur le trimestre"),
        ("CD_TRIM", "TEXT", "trimestre de mesure, ex. 2025-T2"),
        ("FL_RESP", "TEXT", "O si le covenant est respecté, N s'il est dépassé")]),
    "DOC_GED": ("Documents de la GED reliés au RAG (rapports, emails, notes)", [
        ("ID_DOC", "TEXT PRIMARY KEY", "identifiant du document, commun avec les bases NoSQL / vectorielle"),
        ("ID_TIERS", "TEXT REFERENCES TIE_PERSONNE(ID_TIERS)", "client concerné (vide si non lié)"),
        ("LB_FICHIER", "TEXT", "nom du fichier"),
        ("LB_TYPE_DOC", "TEXT", "type de document"),
        ("DT_DOC", "TEXT", "date du document, AAAA-MM-JJ"),
        ("FL_LIE", "TEXT", "O si le document est rapproché d'un client, N sinon")]),
}

# Tables pieges : copies historiques / archives / staging qui ressemblent aux vraies
TRAPS = {
    "CRD_DOSSIER_HIST": ("CRD_DOSSIER", "Historique mensuel (photos) des dossiers de crédit - ne pas utiliser pour l'état actuel"),
    "CRD_DOSSIER_ARCH": ("CRD_DOSSIER", "Archives des dossiers de crédit clôturés avant 2016"),
    "STG_CRD_DOSSIER": ("CRD_DOSSIER", "Zone de chargement (staging) technique des dossiers de crédit, données brutes non validées"),
    "CRD_IMPAYE_HIST": ("CRD_IMPAYE", "Historique des impayés (photos mensuelles)"),
    "CRD_GARANTIE_HIST": ("CRD_GARANTIE", "Historique des garanties levées ou expirées"),
    "TIE_PERSONNE_HIST": ("TIE_PERSONNE", "Historique des modifications des tiers"),
    "STG_TIE_PERSONNE": ("TIE_PERSONNE", "Zone de chargement (staging) des tiers"),
    "CPT_COMPTE_HIST": ("CPT_COMPTE", "Historique des soldes de comptes"),
    "TMP_EXPORT_RISQUE": ("CRD_DOSSIER", "Table temporaire d'export pour le reporting risque"),
}

# Autres domaines du SI (bruit realiste)
DOMAINS = {
    "RH": ("Ressources humaines", ["EMPLOYE", "PAIE", "CONGE", "FORMATION", "POSTE", "ABSENCE",
                                   "EVALUATION", "RECRUTEMENT"]),
    "CTB": ("Comptabilité générale", ["ECRITURE", "JOURNAL", "COMPTE_GL", "BALANCE", "EXERCICE",
                                      "PROVISION", "IMMOBILISATION"]),
    "MKT": ("Marketing", ["CAMPAGNE", "CIBLE", "OFFRE", "CONTACT", "RETOUR", "SATISFACTION",
                          "PROSPECT"]),
    "IT": ("Informatique interne", ["TICKET", "INCIDENT", "APPLICATION", "SERVEUR", "HABILITATION",
                                    "CHANGEMENT", "SAUVEGARDE"]),
    "MON": ("Monétique (cartes)", ["CARTE", "TRANSACTION_CB", "TPE", "OPPOSITION", "PLAFOND",
                                   "COMMERCANT"]),
    "TRE": ("Trésorerie et marchés", ["PLACEMENT", "OPERATION_CHANGE", "POSITION", "LIGNE_REFI",
                                      "COURS_DEVISE"]),
    "REG": ("Réglementaire", ["DECLARATION_BCT", "CENTRALE_RISQUE", "LCB_ALERTE", "KYC_CONTROLE",
                              "RATIO_PRUDENTIEL", "SANCTION_LISTE"]),
    "AUD": ("Audit interne", ["LOG_CONNEXION", "PISTE_AUDIT", "MISSION", "RECOMMANDATION"]),
    "PAR": ("Paramétrage", ["PARAMETRE", "CALENDRIER", "TAUX_REFERENCE", "JOUR_FERIE",
                            "UTILISATEUR"]),
    "JUR": ("Service juridique", ["AVOCAT", "HUISSIER", "PROCEDURE", "AUDIENCE"]),
    "GAB": ("Distributeurs automatiques", ["DISTRIBUTEUR", "RETRAIT", "ALIMENTATION"]),
    "EPA": ("Épargne", ["PLAN_EPARGNE", "VERSEMENT", "INTERET"]),
    "ASS": ("Bancassurance", ["CONTRAT", "SINISTRE", "PRIME"]),
    "CRM": ("Relation client", ["INTERACTION", "RECLAMATION", "RDV", "NOTE_CONSEILLER"]),
}
NOISE_COLS = [
    ("CD_TYPE", "TEXT", "code type"), ("LB_LIBELLE", "TEXT", "libellé"),
    ("DT_CREATION", "TEXT", "date de création"), ("DT_MAJ", "TEXT", "date de mise à jour"),
    ("MT_MONTANT", "REAL", "montant en TND"), ("NB_QUANTITE", "INTEGER", "nombre"),
    ("FL_ACTIF", "TEXT", "indicateur actif O/N"), ("CD_STATUT", "TEXT", "code statut"),
    ("ID_TIERS", "TEXT REFERENCES TIE_PERSONNE(ID_TIERS)", "client concerné"),
    ("CD_AGC", "TEXT REFERENCES REF_AGENCE(CD_AGC)", "agence concernée"),
    ("CD_UTIL", "TEXT", "code utilisateur"), ("TX_TAUX", "REAL", "taux en %"),
    ("LB_COMMENTAIRE", "TEXT", "commentaire libre"), ("NB_JOURS", "INTEGER", "nombre de jours"),
]
WORDS = ["standard", "urgent", "mensuel", "annuel", "validé", "rejeté", "en attente", "spécial",
         "prioritaire", "révisé", "provisoire", "définitif", "interne", "externe", "manuel",
         "automatique", "national", "régional", "contrôlé", "à revoir"]


def create_table(con, name, cols):
    con.execute(f"CREATE TABLE {name} (" + ", ".join(f"{c} {t}" for c, t, _ in cols) + ")")


def build(con: sqlite3.Connection) -> dict:
    rng = random.Random(SEED)
    docs = {"tables": {}, "columns": {}}

    def register(name, desc, cols):
        docs["tables"][name] = desc
        for c, _, cd in cols:
            docs["columns"][f"{name}.{c}"] = cd

    for name, (desc, cols) in CORE.items():
        create_table(con, name, cols)
        register(name, desc, cols)

    # --- referentiels ----------------------------------------------------------
    gouv = [(f"G{i + 1:02d}", g) for i, g in enumerate(GOUVERNORATS)]
    con.executemany("INSERT INTO REF_GOUVERNORAT VALUES (?,?)", gouv)
    locs = []
    for cg, g in gouv:
        for j, q in enumerate(QUARTIERS):
            locs.append((f"L{len(locs) + 1:04d}", f"{g} {q}", cg))
    con.executemany("INSERT INTO REF_LOCALITE VALUES (?,?,?)", locs)
    agences = [(f"A{i + 1:03d}", f"Agence {locs[i * 2][1]}", locs[i * 2][0]) for i in range(140)]
    con.executemany("INSERT INTO REF_AGENCE VALUES (?,?,?)", agences)
    con.executemany("INSERT INTO REF_SECTEUR VALUES (?,?)", SECTEURS)
    con.executemany("INSERT INTO REF_SEGMENT VALUES (?,?)", SEGMENTS)
    con.executemany("INSERT INTO REF_PRODUIT_CRD VALUES (?,?)", PRODUITS)
    con.executemany("INSERT INTO REF_STATUT_DOSSIER VALUES (?,?)", STATUTS)
    con.executemany("INSERT INTO REF_TYPE_GARANTIE VALUES (?,?)", TYPES_GAR)
    con.executemany("INSERT INTO REF_DEVISE VALUES (?,?)", DEVISES)
    con.executemany("INSERT INTO REF_ETAT_COMPTE VALUES (?,?)", ETATS_CPT)

    # --- tiers ---------------------------------------------------------------
    n_pm, n_pp = 4000, 16000
    rs_all = [f"{a} {b}{c}" for a in RS_A for b in RS_B for c in RS_C]
    rng.shuffle(rs_all)
    tiers, entreprises = [], []
    ages = ["18-25", "26-35", "36-50", "51-65", "65+"]
    for i in range(n_pm + n_pp):
        tid = f"T{i + 1:07d}"
        loc = rng.choice(locs)[0]
        agc = rng.choice(agences)[0]
        if i < n_pm:
            seg = rng.choices(["TPE", "PME", "GE", "INST"], weights=[45, 40, 12, 3])[0]
            tiers.append((tid, "PM", seg, rng.choice(SECTEURS)[0], loc, agc,
                          d(rng, date(2000, 1, 1), date(2025, 6, 30)), None))
            entreprises.append((tid, rs_all[i], rng.choice(FORMES),
                                {"TPE": rng.randint(1, 9), "PME": rng.randint(10, 199),
                                 "GE": rng.randint(200, 5000), "INST": rng.randint(20, 800)}[seg],
                                d(rng, date(1970, 1, 1), date(2022, 12, 31))))
        else:
            seg = rng.choices(["PART", "PRO"], weights=[85, 15])[0]
            tiers.append((tid, "PP", seg, rng.choice(SECTEURS)[0], loc, agc,
                          d(rng, date(2000, 1, 1), date(2025, 6, 30)), rng.choice(ages)))
    con.executemany("INSERT INTO TIE_PERSONNE VALUES (?,?,?,?,?,?,?,?)", tiers)
    con.executemany("INSERT INTO TIE_ENTREPRISE VALUES (?,?,?,?,?)", entreprises)

    # --- comptes -------------------------------------------------------------
    comptes = []
    for i in range(30000):
        t = rng.choice(tiers)
        comptes.append((f"C{i + 1:010d}", t[0], rng.choices(["TND", "EUR", "USD"], [90, 7, 3])[0],
                        rng.choices(["ACT", "BLQ", "CLO", "DOR"], [80, 3, 10, 7])[0],
                        d(rng, date(2000, 1, 1), date(2025, 9, 30)),
                        round(rng.uniform(-5000, 250000), 3)))
    con.executemany("INSERT INTO CPT_COMPTE VALUES (?,?,?,?,?,?)", comptes)

    # --- dossiers de credit ----------------------------------------------------
    dossiers = []
    for i in range(40000):
        t = rng.choice(tiers)
        if t[1] == "PP":
            prod = rng.choices(["CI", "CC"], [35, 65])[0]
            mt = rng.choice([5000, 10000, 20000, 50000, 100000, 200000, 350000])
        else:
            prod = rng.choice(["CINV", "CT", "LS", "ESC", "AVM", "CAS", "EPS"])
            mt = rng.choice([50000, 100000, 250000, 500000, 1000000, 2500000, 5000000])
        stat = rng.choices(["EC", "SO", "RET", "CTX", "RES", "ANN"], [55, 25, 9, 4, 4, 3])[0]
        dossiers.append((f"D{i + 1:08d}", t[0], prod, stat, rng.choice(agences)[0], float(mt),
                         round(rng.uniform(7.0, 14.5), 2),
                         rng.choice([6, 12, 24, 36, 60, 84, 120, 180, 240]),
                         d(rng, date(2016, 1, 1), date(2025, 9, 30))))
    con.executemany("INSERT INTO CRD_DOSSIER VALUES (?,?,?,?,?,?,?,?,?)", dossiers)

    echeances = []
    for dos in dossiers:
        mens = round(dos[5] / dos[7] * (1 + dos[6] / 100), 3)
        start = date.fromisoformat(dos[8])
        for k in range(1, 7):
            due = start + timedelta(days=30 * k)
            paid = mens if dos[3] in ("EC", "SO", "RES") or rng.random() < 0.6 else round(mens * rng.uniform(0, 0.8), 3)
            echeances.append((dos[0], k, due.isoformat(), mens, paid))
    con.executemany("INSERT INTO CRD_ECHEANCE VALUES (?,?,?,?,?)", echeances)

    impayes, n = [], 0
    for dos in dossiers:
        p = {"RET": 1.0, "CTX": 1.0, "RES": 0.5, "EC": 0.04, "SO": 0.02, "ANN": 0.0}[dos[3]]
        if rng.random() < p:
            for _ in range(rng.choice([1, 1, 2, 3])):
                n += 1
                ret = rng.choice([90, 120, 180, 270, 360]) if dos[3] == "CTX" else rng.choice([5, 15, 30, 45, 60, 90])
                impayes.append((f"I{n:07d}", dos[0], d(rng, date.fromisoformat(dos[8]), date(2025, 10, 31)),
                                ret, round(dos[5] * rng.uniform(0.005, 0.06), 3),
                                "N" if dos[3] in ("RET", "CTX") and rng.random() < 0.7 else "O"))
    con.executemany("INSERT INTO CRD_IMPAYE VALUES (?,?,?,?,?,?)", impayes)

    garanties, n = [], 0
    for dos in dossiers:
        if rng.random() < 0.7:
            n += 1
            tg = "HYP" if dos[2] == "CI" else rng.choice([g[0] for g in TYPES_GAR[1:]])
            garanties.append((f"G{n:07d}", dos[0], tg, round(dos[5] * rng.uniform(0.5, 1.6), -2)))
    con.executemany("INSERT INTO CRD_GARANTIE VALUES (?,?,?,?)", garanties)

    covenants, n = [], 0
    for dos in dossiers:
        if dos[2] in ("CINV", "CT") and rng.random() < 0.5:
            for trim in rng.sample(["2025-T1", "2025-T2", "2025-T3"], k=rng.choice([1, 2, 3])):
                n += 1
                ind, op, seuil = rng.choice(INDICATEURS)
                val = round(seuil * (1 + rng.uniform(-0.35, 0.25)), 2)
                ok = val <= seuil if op == "<=" else val >= seuil
                covenants.append((f"V{n:07d}", dos[0], ind, op, seuil, val, trim, "O" if ok else "N"))
    con.executemany("INSERT INTO CRD_COVENANT VALUES (?,?,?,?,?,?,?,?)", covenants)

    docs_ged = []
    for i in range(3000):
        lie = i % 15 != 0
        t = rng.choice(tiers)[0] if lie else None
        td = rng.choice(TYPES_DOC)
        dd = d(rng, date(2022, 1, 1), date(2025, 10, 31))
        ext = ".eml" if td == "Email" else rng.choice([".pdf", ".pdf", ".docx"])
        docs_ged.append((f"DOC{i + 1:06d}", t,
                         f"{td.lower().replace(' ', '_')}_{(t or 'inconnu').lower()}_{dd[:7]}{ext}",
                         td, dd, "O" if lie else "N"))
    con.executemany("INSERT INTO DOC_GED VALUES (?,?,?,?,?,?)", docs_ged)

    # --- tables pieges (copies partielles) --------------------------------------
    for name, (src, desc) in TRAPS.items():
        cols = list(CORE[src][1])
        cols = [(c, t.split(" REFERENCES")[0].replace(" PRIMARY KEY", ""), cd) for c, t, cd in cols]
        cols.append(("DT_SNAP", "TEXT", "date de la photo / du chargement"))
        create_table(con, name, cols)
        register(name, desc, cols)
        src_rows = con.execute(f"SELECT * FROM {src} ORDER BY random() LIMIT 3000").fetchall()
        snap = "2015-12-31" if name.endswith("_ARCH") else "2024-12-31"
        con.executemany(f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})",
                        [tuple(r) + (snap,) for r in src_rows])

    # --- tables des autres domaines ---------------------------------------------
    for prefix, (label, entities) in DOMAINS.items():
        for ent in entities:
            name = f"{prefix}_{ent}"
            readable = ent.lower().replace("_", " ")
            cols = [(f"ID_{ent[:10]}", "TEXT PRIMARY KEY", f"identifiant ({readable})")]
            for c, t, cd in rng.sample(NOISE_COLS, k=rng.randint(4, 9)):
                cols.append((c, t, f"{cd} ({readable})"))
            create_table(con, name, cols)
            register(name, f"{label} : {readable}", cols)
            rows = []
            for k in range(rng.randint(80, 400)):
                row = [f"{ent[:3]}{k + 1:06d}"]
                for c, t, _ in cols[1:]:
                    if c == "ID_TIERS":
                        row.append(rng.choice(tiers)[0])
                    elif c == "CD_AGC":
                        row.append(rng.choice(agences)[0])
                    elif c.startswith("DT_"):
                        row.append(d(rng, date(2018, 1, 1), date(2025, 10, 31)))
                    elif c.startswith(("MT_", "TX_")):
                        row.append(round(rng.uniform(0, 100000 if c.startswith("MT_") else 15), 2))
                    elif c.startswith("NB_"):
                        row.append(rng.randint(0, 500))
                    elif c.startswith("FL_"):
                        row.append(rng.choice(["O", "N"]))
                    elif c.startswith("CD_"):
                        row.append(f"{c[3:5]}{rng.randint(1, 6)}")
                    else:
                        row.append(rng.choice(WORDS))
                rows.append(tuple(row))
            con.executemany(f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})", rows)

    # index utiles (les vraies bases en ont)
    for sql in ["CREATE INDEX ix_dos_tiers ON CRD_DOSSIER(ID_TIERS)",
                "CREATE INDEX ix_ech_dos ON CRD_ECHEANCE(ID_DOS)",
                "CREATE INDEX ix_imp_dos ON CRD_IMPAYE(ID_DOS)",
                "CREATE INDEX ix_gar_dos ON CRD_GARANTIE(ID_DOS)",
                "CREATE INDEX ix_cov_dos ON CRD_COVENANT(ID_DOS)",
                "CREATE INDEX ix_cpt_tiers ON CPT_COMPTE(ID_TIERS)"]:
        con.execute(sql)
    return docs


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true")
    p.add_argument("--path", default=str(DB_PATH))
    args = p.parse_args()
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not args.force:
            raise SystemExit(f"{path} existe deja. Utilise --force pour la recreer.")
        path.unlink()

    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        docs = build(con)
        con.commit()
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise SystemExit(f"Cles etrangeres invalides : {bad[:5]}")
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        total = sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
        ncols = sum(len(con.execute(f"PRAGMA table_info({t})").fetchall()) for t in tables)
    finally:
        con.close()

    docs_path = path.with_suffix(".docs.json")
    docs_path.write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Base creee : {path} ({path.stat().st_size / 1e6:.1f} Mo)")
    print(f"  {len(tables)} tables, {ncols} colonnes, {total} lignes")
    print(f"Dictionnaire de donnees : {docs_path}")


if __name__ == "__main__":
    main()
