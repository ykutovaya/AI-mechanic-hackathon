"""Хранение фрагментов и два режима поиска по руководствам в Qdrant."""

from functools import lru_cache
from itertools import zip_longest
import hashlib
import os
import json
from pathlib import Path
from uuid import uuid4

from fastembed import SparseTextEmbedding
from filelock import FileLock
from qdrant_client import QdrantClient, models

from ._e5 import BM25_PARAMETERS, E5Embeddings, E5_REVISION
from .search_queries import followup_queries


URL = "http://127.0.0.1:6333"
COLLECTION = "mechanic_hackathon"
CACHE = Path(os.environ.get("FASTEMBED_CACHE_PATH", Path.home() / ".cache" / "fastembed"))
MODEL_DIR = Path.home() / ".cache" / "ai-mechanic" / "e5-small"


@lru_cache(maxsize=2)
def embeddings(cache_dir):
    """Открывает закреплённую E5 для документов и вопросов вместе с русским BM25."""
    cache = Path(cache_dir).resolve()
    model_dir = Path(os.environ.get("AI_MECHANIC_E5_DIR", MODEL_DIR))
    revision = (model_dir / "REVISION").read_text(encoding="utf-8-sig").strip()
    if revision != E5_REVISION:
        raise ValueError(f"Для поиска нужна ревизия E5 Small {E5_REVISION}; в {model_dir} указана {revision}.")
    sparse = SparseTextEmbedding("Qdrant/bm25", cache_dir=str(cache), language="russian", threads=4,
                                 **BM25_PARAMETERS)
    return E5Embeddings(model_dir, sparse, revision=revision)


def _fingerprint(rows, signature):
    digest = hashlib.sha256(("dense384-cosine-exact+bm25-idf:2|" + signature).encode())
    for row in sorted(rows, key=lambda row: row["id"]):
        data = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest.update(data.encode())
        digest.update(b"\n")
    return digest.hexdigest()[:24]


def replace_index(chunks, *, url=URL, collection=COLLECTION, client=None, encoder=None,
                  cache_dir=CACHE, lock_dir="data/processed", batch_size=32):
    """Публикует полную сборку одной заменой псевдонима. Предыдущая доступна до переключения."""
    rows = list(chunks)
    if not rows:
        raise ValueError("Пустой корпус. Действующая коллекция сохранена.")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("В списке фрагментов есть повторяющиеся UUID.")
    if batch_size <= 0:
        raise ValueError("Размер пакета должен быть положительным.")
    owned = client is None
    client = client if client is not None else QdrantClient(url=url, timeout=90)
    try:
        encoder = encoder if encoder is not None else embeddings(Path(cache_dir).resolve())
        digest = _fingerprint(rows, encoder.signature)
        lock_root = Path(lock_dir)
        lock_root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256((url + "/" + collection).encode()).hexdigest()[:16]
        with FileLock(lock_root / ("qdrant-" + key + ".lock")):
            active = next((r.collection_name for r in client.get_aliases().aliases
                           if r.alias_name == collection), None)
            prefix = collection + "__"
            stem = prefix + digest + "_"
            if active and active.startswith(stem) and client.count(active, exact=True).count == len(rows):
                return len(rows)
            if active is None and client.collection_exists(collection):
                raise ValueError(f"Имя {collection} занято отдельной коллекцией. Укажите другое имя для сборки.")
            target = stem + uuid4().hex[:10]
            client.create_collection(
                target, metadata={"encoder_signature": encoder.signature},
                vectors_config={"dense": models.VectorParams(size=384, distance=models.Distance.COSINE)},
                sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
                hnsw_config=models.HnswConfigDiff(m=0, payload_m=0),
            )
            try:
                client.create_payload_index(target, "applicability", models.PayloadSchemaType.KEYWORD, wait=True)
                for start in range(0, len(rows), batch_size):
                    batch = rows[start:start + batch_size]
                    texts = [r.get("embedding_text") or (r["section"] + "\n" + r["text"]) for r in batch]
                    vectors = encoder.vectors(texts)
                    points = [models.PointStruct(id=row["id"], vector={"dense": dense, "bm25": sparse},
                                                 payload=row) for row, (dense, sparse) in zip(batch, vectors, strict=True)]
                    client.upsert(target, points=points, wait=True)
                if client.count(target, exact=True).count != len(rows):
                    raise ValueError("Число сохранённых фрагментов отличается от числа загруженных.")
                operations = []
                if active:
                    operations.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=collection)))
                operations.append(models.CreateAliasOperation(create_alias=models.CreateAlias(
                    collection_name=target, alias_name=collection)))
                client.update_collection_aliases(operations)
            except Exception:
                # При потере ответа на переключение сервер уже мог принять новый псевдоним.
                if all(r.collection_name != target for r in client.get_aliases().aliases):
                    client.delete_collection(target)
                raise
            # Предыдущая версия остаётся доступной уже начатым запросам и для возврата.
            return len(rows)
    finally:
        if owned:
            client.close()


