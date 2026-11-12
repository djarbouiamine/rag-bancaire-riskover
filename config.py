"""
Configuration centralisée du projet rag_bancaire.
Toutes les variables clés et chemins de fichiers sont définis ici.
"""

from pathlib import Path
import os

# Chemins de base du projet
BASE_DIR = Path(__file__).resolve().parent

# Base de test RisKover (7 tables, donnees fictives) : python src/create_riskover_db.py
# N'importe quelle autre base SQLite peut etre interrogee avec --db chemin/vers/base.db
# (ou $env:RISKOVER_DB_PATH).
DB_DIR = BASE_DIR / "db"
RISKOVER_DB_PATH = Path(os.getenv("RISKOVER_DB_PATH", DB_DIR / "riskover.db"))

# Limites de lignes (modifiables aussi avec --limit et --show)
MAX_ROWS = int(os.getenv("MAX_ROWS", "200"))      # lignes max renvoyees par la base (LIMIT)
SHOW_ROWS = int(os.getenv("SHOW_ROWS", "10"))     # lignes affichees a l'ecran
ANSWER_ROWS = int(os.getenv("ANSWER_ROWS", "50"))  # lignes donnees au modele qui redige la reponse

# Logs
LOGS_DIR = BASE_DIR / "logs"
QUERY_LOG_PATH = LOGS_DIR / "queries.log"

# Dossiers de données
DATA_DIR = BASE_DIR / "data"          # data/cache : vecteurs bge-m3 (recalcules si absents)

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

# Schema linking (bge-m3) : le modele ne recoit que les tables utiles + les valeurs trouvees
# dans la question. Active des que la base a au moins LINKING_MIN_TABLES tables.
#   1  -> TOUJOURS active (choix du projet, teste le 11/11/2026 sur riskover.db :
#         24/25 et 12/15 comme sans linking, tables utiles toujours gardees, +0.5 s/question)
#   20 -> seulement pour les grandes bases
# Desactiver ponctuellement : --linking off
LINKING_MIN_TABLES = int(os.getenv("LINKING_MIN_TABLES", "1"))

