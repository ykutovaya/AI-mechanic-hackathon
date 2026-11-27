"""Проверяет независимые сведения о замере масла и лампе в диалоге."""

from decimal import Decimal
import unittest
from unittest.mock import patch

from ai_mechanic import web
import test_temperature_pressure_checks as zmz
import test_yamz_oil_pressure as yamz


class OilObservationStateTests(unittest.TestCase):
    def fixture(self, case_type):
        fixture = case_type()
        self.addCleanup(fixture.doCleanups)
        make_server = web.make_server
        workshop = []

        def capture(*args, **kwargs):
            server = make_server(*args, **kwargs)
            workshop.append(server.workshop)
            return server

        with patch.object(web, "make_server", side_effect=capture):
            fixture.setUp()
        return fixture, workshop[0]

    def test_oil_reading_and_lamp_are_independent_for_each_engine(self):
        cases = (
            ("ЯМЗ-236М2", yamz.YamzOilPressureFlowTests, "280", "кПа"),
            ("ЯМЗ-536", yamz.YamzOilPressureFlowTests, "380", "кПа"),
            ("ЗМЗ-40904.10", zmz.TemperaturePressureChecks, "1,2", "кгс/см²"),
        )
        for engine, fixture_type, value, unit in cases:
            with self.subTest(engine=engine):
                fixture, workshop = self.fixture(fixture_type)
                selection = fixture.selection(engine)
                key = (selection["machine_id"], selection["check_id"])
                first = fixture.post("/api/diagnose", selection)
                initial = getattr(workshop.observations[key], "oil", None)
                self.assertIsNotNone(initial, "Должно быть отдельное состояние наблюдений об уровне давления и лампе")
                self.assertIsNone(initial.measurement)
                self.assertIsNone(initial.lamp)

                reply = fixture.post("/api/ask", selection | {
                    "topic": first["topic"],
                    "question": ("Лампа аварийного давления масла загорелась на минимальном холостом ходу, "
                                 f"а контрольный манометр показал давление масла {value} {unit} "
                                 "на номинальных оборотах при температуре двигателя 90 °C."),
                })
                current = workshop.observations[key].oil
                self.assertEqual(current.measurement.value, Decimal(value.replace(",", ".")))
                self.assertEqual(current.measurement.instrument, "контрольный манометр")
                self.assertEqual(current.measurement.regime, "номинальные обороты")
                self.assertEqual(current.measurement.temperature, Decimal(90))
                self.assertTrue(current.lamp.lit)
                self.assertEqual(current.lamp.regime, "холостой ход")

                reading_with_comma = fixture.post("/api/ask", selection | {
                    "topic": reply["topic"],
                    "question": (f"Контрольный манометр показал {value} {unit}, "
                                 "на номинальных оборотах при температуре двигателя 90 °C."),
                })
                self.assertEqual(workshop.observations[key].oil, current)
                after_lamp = fixture.post("/api/ask", selection | {
                    "topic": reading_with_comma["topic"], "question": "Лампа аварийного давления масла погасла.",
                })
                updated = workshop.observations[key].oil
                self.assertEqual(updated.measurement, current.measurement)
                self.assertFalse(updated.lamp.lit)
                no_temperature = fixture.post("/api/ask", selection | {
                    "topic": after_lamp["topic"], "question": "Температуру замера не знаю.",
                })
                updated = workshop.observations[key].oil
                self.assertEqual(updated.measurement.value, current.measurement.value)
                self.assertIsNone(updated.measurement.temperature)
                self.assertFalse(updated.lamp.lit)
                fixture.post("/api/ask", selection | {
                    "topic": no_temperature["topic"], "question": "Фактическое давление масла не измеряли.",
                })
                updated = workshop.observations[key].oil
                self.assertFalse(updated.measurement.taken)
                self.assertIsNone(updated.measurement.value)
                self.assertFalse(updated.lamp.lit)
