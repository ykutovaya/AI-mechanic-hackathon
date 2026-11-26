"""Проверяет замену поисковой коллекции и фильтрацию по исполнению без сети."""

from importlib import import_module, util
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models


class Vectors:
    signature = "тестовая модель 1"
    failed = False

    def vectors(self, texts):
        if self.failed:
            raise RuntimeError("ошибка кодирования")
        for text in texts:
            yield [1.0] + [0.0] * 383, models.SparseVector(indices=[1], values=[1.0])

    def query(self, text, hybrid):
        return next(self.vectors([text]))


def fragment(pair, page):
    text = f"Строка {page}."
    return dict(id=str(uuid5(NAMESPACE_URL, pair)), text=text, document_id="d", edition="1",
                section="Раздел", applicability=[pair], engine_models=[pair.split("|")[0]],
                source_pages=[page], page_parts=[{"page": page, "text": text}], title="Руководство")


class QdrantStorageTests(unittest.TestCase):
    def setUp(self):
        module = "ai_mechanic.knowledge._qdrant_store"
        self.assertIsNotNone(util.find_spec(module), "Нужно реализовать хранение и смену редакции")
        self.store = import_module(module)
        self.client = QdrantClient(":memory:")
        self.addCleanup(self.client.close)
        self.directory = self.enterContext(TemporaryDirectory())
        self.encoder = Vectors()
        self.rows = [fragment("Д|all", 1), fragment("Д|Б", 2), fragment("Е|all", 9)]

    def publish(self, rows):
        return self.store.replace_index(rows, client=self.client, collection="manuals",
                                        encoder=self.encoder, lock_dir=self.directory)

    def test_search_and_builder_share_default_alias(self):
        from unittest.mock import patch
        from ai_mechanic.knowledge import qdrant_index

        reader = self.store.QdrantSearch(client=self.client)
        self.assertEqual(reader.collection, "mechanic_hackathon")
        with patch.object(qdrant_index, "load_chunks", return_value=self.rows), \
             patch.object(qdrant_index, "replace_index", return_value=len(self.rows)) as publish:
            self.assertEqual(qdrant_index.main(["build"]), 0)
        self.assertEqual(publish.call_args.kwargs["collection"], reader.collection)

    def test_partial_build_requires_a_separate_alias(self):
        from unittest.mock import patch
        from ai_mechanic.knowledge import qdrant_index

        with patch.object(qdrant_index, "load_chunks") as load:
            self.assertEqual(qdrant_index.main(["build", "--document", "manual"]), 1)
        load.assert_not_called()

    def test_runtime_loads_pinned_e5_without_minilm(self):
        from pathlib import Path
        from unittest.mock import patch, Mock
        from types import SimpleNamespace
        import os

        model_dir = Path(self.directory)
        rev = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
        (model_dir / "REVISION").write_text(rev, encoding="ascii")
        tokenizer = Mock()
        tokenizer.encode.return_value.ids = [0, 2]
        with patch.dict(os.environ, {"AI_MECHANIC_E5_DIR": str(model_dir)}), \
             patch.object(self.store, "SparseTextEmbedding") as sparse, \
             patch.object(self.store, "TextEmbedding", create=True) as minilm, \
             patch("tokenizers.Tokenizer.from_file", return_value=tokenizer), \
             patch("onnxruntime.InferenceSession") as session:
            session.return_value.get_inputs.return_value = [SimpleNamespace(name="input_ids")]
            cache = model_dir / "fastembed"
            encoder = self.store.embeddings(cache)
            self.assertIn("intfloat/multilingual-e5-small|" + rev, encoder.signature)
            self.assertEqual(encoder.sparse, sparse.return_value)
            minilm.assert_not_called()
            self.assertEqual(Path(session.call_args.args[0]), model_dir / "onnx/model.onnx")

    def test_reader_pins_validated_collection_while_alias_changes(self):
        from unittest.mock import patch

        self.publish(self.rows)
        original_query = self.client.query_points
        def publish_new_version_then_read(collection, **kwargs):
            self.publish([dict(self.rows[0], text="другая редакция")])
            return original_query(collection, **kwargs)
        reader = self.store.QdrantSearch(client=self.client, collection="manuals", encoder=self.encoder)
        with patch.object(self.client, "query_points", side_effect=publish_new_version_then_read):
            self.assertEqual(reader.search("Д", "Строка")[0]["page"], 1)
        self.assertEqual(self.client.count("manuals", exact=True).count, 1)

    def test_foreign_revision_rejected_before_model_load(self):
        import os
        from pathlib import Path
        from unittest.mock import patch

        model_dir = Path(self.directory)
        (model_dir / "REVISION").write_text("другая ревизия", encoding="utf8")
        with patch.dict(os.environ, {"AI_MECHANIC_E5_DIR": str(model_dir)}), \
             patch.object(self.store, "TextEmbedding", create=True), \
             patch.object(self.store, "SparseTextEmbedding"):
            with self.assertRaisesRegex(ValueError, "ревизи"):
                self.store.embeddings(model_dir / "fastembed")

    def test_search_rejects_wrong_encoder_even_with_same_dimension(self):
        self.publish(self.rows)
        other = Vectors()
        other.signature = "другая модель 384"
        search = self.store.QdrantSearch(client=self.client, collection="manuals", encoder=other)
        with self.assertRaisesRegex(ValueError, "модел"):
            search.search("Д", "Строка")

    def test_previous_unmarked_collection_uses_original_signature(self):
        digest = self.store._fingerprint(self.rows, self.encoder.signature)
        collection = "старый__" + digest + "_1234567890"
        self.client.create_collection(collection, vectors_config={"dense": models.VectorParams(
            size=384, distance=models.Distance.COSINE)})
        self.client.upsert(collection, [models.PointStruct(
            id=row["id"], vector={"dense": [1.0] + [0.0] * 383}, payload=row) for row in self.rows])
        search = self.store.QdrantSearch(client=self.client, collection=collection, mode="dense", encoder=self.encoder)
        self.assertEqual([p["page"] for p in search.search("Д", "Строка")], [1])
        wrong = Vectors()
        wrong.signature = "другая модель 384"
        search = self.store.QdrantSearch(client=self.client, collection=collection, mode="dense", encoder=wrong)
        with self.assertRaisesRegex(ValueError, "модел"):
            search.search("Д", "Строка")

    def test_hybrid_interleaves_chunks_including_distinct_hits_on_the_same_page(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.publish(self.rows)
        def point(name, pages):
            return SimpleNamespace(id=name, score=0.8, payload={"id": name, "source_pages": pages})

        original = point("first", [8, 9])
        lamp = point("lamp", [3])
        level = point("level", [3])
        causes = point("causes", [4])
        extra = point("extra", [6])
        rankings = [[original, extra], [lamp, original], [lamp, level], [causes]]
        search = self.store.QdrantSearch(client=self.client, collection="manuals", encoder=self.encoder)
        with patch.object(self.client, "query_points", side_effect=[
                SimpleNamespace(points=hits) for hits in rankings]):
            rows = search.search_chunks("Д", "Упало давление масла. Что проверить?", limit=5)
        self.assertEqual([r["id"] for r in rows], ["first", "lamp", "causes", "extra", "level"])
        self.assertEqual(rows[0]["source_pages"], [8, 9])

    def test_subqueries_keep_the_physical_collection_and_filters(self):
        from unittest.mock import patch

        self.publish(self.rows)
        question = "При проверке топливной аппаратуры как считать код по вспышкам лампы?"
        search = self.store.QdrantSearch(client=self.client, collection="manuals", encoder=self.encoder)
        for variant, pages in ((None, {1}), ("all", {1}), ("Б", {1, 2})):
            with self.subTest(variant=variant), patch.object(self.client, "get_aliases", wraps=self.client.get_aliases) as aliases, \
                 patch.object(self.client, "query_points", wraps=self.client.query_points) as query:
                hits = search.search("Д", question, variant=variant)
                self.assertEqual({r["page"] for r in hits}, pages)
                self.assertEqual(query.call_count, 2)
                self.assertEqual(aliases.call_count, 1)
                physical, = {call.args[0] for call in query.call_args_list}
                self.assertNotEqual(physical, "manuals")
                for call in query.call_args_list:
                    allowed = call.kwargs["query_filter"]
                    pairs = {"Д|all", "Д|Б"} if variant == "Б" else {"Д|all"}
                    self.assertEqual(set(allowed.must[0].match.any), pairs)
                    self.assertTrue(all(branch.filter == allowed for branch in call.kwargs["prefetch"]))

    def test_only_broad_hybrid_questions_use_extra_queries(self):
        from unittest.mock import patch

        self.publish(self.rows)
        oil = "Водитель говорит, что в системе смазки упал напор. О чём его расспросить?"
        cases = [("hybrid", oil, 12, 4), ("hybrid", oil, 1, 1), ("dense", oil, 12, 1),
                 ("hybrid", "Низкое давление масла. Каков порог срабатывания сигнализатора?", 12, 1),
                 ("hybrid", "Упало давление топлива при нормальном уровне масла. Что проверить?", 12, 1),
                 ("hybrid", "Давление масла не снижалось. Что проверить?", 12, 1),
                 ("hybrid", "Как заменить диагностическую кнопку?", 12, 1),
                 ("hybrid", "Проверка уровня масла в двигателе", 12, 1)]
        for mode, question, limit, count in cases:
            with self.subTest(question=question, mode=mode, limit=limit), patch.object(
                    self.encoder, "query", wraps=self.encoder.query) as encode:
                search = self.store.QdrantSearch(client=self.client, collection="manuals", encoder=self.encoder, mode=mode)
                hits = search.search_chunks("Д", question, limit=limit)
                self.assertEqual(encode.call_count, count)
                self.assertEqual(encode.call_args_list[0].args[0], question)
                self.assertLessEqual(len(hits), limit)

    def test_query_page_parts_respect_variant_in_both_modes(self):
        self.publish(self.rows)
        for mode in ("hybrid", "dense"):
            search = self.store.QdrantSearch(client=self.client, collection="manuals",
                                             encoder=self.encoder, mode=mode)
            for variant, expected in [(None, {1}), ("Б", {1, 2})]:
                hits = search.search("Д", "Строка", variant=variant, limit=5)
                self.assertEqual({r["page"] for r in hits}, expected)

    def test_vehicle_lookup_keeps_model_and_variant_as_one_pair(self):
        cross = dict(fragment("Д|А", 8), applicability=["Д|А", "Е|Б"],
                     engine_models=["Д", "Е"])
        self.publish(self.rows + [cross])
        vehicle = {"engine_model": "Д", "model": "Е"}
        for mode in ("hybrid", "dense"):
            reader = self.store.QdrantSearch(client=self.client, collection="manuals",
                                             encoder=self.encoder, mode=mode)
            self.assertEqual({hit["page"] for hit in reader.search_vehicle(vehicle, "Строка")}, {1})
            self.assertEqual({hit["page"] for hit in reader.search_vehicle(vehicle, "Строка", variant="Б")},
                             {1, 2})
            self.assertEqual({hit["page"] for hit in reader.search_vehicle(vehicle, "Строка", variant="А")},
                             {1, 8})

    def test_idempotence_failure_and_new_edition(self):
        self.publish(self.rows)
        self.encoder.failed = True
        self.publish(self.rows)
        changed = [dict(self.rows[0], text="Новая редакция", edition="2")]
        with self.assertRaises(RuntimeError):
            self.publish(changed)
        self.assertEqual(self.client.count("manuals").count, 3)
        self.encoder.failed = False
        self.publish(changed)
        points, _ = self.client.scroll("manuals", with_payload=True)
        self.assertEqual([p.payload["edition"] for p in points], ["2"])
