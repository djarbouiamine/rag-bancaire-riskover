"""
schema_linking.py - Pour les GRANDES bases : choisir les tables utiles et retrouver les valeurs.

Probleme : une base de 100 tables decrite en entier fait ~22 000 tokens. C'est trop long pour
le modele (le prompt est coupe) et il confond les tables qui se ressemblent.

Solution en 2 outils, executes AVANT d'appeler le modele SQL :

1. SchemaLinker  (« quelles tables ? »)
   - Chaque table et chaque colonne est decrite en une phrase (nom + dictionnaire de donnees).
   - bge-m3 transforme chaque phrase en vecteur (embedding) : une fois, puis mis en cache.
   - Pour une question : vecteur de la question -> similarite cosinus avec chaque table /
     colonne -> score par table (+ un petit bonus si des mots de la question apparaissent
     dans le nom ou la description de la table).
   - On garde les meilleures tables, puis on ajoute les tables « pont » necessaires pour
     les relier entre elles par les cles etrangeres (chemin le plus court dans le graphe).

2. ValueIndex  (« quelles valeurs exactes ? »)
   - Lit les valeurs distinctes des colonnes texte (localites, raisons sociales, libelles...).
   - Cherche dans la question des groupes de mots (n-grammes) egaux ou tres proches d'une
     valeur reelle (normalisation + distance de chaine : gere les fautes de frappe et les
     pluriels). Ex. « credits immobiliers » -> 'Crédit immobilier' (REF_PRODUIT_CRD.LB_PROD).
   - Les valeurs trouvees sont donnees au modele, et leurs tables sont ajoutees.

Usage direct (depuis rag_bancaire) :
    python src/schema_linking.py "credits en contentieu a kairouan"
    python src/schema_linking.py --db autre_base.db "ma question"
"""
import difflib
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import requests

try:
    from src.mschema import DbSchema, _is_identifier
except ImportError:
    from mschema import DbSchema, _is_identifier

STOPWORDS = set("""
le la les l un une des du de d au aux a et ou en dans par pour sur avec sans sous chez
qui que quoi quel quelle quels quelles dont est sont ont a-t-il y il ils elle elles on
ce cet cette ces son sa ses leur leurs mon ma mes notre nos votre vos tout tous toute toutes
combien liste lister donne donner montre montrer affiche afficher moi nous je tu
plus moins tres entre depuis avant apres ont-ils chaque the of and in for
""".split())


# Tables techniques : copies historiques / archives / chargement / temporaires.
# A adapter aux conventions de nommage de la vraie base RisKover.
TECH_TABLE = re.compile(r"(_HIST|_ARCH|_OLD|_BKP)$|^(STG|TMP|BKP)_", re.I)
TECH_WORDS = ("histor", "archiv", "staging", "tempor", "snapshot", "photo")


def norm(x: str) -> str:
    s = unicodedata.normalize("NFD", str(x).strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", norm(text))


def keywords(text: str) -> List[str]:
    return [t for t in tokens(text) if len(t) >= 3 and t not in STOPWORDS]


# =============================================================================
#  Embeddings (bge-m3 via Ollama), avec cache disque
# =============================================================================
class Embedder:
    """Transforme des textes en vecteurs. Les vecteurs deja calcules sont gardes dans un
    fichier JSON (cle = empreinte SHA-1 du texte) pour ne jamais les recalculer."""

    def __init__(self, ollama_url: str, model: str = "bge-m3",
                 cache_path: Optional[Path] = None,
                 embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,
                 batch_size: int = 64):
        self.url = ollama_url.rstrip("/")
        self.model = model
        self.cache_path = Path(cache_path) if cache_path else None
        self.embed_fn = embed_fn        # injection pour les tests (pas d'appel reseau)
        self.batch_size = batch_size
        self.cache: Dict[str, List[float]] = {}
        if self.cache_path and self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _key(self, text: str) -> str:
        return hashlib.sha1(f"{self.model}|{text}".encode("utf-8")).hexdigest()

    def _call(self, texts: List[str]) -> List[List[float]]:
        if self.embed_fn:
            return self.embed_fn(texts)
        r = requests.post(f"{self.url}/api/embed",
                          json={"model": self.model, "input": texts}, timeout=600)
        r.raise_for_status()
        return r.json()["embeddings"]

    def embed(self, texts: Sequence[str], save: bool = True) -> List[List[float]]:
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.cache]
        for i in range(0, len(missing), self.batch_size):
            chunk = missing[i:i + self.batch_size]
            for t, v in zip(chunk, self._call(chunk)):
                self.cache[self._key(t)] = v
        if missing and save and self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [self.cache[self._key(t)] for t in texts]


