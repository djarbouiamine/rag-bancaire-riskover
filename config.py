"""
Configuration centralisée du projet rag_bancaire.
Toutes les variables clés et chemins de fichiers sont définis ici.
"""

from pathlib import Path
import os

# Chemins de base du projet
BASE_DIR = Path(__file__).resolve().parent

# Base RisKover de test (7 tables reliees, donnees fictives), utilisee par src/text2sql.py
DB_DIR = BASE_DIR / "db"
RISKOVER_DB_PATH = Path(os.getenv("RISKOVER_DB_PATH", DB_DIR / "riskover.db"))

# Logs
LOGS_DIR = BASE_DIR / "logs"
QUERY_LOG_PATH = LOGS_DIR / "queries.log"

# Dossiers de données
DATA_DIR = BASE_DIR / "data"
DATA_RAW = DATA_DIR / "raw"
DATA_CLEAN = DATA_DIR / "clean"

# Serveur Ollama SUP'COM via tunnel SSH :
#   ssh -N -L 11435:127.0.0.1:11434 <utilisateur>@<adresse-serveur>
# (port local 11435 car le port 11434 est deja pris par l'Ollama installe sur le PC)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")

# Modele qui redige les reponses en francais (et, plus tard, l'agent)
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3:14b")

# Modele text-to-SQL (prompt au format M-Schema).
# Benchmark du 10/11/2026 : qwen2.5-coder:14b 24/25 (1.3 s/question), gpt-oss 25/25 (3.6 s),
# qwen3:14b 24/25 (16.8 s).
SQL_MODEL = os.getenv("SQL_MODEL", "qwen2.5-coder:14b")

# Modele d'embeddings (schema linking des grandes bases, puis RAG)
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

# Schema linking : au-dela de ce nombre de tables, on n'envoie au modele que les tables utiles
LINKING_MIN_TABLES = int(os.getenv("LINKING_MIN_TABLES", "20"))

