"""
test_schema_linking.py - Tests hors ligne du schema linking (faux embeddings, pas de serveur).

Le faux embedding est un « sac de mots » : un vecteur qui compte les mots du texte.
Il suffit pour verifier la logique (choix des tables, tables pont, valeurs, malus des
tables techniques) sans appeler bge-m3.

Usage (depuis rag_bancaire) :
    python -m unittest tests.test_schema_linking -v
"""
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.text2sql as t2s
from src.mschema import DbSchema
from src.schema_linking import Embedder, SchemaLinker, ValueIndex, tokens

DIM = 256
NOISE_TABLES = ["RH_PAIE", "RH_CONGE", "IT_TICKET", "IT_SERVEUR", "MKT_CAMPAGNE",
                "MKT_OFFRE", "CTB_JOURNAL", "CTB_BALANCE"]


def fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * DIM
        for w in tokens(t):
            v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % DIM] += 1.0
        out.append(v)
    return out


def make_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE REF_LOCALITE (CD_LOC TEXT PRIMARY KEY, LB_LOC TEXT);
    CREATE TABLE REF_STATUT (CD_STAT TEXT PRIMARY KEY, LB_STAT TEXT);
    CREATE TABLE TIERS (ID_TIERS TEXT PRIMARY KEY, CD_LOC TEXT REFERENCES REF_LOCALITE(CD_LOC));
    CREATE TABLE DOSSIER (ID_DOS TEXT PRIMARY KEY, ID_TIERS TEXT REFERENCES TIERS(ID_TIERS),
                          CD_STAT TEXT REFERENCES REF_STATUT(CD_STAT), MT_ACC REAL);
    CREATE TABLE DOSSIER_HIST (ID_DOS TEXT, ID_TIERS TEXT, CD_STAT TEXT, MT_ACC REAL);
    CREATE TABLE RH_EMPLOYE (ID_EMP TEXT PRIMARY KEY, LB_POSTE TEXT);
    """)
    con.executemany("INSERT INTO REF_LOCALITE VALUES (?,?)",
                    [(f"L{i}", f"Ville{i} Quartier") for i in range(40)] + [("L99", "Sfax Médina")])
    con.executemany("INSERT INTO REF_STATUT VALUES (?,?)",
                    [("EC", "En cours"), ("CTX", "Contentieux")])
    con.executemany("INSERT INTO TIERS VALUES (?,?)", [("T1", "L99"), ("T2", "L1")])
    con.executemany("INSERT INTO DOSSIER VALUES (?,?,?,?)",
                    [("D1", "T1", "CTX", 1000.0), ("D2", "T2", "EC", 2000.0)])
    con.executemany("INSERT INTO DOSSIER_HIST VALUES (?,?,?,?)", [("D1", "T1", "EC", 1000.0)])
    con.executemany("INSERT INTO RH_EMPLOYE VALUES (?,?)", [("E1", "Analyste")])
    for name in NOISE_TABLES:   # bruit : d'autres domaines du SI
        con.execute(f"CREATE TABLE {name} (ID_X TEXT PRIMARY KEY, LB_X TEXT, NB_X INTEGER)")
    con.commit()
    con.close()
    path.with_suffix(".docs.json").write_text("""{
     "tables": {"REF_LOCALITE": "localites ville quartier", "REF_STATUT": "statuts des credits",
                "TIERS": "clients de la banque", "DOSSIER": "dossiers de credit actuels",
                "DOSSIER_HIST": "historique des dossiers de credit",
                "RH_EMPLOYE": "employes ressources humaines"},
     "columns": {"DOSSIER.MT_ACC": "montant du credit", "REF_STATUT.LB_STAT": "libelle du statut",
                 "REF_LOCALITE.LB_LOC": "nom de la localite", "TIERS.CD_LOC": "localite du client"}
    }""", encoding="utf-8")


class SchemaLinkingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "mini.db"
        make_db(cls.db)
        cls.schema = DbSchema(cls.db)
        cls.linker = SchemaLinker(cls.schema, Embedder("http://x", embed_fn=fake_embed))
        cls.values = ValueIndex(cls.schema)
        cls._orig_log = t2s.LOG_FILE
        t2s.LOG_FILE = cls.tmp / "queries.log"

    @classmethod
    def tearDownClass(cls):
        t2s.LOG_FILE = cls._orig_log

    def test_docs_json_loaded(self):
        self.assertEqual(self.schema.table_doc("DOSSIER"), "dossiers de credit actuels")
        self.assertIn("dossiers de credit actuels", self.schema.to_mschema(["DOSSIER"]))

    def test_mschema_subset_keeps_only_fk_inside(self):
        ms = self.schema.to_mschema(["DOSSIER", "TIERS"])
        self.assertIn("DOSSIER.ID_TIERS=TIERS.ID_TIERS", ms)
        self.assertNotIn("REF_STATUT", ms)
        self.assertNotIn("RH_EMPLOYE", ms)

    def test_value_search_exact_and_typo(self):
        hits = {(h["table"], h["valeur"]) for h in
                self.values.search("credits en contentieu a SFAX MEDINA")}
        self.assertIn(("REF_LOCALITE", "Sfax Médina"), hits)
        self.assertIn(("REF_STATUT", "Contentieux"), hits)

    def test_value_search_nothing(self):
        self.assertEqual(self.values.search("bonjour"), [])

    def test_tech_table_penalty(self):
        ranked = [t for t, _ in self.linker.score_tables("montant des dossiers de credit")]
        self.assertLess(ranked.index("DOSSIER"), ranked.index("DOSSIER_HIST"))
        ranked_h = [t for t, _ in self.linker.score_tables("historique des dossiers de credit")]
        self.assertLess(ranked_h.index("DOSSIER_HIST"), ranked_h.index("RH_EMPLOYE"))

    def test_bridge_tables_added(self):
        # REF_LOCALITE et DOSSIER ne sont relies que par TIERS : il doit etre ajoute
        tables = self.linker.link("credits", top_k=1, extra_tables=["REF_LOCALITE"])
        self.assertIn("TIERS", tables)

    def test_solve_with_linking(self):
        t2s._linking_cache.clear()
        llm_calls = []

        def llm(model, messages):
            llm_calls.append(messages[0]["content"])
            return ("SELECT COUNT(d.ID_DOS) FROM DOSSIER d JOIN TIERS t ON t.ID_TIERS = d.ID_TIERS "
                    "JOIN REF_LOCALITE l ON l.CD_LOC = t.CD_LOC WHERE l.LB_LOC = 'sfax medina'")
        res = t2s.solve("combien de dossiers a sfax medina", model="fake", db_path=self.db,
                        with_answer=False, llm=llm, linking=True, embed_fn=fake_embed)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["rows"], [(1,)])
        self.assertIn("REF_LOCALITE", res["tables_selectionnees"])
        self.assertIn("Valeurs trouvées", llm_calls[0])
        self.assertIn("'Sfax Médina'", llm_calls[0])
        # seule une partie des tables est envoyee au modele
        self.assertLess(len(res["tables_selectionnees"]), len(self.schema.tables))
        sent_noise = [t for t in NOISE_TABLES if f"# Table: {t}" in llm_calls[0]]
        self.assertLess(len(sent_noise), len(NOISE_TABLES))


if __name__ == "__main__":
    unittest.main()
