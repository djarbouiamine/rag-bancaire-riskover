"""
mschema.py - Genere la description de la base au format M-Schema (format XiYan-SQL).

Exemple de sortie :

    【DB_ID】 riskover
    【Schema】
    # Table: clients, clients de la banque (pseudonymises)
    [
    (client_id:TEXT, Primary Key, identifiant du client, format CLI_001),
    (secteur:TEXT, secteur d'activite, Examples: [BTP, Commerce, Finance]),
    ...
    ]
    【Foreign keys】
    clients.entreprise_id=entreprises.entreprise_id

Regles :
  - Les colonnes TEXT avec peu de valeurs distinctes (<= MAX_EXAMPLES) recoivent
    la liste COMPLETE de leurs valeurs (Examples). Le modele doit choisir dans cette liste.
  - Les identifiants, dates et nombres n'ont pas d'Examples (on ne veut pas que le modele
    se limite a 3 identifiants), seulement un format.
  - Les cles etrangeres sont lues automatiquement (PRAGMA foreign_key_list).
"""
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Set, Tuple

MAX_EXAMPLES = 20

# Descriptions metier des tables et colonnes (lues par le modele).
TABLE_DOCS: Dict[str, str] = {
    "entreprises": "entreprises clientes ou liees a un client",
    "clients": "clients de la banque (pseudonymises, aucune donnee nominative)",
    "credits": "credits accordes aux clients",
    "garanties": "garanties (suretes) adossees a un credit",
    "covenants": "covenants financiers suivis par trimestre pour un credit",
    "incidents_paiement": "incidents / retards de paiement sur un credit",
    "documents": "documents (rapports, emails, notes) relies au RAG par doc_id",
}

COLUMN_DOCS: Dict[str, str] = {
    "entreprises.entreprise_id": "identifiant de l'entreprise, format ENT_001",
    "entreprises.raison_sociale": "nom de l'entreprise",
    "entreprises.secteur": "secteur d'activite",
    "entreprises.ville": "ville du siege",
    "entreprises.taille": "taille de l'entreprise",
    "entreprises.forme_juridique": "forme juridique",
    "entreprises.date_creation": "date de creation, format AAAA-MM-JJ",
    "clients.client_id": "identifiant du client, format CLI_001",
    "clients.entreprise_id": "entreprise du client (vide pour un particulier)",
    "clients.type_client": "type de client",
    "clients.segment": "segment commercial",
    "clients.secteur": "secteur d'activite du client",
    "clients.tranche_age": "tranche d'age (particuliers et professionnels seulement)",
    "clients.ville": "ville du client",
    "clients.date_entree": "date d'entree en relation, format AAAA-MM-JJ",
    "credits.credit_id": "identifiant du credit, format CRD_0001",
    "credits.client_id": "client emprunteur",
    "credits.type_credit": "type de credit",
    "credits.montant": "montant accorde en dinars (TND)",
    "credits.taux": "taux d'interet annuel en %",
    "credits.duree_mois": "duree du credit en mois",
    "credits.date_octroi": "date d'octroi, format AAAA-MM-JJ",
    "credits.statut": "statut actuel du credit",
    "garanties.garantie_id": "identifiant de la garantie, format GAR_0001",
    "garanties.credit_id": "credit garanti",
    "garanties.type_garantie": "type de garantie",
    "garanties.valeur": "valeur de la garantie en dinars (TND)",
    "covenants.covenant_id": "identifiant du covenant, format COV_0001",
    "covenants.credit_id": "credit concerne",
    "covenants.indicateur": "indicateur financier suivi",
    "covenants.operateur": "sens du seuil : la valeur doit etre <= ou >= au seuil",
    "covenants.seuil": "seuil contractuel",
    "covenants.valeur_constatee": "valeur mesuree pour le trimestre",
    "covenants.trimestre": "trimestre de mesure",
    "covenants.respecte": "Oui si le covenant est respecte, Non s'il est depasse / non respecte",
    "incidents_paiement.incident_id": "identifiant de l'incident, format INC_0001",
    "incidents_paiement.credit_id": "credit concerne",
    "incidents_paiement.date_incident": "date de l'incident, format AAAA-MM-JJ",
    "incidents_paiement.jours_retard": "nombre de jours de retard",
    "incidents_paiement.montant_impaye": "montant impaye en dinars (TND)",
    "incidents_paiement.regularise": "Oui si l'incident est regularise",
    "documents.doc_id": "identifiant du document (commun avec les bases NoSQL et vectorielle), format DOC_0001",
    "documents.client_id": "client concerne (vide si document non lie)",
    "documents.entreprise_id": "entreprise concernee (vide si document non lie)",
    "documents.nom_fichier": "nom du fichier source",
    "documents.type_doc": "type de document",
    "documents.date_doc": "date du document, format AAAA-MM-JJ",
    "documents.lie": "Oui si le document est rapproche de la base, Non sinon",
}


def _is_text(ctype: str) -> bool:
    return any(t in (ctype or "").upper() for t in ("TEXT", "CHAR", "CLOB"))


def _is_identifier(table: str, col: str, pk: bool, fk_cols: Set[str]) -> bool:
    """Colonne identifiant (pas d'Examples) : cle primaire ou nom du type
    client_id, ID_TIERS, id, CustomerId, no_compte..."""
    c = col.lower()
    return (pk or c == "id" or c.endswith("_id") or c.startswith("id_") or c.startswith("no_")
            or (len(col) > 2 and col.endswith("Id") and col[-3].islower())   # CustomerId
            or c in ("nom_fichier", "lb_fichier"))


