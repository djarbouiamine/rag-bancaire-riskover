"""
test_compare_sql.py - Tests hors ligne de src/compare_sql.py (faux modele, pas de serveur).

Usage (depuis rag_bancaire) :
    python -m unittest tests.test_compare_sql -v
"""
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.compare_sql as cmp
import src.text2sql as t2s
from src.create_riskover_db import build


class CompareSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "riskover.db"
        con = sqlite3.connect(cls.db)
        build(con)
        con.commit()
        con.close()
        cls._orig_log = t2s.LOG_FILE
        t2s.LOG_FILE = cls.tmp / "queries.log"
        cls.args = SimpleNamespace(model="fake", linking="off", show=5)

    @classmethod
    def tearDownClass(cls):
        t2s.LOG_FILE = cls._orig_log

    def run_case(self, question, ref_sql, model_sql):
        out = io.StringIO()
        with patch.object(t2s, "ask_llm", lambda model, messages: model_sql), redirect_stdout(out):
            ok = cmp.compare_one(question, ref_sql, self.args, self.db)
        return ok, out.getvalue()

    def test_same_result(self):
        ok, out = self.run_case("Combien de crédits en retard ?",
                                "SELECT COUNT(*) FROM credits WHERE statut = 'En retard';",
                                "SELECT COUNT(c.credit_id) FROM credits c WHERE c.statut = 'en retard'")
        self.assertTrue(ok)
        self.assertIn("MEME RESULTAT", out)

    def test_different_result(self):
        ok, out = self.run_case("Combien de crédits en retard ?",
                                "SELECT COUNT(*) FROM credits WHERE statut = 'En retard'",
                                "SELECT COUNT(c.credit_id) FROM credits c")
        self.assertFalse(ok)
        self.assertIn("DIFFERENT", out)

    def test_cannot_answer_and_model_refuses(self):
        ok, out = self.run_case("Quel est le salaire des clients ?", "-",
                                "SELECT 'HORS_BASE' AS erreur;")
        self.assertTrue(ok)
        self.assertIn("refuse d'inventer", out)

    def test_cannot_answer_but_model_invents(self):
        ok, out = self.run_case("Quel est le salaire des clients ?", "-",
                                "SELECT AVG(cr.montant) FROM credits cr")
        self.assertFalse(ok)

    def test_reference_select_star_allowed_but_not_writes(self):
        cols, rows = cmp.run_reference("SELECT * FROM clients", self.db)
        self.assertEqual(len(rows), 60)
        with self.assertRaises(ValueError):
            cmp.run_reference("DELETE FROM clients", self.db)

    def test_bad_reference_sql(self):
        ok, out = self.run_case("q", "SELECT nom FROM clients", "SELECT 1")
        self.assertIsNone(ok)
        self.assertIn("ne marche pas", out)

    def test_save_question(self):
        path = self.tmp / "mes_questions.csv"
        with redirect_stdout(io.StringIO()):
            cmp.save_question(path, "Combien ; de clients ?", "SELECT COUNT(*) FROM clients;", "simple")
            cmp.save_question(path, "Salaire ?", "-", "piege")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[2], "1;simple;Combien , de clients ?;match;SELECT COUNT(*) FROM clients")
        self.assertEqual(lines[3], "2;piege;Salaire ?;absent;")


if __name__ == "__main__":
    unittest.main()
