"""
test_text2sql.py - Tests hors ligne de src/text2sql.py (aucun appel au serveur Ollama).

Usage (depuis rag_bancaire) :
    python -m unittest tests.test_text2sql -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.text2sql as t2s
from src.create_riskover_db import build
from src.mschema import DbSchema


def fake_llm(responses):
    """Faux modele : renvoie les reponses dans l'ordre et garde les messages recus."""
    calls = []

    def llm(model, messages):
        calls.append([dict(m) for m in messages])
        return responses[min(len(calls) - 1, len(responses) - 1)]
    return llm, calls


class Text2SqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = Path(cls.tmp) / "riskover.db"
        con = sqlite3.connect(cls.db)
        build(con)
        con.commit()
        con.close()
        cls._orig_log = t2s.LOG_FILE
        t2s.LOG_FILE = Path(cls.tmp) / "queries.log"
        cls.schema = DbSchema(cls.db)

    @classmethod
    def tearDownClass(cls):
        t2s.LOG_FILE = cls._orig_log

    def solve(self, responses, question="q"):
        llm, calls = fake_llm(responses)
        res = t2s.solve(question, model="fake", db_path=self.db, with_answer=False, llm=llm)
        return res, calls

    # --- norm() ---------------------------------------------------------------
    def test_norm(self):
        self.assertEqual(t2s.norm("  SANTÉ "), "sante")
        self.assertEqual(t2s.norm("cli_009"), t2s.norm("CLI_009"))
        self.assertEqual(t2s.norm("Gabès"), "gabes")
        self.assertIsNone(t2s.norm(None))

    def test_norm_in_sqlite(self):
        con = t2s.connect(self.db)
        n = con.execute("SELECT COUNT(c.client_id) FROM clients AS c "
                        "WHERE norm(c.secteur) = norm('SANTE')").fetchone()[0]
        ref = con.execute("SELECT COUNT(c.client_id) FROM clients AS c "
                          "WHERE c.secteur = 'Santé'").fetchone()[0]
        con.close()
        self.assertGreater(ref, 0)
        self.assertEqual(n, ref)

    # --- validation (regles 1 et 6) ------------------------------------------
    def test_validate_rejects(self):
        for bad in ["DROP TABLE clients",
                    "SELECT c.client_id FROM clients c; DELETE FROM clients",
                    "SELECT * FROM clients",
                    "SELECT c.* FROM clients c",
                    "UPDATE clients SET ville = 'x'",
                    "SELECT c.client_id FROM clients c -- commentaire"]:
            self.assertFalse(t2s.validate_sql(bad)[0], bad)

    def test_validate_accepts(self):
        for good in ["SELECT COUNT(*) AS n FROM clients",
                     "WITH x AS (SELECT c.client_id FROM clients c) SELECT x.client_id FROM x",
                     "SELECT c.client_id FROM clients c WHERE c.ville = 'a;b -- c'"]:
            self.assertTrue(t2s.validate_sql(good)[0], good)

    def test_enforce_limit(self):
        self.assertTrue(t2s.enforce_limit("SELECT a FROM t").endswith("LIMIT 200"))
        self.assertTrue(t2s.enforce_limit("SELECT a FROM t LIMIT 5000").endswith("LIMIT 200"))
        self.assertTrue(t2s.enforce_limit("SELECT a FROM t LIMIT 10").endswith("LIMIT 10"))

    def test_authorizer_blocks_system_tables(self):
        with self.assertRaises(sqlite3.Error):
            t2s.run_select("SELECT name FROM sqlite_master", self.db)

    def test_allowed_tables(self):
        with self.assertRaises(sqlite3.Error):
            t2s.run_select("SELECT e.raison_sociale FROM entreprises e", self.db,
                           allowed_tables=["clients"])

    # --- reecriture norm() (regles 2 et 4) -----------------------------------
    def test_apply_norm(self):
        tc = self.schema.text_columns
        out = t2s.apply_norm("SELECT c.client_id FROM clients c WHERE c.ville = 'SFAX' "
                             "AND c.client_id IN ('cli_001','cli_002') AND cr.montant > 1000 "
                             "AND e.raison_sociale LIKE '%atlas%'", tc)
        self.assertIn("norm(c.ville) = norm('SFAX')", out)
        self.assertIn("norm(c.client_id) IN (norm('cli_001'), norm('cli_002'))", out)
        self.assertIn("cr.montant > 1000", out)
        self.assertIn("norm(e.raison_sociale) LIKE norm('%atlas%')", out)
        # deja correct : ne change pas
        ok = "SELECT c.client_id FROM clients c WHERE norm(c.ville) = norm('Sfax')"
        self.assertEqual(t2s.apply_norm(ok, tc), ok)
        # norm a gauche seulement
        self.assertIn("norm(c.ville) = norm('Sfax')",
                      t2s.apply_norm("SELECT 1 FROM clients c WHERE norm(c.ville) = 'Sfax'", tc))

    def test_numeric_not_normed(self):
        out = t2s.apply_norm("SELECT 1 FROM credits cr WHERE cr.duree_mois = '60'",
                             self.schema.text_columns)
        self.assertNotIn("norm(cr.duree_mois)", out)

    # --- solve() de bout en bout ---------------------------------------------
    def test_case_insensitive_end_to_end(self):
        res, _ = self.solve(["```sql\nSELECT c.client_id FROM clients AS c "
                             "WHERE c.client_id = 'cli_009'\n```"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["rows"], [("CLI_009",)])
        self.assertIn("LIMIT 200", res["sql"])

    def test_accent_insensitive_join(self):
        res, _ = self.solve(["SELECT cr.credit_id FROM credits AS cr JOIN clients AS c "
                             "ON c.client_id = cr.client_id WHERE norm(c.ville) = norm('GABES')"])
        self.assertEqual(res["status"], "ok")
        self.assertGreater(len(res["rows"]), 0)

    def test_hors_base(self):
        res, _ = self.solve(["SELECT 'HORS_BASE' AS erreur;"])
        self.assertEqual(res["status"], "hors_base")

    def test_unknown_value_then_corrected(self):
        res, calls = self.solve([
            "SELECT c.client_id FROM clients c WHERE c.secteur = 'Banque'",
            "SELECT c.client_id FROM clients c WHERE c.secteur = 'Finance'"])
        self.assertEqual(res["status"], "ok")
        self.assertIn("valeurs possibles", calls[1][-1]["content"])

    def test_unknown_value_twice_is_absent(self):
        res, _ = self.solve(["SELECT c.client_id FROM clients c WHERE c.ville = 'Casablanca'"])
        self.assertEqual(res["status"], "valeur_absente")

    def test_select_star_is_corrected(self):
        res, calls = self.solve(["SELECT * FROM clients",
                                 "SELECT c.client_id FROM clients c"])
        self.assertEqual(res["status"], "ok")
        self.assertIn("SELECT *", calls[1][-1]["content"])

    def test_sql_error_then_fixed(self):
        res, calls = self.solve(["SELECT c.nom FROM clients c",
                                 "SELECT c.client_id FROM clients c"])
        self.assertEqual(res["status"], "ok")
        self.assertIn("no such column", calls[1][-1]["content"])

    def test_all_fail(self):
        res, _ = self.solve(["DROP TABLE clients"])
        self.assertEqual(res["status"], "echec")

    def test_mschema_has_fk_and_examples(self):
        ms = self.schema.to_mschema()
        self.assertIn("【Foreign keys】", ms)
        self.assertIn("credits.client_id=clients.client_id", ms)
        self.assertIn("Examples: [Contentieux, En cours, En retard, Remboursé]", ms)
        self.assertNotIn("CLI_001, CLI_002", ms)  # pas d'Examples sur les identifiants


if __name__ == "__main__":
    unittest.main()
