"""Проверки создания реестра руководств."""

import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pymupdf

from scripts import create_manual_registry


class ManualRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pdf = self.root / "Общее руководство.pdf"
        with pymupdf.open() as document:
            document.new_page()
            document.new_page()
            document.save(self.pdf)
        self.output = self.root / "annotations" / "manuals.json"
        self.manual = {
            "document_id": "shared-test",
            "title": "Руководство по ремонту",
            "edition": "2025",
            "engine_models": ["Двигатель 1", "Двигатель 2"],
        }

    def run_registry(self, expected_pages):
        args = ["--manuals-dir", str(self.root), "--output", str(self.output)]
        catalog = {self.pdf.name: {**self.manual, "page_count": expected_pages}}
        errors = io.StringIO()
        with patch.object(create_manual_registry, "MANUALS", catalog):
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                code = create_manual_registry.main(args)
        return code, errors.getvalue()

    def test_registers_models_physical_pages_and_pdf_hash(self):
        code, errors = self.run_registry(expected_pages=2)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), [{
            **self.manual,
            "path": self.pdf.as_posix(),
            "page_count": 2,
            "source_sha256": hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
        }])

    def test_wrong_page_count_leaves_previous_registry(self):
        self.output.parent.mkdir()
        self.output.write_text("[]\n", encoding="utf-8")
        before = self.output.read_bytes()

        code, errors = self.run_registry(expected_pages=3)

        self.assertEqual(code, 1)
        self.assertIn(self.pdf.name, errors)
        self.assertIn("2 стр.", errors)
        self.assertIn("3", errors)
        self.assertEqual(self.output.read_bytes(), before)