def _unit(v: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# =============================================================================
#  1. Choix des tables
# =============================================================================
class SchemaLinker:
    def __init__(self, schema: DbSchema, embedder: Embedder):
        self.schema = schema
        self.embedder = embedder
        # Une phrase par table et par colonne
        self.table_texts: Dict[str, str] = {}
        self.col_texts: List[Tuple[str, str]] = []   # (table, phrase)
        self.table_words: Dict[str, Set[str]] = {}
        for t in schema.tables:
            cols = schema.columns[t]
            col_desc = ", ".join(f"{c} ({schema.column_doc(t, c)})" if schema.column_doc(t, c)
                                 else c for c, _, _ in cols)
            self.table_texts[t] = f"Table {t} : {schema.table_doc(t)}. Colonnes : {col_desc}"
            for c, _, _ in cols:
                self.col_texts.append((t, f"{t}.{c} : {schema.column_doc(t, c)}"))
            words = set(tokens(t.replace("_", " ")))
            words |= set(keywords(schema.table_doc(t)))
            for c, _, _ in cols:
                words |= set(keywords(schema.column_doc(t, c)))
            self.table_words[t] = words
        texts = list(self.table_texts.values()) + [p for _, p in self.col_texts]
        vecs = [_unit(v) for v in embedder.embed(texts)]
        self.table_vecs = dict(zip(self.table_texts.keys(), vecs[:len(self.table_texts)]))
        self.col_vecs = [(t, v) for (t, _), v in zip(self.col_texts, vecs[len(self.table_texts):])]
        # Graphe des cles etrangeres (non oriente)
        self.graph: Dict[str, Set[str]] = defaultdict(set)
        for a, _, b, _ in schema.foreign_keys:
            self.graph[a].add(b)
            self.graph[b].add(a)

    def score_tables(self, question: str) -> List[Tuple[str, float]]:
        q = _unit(self.embedder.embed([question], save=False)[0])
        best_col: Dict[str, float] = defaultdict(float)
        for t, v in self.col_vecs:
            s = _dot(q, v)
            if s > best_col[t]:
                best_col[t] = s
        kws = keywords(question)
        wants_tech = any(k.startswith(TECH_WORDS) for k in kws)
        scores = []
        for t, v in self.table_vecs.items():
            sem = max(_dot(q, v), best_col[t])
            # bonus lexical : mots de la question presents dans la table (prefixe de 5 lettres
            # pour accepter singulier / pluriel : "credits" ~ "credit")
            hits = sum(1 for k in kws if any(w[:5] == k[:5] for w in self.table_words[t]))
            lex = hits / len(kws) if kws else 0.0
            # bonus « table centrale » : reliee aux autres par des cles etrangeres
            central = 0.02 * min(len(self.graph[t]), 4)
            # malus « table technique » (historique, archive, staging, temporaire)
            tech = 0.10 if (TECH_TABLE.search(t) and not wants_tech) else 0.0
            scores.append((t, sem + 0.15 * lex + central - tech))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    def _path(self, a: str, b: str, max_len: int = 3) -> List[str]:
        """Plus court chemin de cles etrangeres entre deux tables (tables intermediaires)."""
        prev = {a: None}
        queue = deque([(a, 0)])
        while queue:
            node, dist = queue.popleft()
            if node == b:
                path = []
                while node is not None:
                    path.append(node)
                    node = prev[node]
                return path[::-1]
            if dist >= max_len:
                continue
            for nxt in self.graph[node]:
                if nxt not in prev:
                    prev[nxt] = node
                    queue.append((nxt, dist + 1))
        return []

    def link(self, question: str, top_k: int = 6, max_tables: int = 10,
             extra_tables: Sequence[str] = ()) -> List[str]:
        ranked = [t for t, _ in self.score_tables(question)]
        selected: List[str] = []
        for t in list(extra_tables) + ranked[:top_k]:
            if t not in selected:
                selected.append(t)
        # Ajouter les tables pont pour que les tables choisies soient reliables
        anchor = selected[0]
        for t in selected[1:]:
            for mid in self._path(anchor, t)[1:-1]:
                if mid not in selected and len(selected) < max_tables:
                    selected.append(mid)
        return selected[:max_tables]


# =============================================================================
#  2. Recherche des valeurs
# =============================================================================
class ValueIndex:
    def __init__(self, schema: DbSchema, max_distinct: int = 50000, min_len: int = 4):
        self.schema = schema
        self.min_len = min_len
        self.exact: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)  # norm -> [(t,c,val)]
        self.by_letter: Dict[str, Set[str]] = defaultdict(set)
        con = sqlite3.connect(f"file:{schema.db_path.as_posix()}?mode=ro", uri=True)
        try:
            for t in schema.tables:
                for c, ctype, pk in schema.columns[t]:
                    if "TEXT" not in (ctype or "TEXT").upper() and ctype:
                        continue
                    if _is_identifier(t, c, pk, set()) or "date" in c.lower() or c.upper().startswith("DT_"):
                        continue
                    n = con.execute(f'SELECT COUNT(DISTINCT "{c}") FROM "{t}"').fetchone()[0]
                    if n == 0 or n > max_distinct:
                        continue
                    for (v,) in con.execute(f'SELECT DISTINCT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL'):
                        nv = norm(v)
                        if len(nv) < min_len:
                            continue
                        self.exact[nv].append((t, c, v))
                        self.by_letter[nv[0]].add(nv)
        finally:
            con.close()

    def search(self, question: str, max_hits: int = 8, cutoff: float = 0.86) -> List[Dict]:
        toks = tokens(question)
        hits: Dict[Tuple[str, str, str], Dict] = {}
        covered: List[Tuple[int, int]] = []   # morceaux deja reconnus (les plus longs d'abord)
        for n in range(5, 0, -1):
            for i in range(len(toks) - n + 1):
                if any(a <= i and i + n <= b for a, b in covered):
                    continue   # "immobiliers" deja couvert par "credits immobiliers"
                gram_toks = toks[i:i + n]
                if all(t in STOPWORDS for t in gram_toks):
                    continue
                gram = " ".join(gram_toks)
                if len(gram) < self.min_len:
                    continue
                if gram in self.exact:
                    matches = [(gram, 1.0)]
                elif len(gram) >= 5:
                    cands = difflib.get_close_matches(gram, self.by_letter.get(gram[0], ()),
                                                      n=3, cutoff=cutoff)
                    matches = [(m, difflib.SequenceMatcher(None, gram, m).ratio()) for m in cands]
                else:
                    matches = []
                if matches:
                    covered.append((i, i + n))
                for nv, score in matches:
                    for t, c, v in self.exact[nv]:
                        key = (t, c, v)
                        if key not in hits or hits[key]["score"] < score:
                            hits[key] = {"table": t, "colonne": c, "valeur": v,
                                         "texte_question": gram, "score": round(score, 3)}
        # Garder les meilleures : score puis longueur du texte reconnu
        ranked = sorted(hits.values(), key=lambda h: (h["score"], len(h["texte_question"])),
                        reverse=True)
        # Un meme morceau de question ne doit pas produire 15 valeurs : 3 max par morceau
        out, per_gram = [], defaultdict(int)
        for h in ranked:
            if per_gram[h["texte_question"]] >= 3:
                continue
            per_gram[h["texte_question"]] += 1
            out.append(h)
            if len(out) >= max_hits:
                break
        return out


if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import config

    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--db", default=str(config.RISKOVER_DB_PATH))
    args = p.parse_args()
    schema = DbSchema(args.db)
    emb = Embedder(config.OLLAMA_URL, config.EMBED_MODEL,
                   cache_path=config.DATA_DIR / "cache" / f"emb_{Path(args.db).stem}.json")
    linker = SchemaLinker(schema, emb)
    values = ValueIndex(schema)
    print("Valeurs trouvees :")
    for h in values.search(args.question):
        print(f"  {h['table']}.{h['colonne']} = '{h['valeur']}'  <- '{h['texte_question']}' ({h['score']})")
    print("Scores des tables (top 10) :")
    for t, s in linker.score_tables(args.question)[:10]:
        print(f"  {s:.3f}  {t}")
    print("Tables choisies :", linker.link(args.question))