def _is_date(col: str) -> bool:
    c = col.lower()
    return "date" in c or c.startswith("dt_")


def _load_docs(db_path: Path):
    """Dictionnaire de donnees (descriptions des tables / colonnes), optionnel :
    1. <base>.docs.json a cote de la base, s'il existe ;
    2. sinon, pour riskover.db uniquement, les descriptions ecrites dans ce fichier ;
    3. sinon aucune description : le modele se base sur les noms des tables et colonnes."""
    p = db_path.with_suffix(".docs.json")
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("tables", {}), data.get("columns", {})
    if db_path.stem == "riskover":
        return TABLE_DOCS, COLUMN_DOCS
    return {}, {}


class DbSchema:
    """Lit une base SQLite une fois et garde tout ce qu'il faut au text-to-SQL :
    tables, colonnes texte, valeurs possibles (Examples), cles etrangeres, M-Schema."""

    def __init__(self, db_path: str | Path, db_id: str | None = None,
                 allowed_tables: List[str] | None = None):
        self.db_path = Path(db_path)
        self.db_id = db_id or self.db_path.stem
        self.table_docs, self.column_docs = _load_docs(self.db_path)
        self.tables: List[str] = []
        self.columns: Dict[str, List[Tuple[str, str, bool]]] = {}  # table -> [(col, type, pk)]
        self.foreign_keys: List[Tuple[str, str, str, str]] = []    # (t, col, ref_t, ref_col)
        self.examples: Dict[str, List[str]] = {}                   # "table.col" -> valeurs
        self.text_columns: Set[str] = set()                        # noms de colonnes TEXT
        self.fk_columns: Set[str] = set()                          # "table.col" cles etrangeres
        self._load(allowed_tables)

    def _load(self, allowed_tables):
        con = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
            if allowed_tables:
                names = [n for n in names if n in allowed_tables]
            self.tables = names
            for t in names:
                fks = con.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
                fk_cols = set()
                for fk in fks:
                    # (id, seq, table, from, to, on_update, on_delete, match)
                    if fk[2] in names:
                        self.foreign_keys.append((t, fk[3], fk[2], fk[4]))
                    fk_cols.add(fk[3])
                    self.fk_columns.add(f"{t}.{fk[3]}")
                cols = []
                for c in con.execute(f'PRAGMA table_info("{t}")').fetchall():
                    name, ctype, pk = c[1], c[2], bool(c[5])
                    if not ctype:
                        # SQLite accepte des colonnes sans type : on regarde les donnees
                        row = con.execute(f'SELECT typeof("{name}") FROM "{t}" '
                                          f'WHERE "{name}" IS NOT NULL LIMIT 1').fetchone()
                        ctype = {"text": "TEXT", "integer": "INTEGER",
                                 "real": "REAL"}.get(row[0] if row else "", "TEXT")
                    cols.append((name, ctype, pk))
                    if not _is_text(ctype):
                        continue
                    self.text_columns.add(name.lower())
                    if _is_identifier(t, name, pk, fk_cols) or _is_date(name):
                        continue
                    vals = con.execute(
                        f'SELECT DISTINCT "{name}" FROM "{t}" WHERE "{name}" IS NOT NULL '
                        f'ORDER BY 1 LIMIT {MAX_EXAMPLES + 1}').fetchall()
                    if 0 < len(vals) <= MAX_EXAMPLES:
                        self.examples[f"{t}.{name}"] = [v[0] for v in vals]
                self.columns[t] = cols
        finally:
            con.close()

    def examples_for_column(self, col: str) -> List[str]:
        """Valeurs possibles d'une colonne (nom seul, toutes tables confondues)."""
        col = col.lower().split(".")[-1]
        out: List[str] = []
        for key, vals in self.examples.items():
            if key.split(".")[1].lower() == col:
                out.extend(v for v in vals if v not in out)
        return out

    def column_doc(self, table: str, col: str) -> str:
        return self.column_docs.get(f"{table}.{col}", "")

    def table_doc(self, table: str) -> str:
        return self.table_docs.get(table, "")

    def to_mschema(self, tables: List[str] | None = None) -> str:
        """M-Schema de toute la base, ou seulement des tables donnees (schema linking)."""
        keep = [t for t in self.tables if tables is None or t in set(tables)]
        lines = [f"【DB_ID】 {self.db_id}", "【Schema】"]
        for t in keep:
            doc = self.table_docs.get(t)
            lines.append(f"# Table: {t}" + (f", {doc}" if doc else ""))
            lines.append("[")
            items = []
            for name, ctype, pk in self.columns[t]:
                parts = [f"{name}:{ctype or 'TEXT'}"]
                if pk:
                    parts.append("Primary Key")
                cdoc = self.column_docs.get(f"{t}.{name}")
                if cdoc:
                    parts.append(cdoc)
                ex = self.examples.get(f"{t}.{name}")
                if ex:
                    parts.append("Examples: [" + ", ".join(ex) + "]")
                items.append("(" + ", ".join(parts) + ")")
            lines.append(",\n".join(items))
            lines.append("]")
        fks = [fk for fk in self.foreign_keys if fk[0] in keep and fk[2] in keep]
        if fks:
            lines.append("【Foreign keys】")
            for t, c, rt, rc in fks:
                lines.append(f"{t}.{c}={rt}.{rc}")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    base = Path(__file__).resolve().parent.parent
    print(DbSchema(base / "db" / "riskover.db").to_mschema())