class QdrantSearch:
    """Ищет по разрешённым парам модели и исполнения и возвращает физические страницы."""

    def __init__(self, url=URL, collection=COLLECTION, mode="hybrid", *, client=None, encoder=None,
                 cache_dir=CACHE):
        if mode not in {"dense", "hybrid"}:
            raise ValueError("Режим поиска должен быть dense или hybrid.")
        self._owns_client = client is None
        self.client = client if client is not None else QdrantClient(url=url, timeout=30)
        self.collection = collection
        self.mode = mode
        self._encoder = encoder
        self.cache_dir = Path(cache_dir).resolve()
        self._verified_version = None

    def close(self):
        """Закрывает собственное соединение с Qdrant."""
        if self._owns_client:
            self.client.close()

    def _physical_collection(self, signature):
        """Закрепляет поиск за физической версией с совместимыми векторами."""
        physical = next((item.collection_name for item in self.client.get_aliases().aliases
                         if item.alias_name == self.collection), self.collection)
        if self._verified_version == (physical, signature):
            return physical
        metadata = self.client.get_collection(physical).config.metadata or {}
        if "encoder_signature" in metadata:
            compatible = metadata["encoder_signature"] == signature
        else:
            # Ранние сборки хранят отпечаток модели и фрагментов в физическом имени.
            rows = []
            offset = None
            while True:
                points, offset = self.client.scroll(physical, limit=128, offset=offset,
                                                    with_payload=True, with_vectors=False)
                rows.extend(point.payload for point in points)
                if offset is None:
                    break
            digest = _fingerprint(rows, signature)
            compatible = physical.rpartition("__")[2].partition("_")[0] == digest
        if not compatible:
            raise ValueError(f"Коллекция {physical} несовместима с моделью запроса. "
                             "Выберите коллекцию этой модели или пересоберите индекс.")
        self._verified_version = (physical, signature)
        return physical

    def search_chunks(self, engine, query, *, variant=None, limit=12):
        if not engine or not query.strip() or not 1 <= limit <= 100:
            return []
        pairs = [f"{engine}|all"]
        if variant and variant != "all":
            pairs.append(f"{engine}|{variant}")
        allowed = models.Filter(must=[models.FieldCondition(key="applicability", match=models.MatchAny(any=pairs))])
        encoder = self._encoder if self._encoder is not None else embeddings(self.cache_dir)
        physical = self._physical_collection(encoder.signature)
        primary = self._ranked_chunks(physical, encoder, query, allowed, limit)
        extra = followup_queries(query) if self.mode == "hybrid" and limit > 1 else ()
        if not extra:
            return primary

        rankings = [primary, *(self._ranked_chunks(physical, encoder, term, allowed, limit) for term in extra)]
        selected = {}
        for group in zip_longest(*rankings):
            for chunk in group:
                if chunk is not None:
                    selected.setdefault(chunk["id"], chunk)
                    if len(selected) == limit:
                        return list(selected.values())
        return list(selected.values())

    def _ranked_chunks(self, physical, encoder, query, allowed, limit):
        """Ищет фрагменты по одной формулировке в выбранной версии коллекции."""
        dense, sparse = encoder.query(query, self.mode == "hybrid")
        exact = models.SearchParams(exact=True)
        args = dict(query_filter=allowed, limit=limit, with_payload=True)
        if self.mode == "dense":
            args.update(query=dense, using="dense", search_params=exact)
        else:
            size = max(32, limit * 3)
            args.update(query=models.FusionQuery(fusion=models.Fusion.RRF), prefetch=[
                models.Prefetch(query=dense, using="dense", params=exact, filter=allowed, limit=size),
                models.Prefetch(query=sparse, using="bm25", filter=allowed, limit=size),
            ])
        result = self.client.query_points(physical, **args)
        return [dict(point.payload, score=point.score) for point in result.points]

    def search_vehicle(self, vehicle, query, *, variant=None, limit=12):
        """Ищет по модели двигателя из карточки и подтверждённому исполнению."""
        if not vehicle.get("engine_model"):
            raise ValueError("В карточке машины отсутствует модель двигателя.")
        return self.search(vehicle["engine_model"], query, variant=variant, limit=limit)

    def search(self, engine, query, *, variant=None, limit=12):
        hits = []
        for chunk in self.search_chunks(engine, query, variant=variant, limit=limit):
            for part in chunk["page_parts"]:
                hit = dict(chunk, **part)
                hit["tables"] = part.get("tables", chunk.get("tables", []) if len(chunk["source_pages"]) == 1 else [])
                hits.append(hit)
        return hits

