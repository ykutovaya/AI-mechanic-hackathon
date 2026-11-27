"""Проверяет получение п. 11.6 при пропуске обычной и экспертной выдачей."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

import pymupdf
from qdrant_client import QdrantClient

from ai_mechanic.ask import ManualSession
from ai_mechanic.knowledge._qdrant_store import QdrantSearch, replace_index
from test_diagnostic_works import TURBO
from test_qdrant_storage import Vectors


class TurboExactPageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        data = self.root / "data"
        (data / "raw").mkdir(parents=True)
        (data / "annotations").mkdir()
        pdf = data / "raw/guide.pdf"
        with pymupdf.open() as guide:
            for _ in range(217):
                guide.new_page()
            guide.save(pdf)
        self.engine = "ЯМЗ-536"
        self.doc = dict(document_id="yamz-536-re-2016", title="536.3902150 РЭ", edition="2016", page_count=217,
                        path="data/raw/guide.pdf", engine_models=[self.engine], source_sha256=sha256(pdf.read_bytes()).hexdigest())
        (data / "annotations/manuals.json").write_text(json.dumps([self.doc]), encoding="utf-8")
        vehicle = dict(vehicle_id="C206CC178", model="Машина", engine_model=self.engine,
                       sessions=[dict(session_id="S-0059", status="anomaly")])
        (data / "raw/vehicles.json").write_text(json.dumps({"vehicles": [vehicle]}), encoding="utf-8")
        self.client = QdrantClient(":memory:")
        self.addCleanup(self.client.close)
        self.encoder = Vectors()
        self.search = QdrantSearch(client=self.client, encoder=self.encoder, collection="turbo")
        self.dialog = ManualSession(root=self.root, search=self.search)

    def fragment(self, suffix, *, page=216, pair="ЯМЗ-536|all", row=TURBO, document=None):
        txt = " | ".join(row)
        part = dict(page=page, text=txt, tables=[dict(rows=[list(row)])])
        return dict(self.doc, id=str(uuid5(NAMESPACE_URL, suffix)), document_id=document or self.doc["document_id"],
                    kind="table", section="Пункт " + row[0].split()[0], applicability=[pair], source_pages=[page],
                    text=txt, page_parts=[part], fault_bindings=[dict(code="F09", rows=[481, 482], source_pages=[page], applicability=pair)])

    def publish(self, rows):
        replace_index(rows, collection="turbo", client=self.client, encoder=self.encoder, lock_dir=self.root)

    def ask(self):
        d = dict(vehicle_id="C206CC178", session_id="S-0059", status="anomaly", anomaly_type="F09",
                 anomaly_title="Турбокомпрессор", signal_quality="good", confidence=.9,
                 description="Свист и подвывание в районе турбокомпрессора, растёт с оборотами")
        return self.dialog.ask("C206CC178", "Что проверить в первую очередь?", check_id="S-0059", diagnosis=d)

    def test_page_216_is_found_when_both_ranked_searches_miss(self):
        self.publish([self.fragment("correct")])
        with patch.object(self.search, "_ranked_chunks", return_value=[]):
            reply = self.ask()
        self.assertEqual([216], [s["page"] for s in reply["sources"]])
        source, = reply["sources"]
        self.assertTrue(source["url"].endswith("#page=216"))
        self.assertEqual(source.get("expert_rows"), [481, 482])
        self.assertIn("Пункт 11.6, подпункт б", reply["text"])
        jobs = [s["text"] for s in reply["inspection_steps"] if s["role"] == "work"]
        self.assertEqual(jobs, ["Проверить крепление соединительных рукавов и состояние воздухопроводов после турбокомпрессора."])

    def test_neighbouring_page_wrong_variant_document_or_item_cannot_supply_work(self):
        adjacent = (TURBO[0].replace("11.6", "11.7"), TURBO[1])
        self.publish([self.fragment("variant", pair="ЯМЗ-536|судовое"),
                      self.fragment("another-engine", pair="ЯМЗ-236М2|all"),
                      self.fragment("another-book", document="yamz-536-rk-2013"),
                      self.fragment("next-page", page=217), self.fragment("other-item", row=adjacent)])
        with patch.object(self.search, "_ranked_chunks", return_value=[]):
            reply = self.ask()
            self.assertFalse(reply["sources"])
            self.assertFalse(reply["inspection_steps"])
            self.assertIn("нет проверки впуска по пункту 11.6", reply["text"])
            self.dialog.machines[0]["engine_variant"] = "судовое"
            permitted = self.ask()
            self.assertEqual([216], [s["page"] for s in permitted["sources"]])

    def test_ranked_expert_row_needs_no_exact_page_read(self):
        chunk = self.fragment("ranked")
        self.publish([chunk])
        with (patch.object(self.search, "_ranked_chunks", side_effect=lambda *args:
                           [chunk] if args[3].should else []),
              patch.object(self.client, "scroll", side_effect=AssertionError("Строка уже найдена"))):
            reply = self.ask()
        self.assertEqual([216], [s["page"] for s in reply["sources"]])
