"""Проверяет черновик после уточнения привода ремня или модели коробки передач."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf

from ai_mechanic import web
from test_generator_gearbox import CASES, FAMILIES, MANUALS, NOISE, YAMZ_236
from test_valve_belt_checks import BELTS, DRIVES
from test_web_flow import running


CLUTCH_PAGES = {
    63: "Техническое обслуживание двухдискового сцепления. Свободный ход муфты выключения, определяемый "
        "зазором между упорным кольцом и подшипником муфты (3,2-4,0 мм), регулировать по руководству транспортного средства.",
    120: "Ежедневная проверка работы сцепления на транспортном средстве включает проверку на наличие "
         "пробуксовки, ведения и посторонних шумов и стуков.",
    121: "Периодически контролировать правильность регулировки привода сцепления. Периодичность и порядок "
         "контроля и регулировки привода сцепления определяется руководством по эксплуатации транспортного средства.",
    122: "Проверка качества установки сцепления осуществляется на неработающем двигателе. Поворот соответствует "
         "перемещению муфты на 11...13 мм от двигателя.",
    125: "Отсутствие смазки в подшипнике муфты выключения или его износ. Смажьте муфту выключения сцепления "
         "через масленку, если шум не устранился - замените муфту с подшипником.",
}


class QualifiedOrderTests(unittest.TestCase):
    cases = {
        "belt": ("F04", BELTS, "Свист ремня навесного оборудования."),
        "gearbox": ("F07", "Коробка передач", CASES["S-0039"][2]),
        "S-0024": ("F06", "Сцепление",
                   "Шум в зоне картера сцепления на холостом ходу, меняется при нажатии педали"),
    }

    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        data = root / "data"
        (data / "raw").mkdir(parents=True)
        (data / "annotations").mkdir()
        vehicle = dict(vehicle_id="V", model="МАЗ", engine_model=YAMZ_236, sessions=[
            dict(session_id=case, vehicle_id="V", status="anomaly") for case in self.cases])
        (data / "raw/vehicles.json").write_text(json.dumps([vehicle]), encoding="utf-8")
        path = root / "guide.pdf"
        with pymupdf.open() as pdf:
            for n in range(1, 136):
                page = pdf.new_page()
                if n in CLUTCH_PAGES:
                    page.insert_htmlbox(page.rect, f"<p>{CLUTCH_PAGES[n]}</p>")
            pdf.save(path)
        manual = dict(document_id=MANUALS[YAMZ_236], path=path.name, page_count=135, title="Руководство",
                      edition="2010", engine_models=[YAMZ_236], source_sha256=sha256(path.read_bytes()).hexdigest())
        (data / "annotations/manuals.json").write_text(json.dumps([manual]), encoding="utf-8")
        self.pages = {28: DRIVES, 128: FAMILIES, 135: NOISE}
        pages = self.pages

        class Search:
            def search(self, engine, query, *, variant, limit):
                return [dict(manual, page=page, text=text, applicability=[engine + "|all"])
                        for page, text in pages.items()]

        self.quality = "good"
        def diagnose(_url, machine, check):
            code, title, symptoms = self.cases[check]
            return dict(vehicle_id=machine, session_id=check, status="anomaly", signal_quality=self.quality,
                        anomaly_type=code, anomaly_title=title, description=symptoms)
        self.enterContext(patch.object(web, "fetch_diagnosis", side_effect=diagnose))
        self.sender = Mock(return_value="TKT-test")
        self.enterContext(patch.object(web, "submit_order", self.sender))
        self.url = self.enterContext(running(web.make_server(("127.0.0.1", 0), root=root, search=Search(),
                                                            orders_db="orders.sqlite")))

    def post(self, endpoint, **payload):
        request = Request(self.url + "/api/" + endpoint, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def ask(self, case, question, **fields):
        return self.post("ask", machine_id="V", check_id=case, question=question, **fields)

    def diagnose(self, case):
        return self.post("diagnose", machine_id="V", check_id=case)

    def check_draft(self, case, confirmation, correction, withdrawal, word, new_word, page):
        start = self.diagnose(case)
        self.assertFalse(start["context_id"])
        answer = self.ask(case, confirmation, topic=start["topic"])
        self.assertIn(page, [s["page"] for s in answer["sources"]])
        cid = answer.get("context_id")
        self.assertTrue(cid, answer["text"])
        draft = self.post("orders/draft", context_id=cid)
        payload = draft["payload"]
        self.assertIn(word, " ".join(payload["works"]))
        self.assertEqual([r["page"] for r in payload["sources"]], [page])
        self.assertEqual(self.ask(case, confirmation, context_id=cid)["context_id"], cid)
        version = {key: draft[key] for key in ("draft_id", "revision")}
        changed = self.post("orders/update", context_id=cid, **version, changes={"priority": "high"})
        other = self.ask(case, "Какое моторное масло залить?", context_id=cid)
        self.assertEqual(self.post("orders/draft", context_id=cid)["revision"], changed["revision"])
        next_cid = self.ask(case, correction, topic=other["topic"], context_id=cid).get("context_id")
        self.assertTrue(next_cid)
        self.assertNotEqual(cid, next_cid)
        actual = self.post("orders/draft", context_id=next_cid)
        self.assertIn(new_word, " ".join(actual["payload"]["works"]))
        with self.assertRaises(HTTPError):
            self.post("orders/send", context_id=cid, draft_id=draft["draft_id"], revision=changed["revision"],
                      confirmed=True)
        self.assertEqual(self.ask(case, withdrawal, context_id=next_cid).get("context_id"), "")
        with self.assertRaises(HTTPError):
            self.post("orders/draft", context_id=next_cid)
        self.sender.assert_not_called()

    def test_belt_confirmation_correction_and_withdrawal(self):
        self.check_draft("belt", "Ремень компрессора", "Это ремень генератора", "Не знаю, какой ремень",
                         "компрессора", "генератора", 28)

    def test_gearbox_confirmation_correction_and_withdrawal(self):
        self.check_draft("gearbox", "Установлена КП ЯМЗ-236П", "ЯМЗ-236Н", "Модель пока неизвестна",
                         "При ослаблении подтянуть", "ЯМЗ-236Н", 135)


    def test_gearbox_uses_exact_work_after_confirming_nameplate(self):
        start = self.diagnose("gearbox")
        prompt = ("Какая модель коробки передач установлена? "
                  "Перепишите полное обозначение с таблички коробки или из документации машины.")
        work = ("Проверить затяжку болтов крепления картера сцепления к картеру маховика и болтов крепления "
                "задней опоры коробки передач. При ослаблении подтянуть")
        self.assertIn(prompt, start["text"])
        self.assertFalse(start["context_id"])
        legacy_work = work.partition(".")[0] + " для КП ЯМЗ-236П."
        for requested_work in (work, legacy_work):
            unavailable = self.ask("gearbox", requested_work, topic=start["topic"])
            self.assertFalse(unavailable.get("context_id"))
        confirmed = self.ask("gearbox", "На табличке указано: ЯМЗ-236П", topic=start["topic"])
        self.assertIn("ЯМЗ-236П", confirmed["text"])
        self.assertTrue(any(s["url"].endswith(".pdf#page=135") for s in confirmed["sources"]))
        draft = self.post("orders/draft", context_id=confirmed["context_id"])
        self.assertEqual(draft["payload"]["works"], [work])
        self.assertEqual([source["page"] for source in draft["payload"]["sources"]], [135])
        version = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
        edited = self.post("orders/update", **version, changes={"works": [legacy_work]})
        version["revision"] = edited["revision"]
        restored = self.post("orders/update", **version, changes={"works": [work]})
        self.assertEqual(restored["payload"]["works"], [work])
        self.assertEqual([source["page"] for source in restored["payload"]["sources"]], [135])

        other = self.ask("gearbox", "На табличке ZF 9S1310", topic=confirmed["topic"], context_id=draft["context_id"])
        self.assertFalse(other.get("context_id"))
        self.assertIn("руководство", other["text"])
        with self.assertRaises(HTTPError):
            self.post("orders/draft", context_id=draft["context_id"])
        self.sender.assert_not_called()

    def test_gearbox_followup_keeps_server_model_even_with_different_client_topic(self):
        start = self.diagnose("gearbox")
        self.assertFalse(start["context_id"])
        wrong_model = json.dumps(["V", "gearbox", "gearbox-noise:ЯМЗ-236Н"])
        unconfirmed = self.ask("gearbox", "Что проверить?", topic=wrong_model)
        self.assertFalse(unconfirmed.get("context_id"))
        self.assertNotIn("ЯМЗ-236Н", unconfirmed["text"])
        original = self.ask("gearbox", "Установлена КП ЯМЗ-236П", topic=start["topic"])
        cid = original["context_id"]
        before = self.post("orders/draft", context_id=cid)
        for question, client_topic in [
            ("Что проверить?", wrong_model),
            ("Как проверить коробку передач при признаках этой неисправности?",
             json.dumps(["V", "gearbox", "oil-grade"])),
        ]:
            with self.subTest(question=question):
                answer = self.ask("gearbox", question, topic=client_topic, context_id=cid)
                self.assertFalse(answer.get("needs_clarification"), answer["text"])
                self.assertIn("ЯМЗ-236П", answer["text"])
                self.assertNotIn("ЯМЗ-236Н", answer["text"])
                self.assertTrue(any(s["page"] == 135 for s in answer["sources"]))
                self.assertEqual(self.post("orders/draft", context_id=cid), before)

    def test_previous_gearbox_replaced_with_zf_revokes_prior_work(self):
        self.diagnose("gearbox")
        valid = self.ask("gearbox", "Установлена КП ЯМЗ-236П")
        cid = valid["context_id"]
        before = self.post("orders/draft", context_id=cid)
        reply = self.ask("gearbox", "ЯМЗ-236П стояла раньше, сейчас стоит ZF", topic=valid["topic"], context_id=cid)
        self.assertEqual(json.loads(reply["topic"])[2], "gearbox-noise:unknown")
        self.assertEqual(reply.get("context_id"), "")
        self.assertFalse(any(s["page"] == 135 for s in reply["sources"]))
        again = self.ask("gearbox", "Что проверить?", topic=valid["topic"])
        self.assertNotIn("ЯМЗ-236П", again["text"])
        self.assertFalse(again.get("context_id"))
        with self.assertRaises(HTTPError):
            self.post("orders/send", context_id=cid, draft_id=before["draft_id"],
                      revision=before["revision"], confirmed=True)
        self.sender.assert_not_called()

    def test_client_topic_cannot_supply_confirmation_or_override_other_check(self):
        for case, state, question in [
            ("gearbox", "gearbox-noise:ЯМЗ-236П", "Как проверить крепление?"),
            ("belt", "accessory-belt", "Кажется, ремень компрессора"),
        ]:
            with self.subTest(case=case):
                forged = json.dumps(["V", case, state])
                self.assertFalse(self.ask(case, question, topic=forged).get("context_id"))
                self.diagnose(case)
                self.assertFalse(self.ask(case, question, topic=forged).get("context_id"))
        self.assertFalse(self.ask("belt", "ЯМЗ-236П", topic=json.dumps(["V", "gearbox", "gearbox-noise:ЯМЗ-236П"])).get("context_id"))
        self.quality = "bad"
        self.diagnose("belt")
        self.assertFalse(self.ask("belt", "Ремень компрессора").get("context_id"))
        self.sender.assert_not_called()

    def test_question_about_other_belts_preserves_the_observed_drive(self):
        self.diagnose("belt")
        cid = self.ask("belt", "Ремень компрессора")["context_id"]
        self.ask("belt", "Можно проверить ремень компрессора или генератора", context_id=cid)
        draft = self.post("orders/draft", context_id=cid)
        self.assertIn("компрессора", " ".join(draft["payload"]["works"]))

    def test_confirmation_without_supported_work_cannot_create_order(self):
        self.pages.pop(28)
        self.pages.pop(135)
        for case, answer in [("belt", "Ремень компрессора"), ("gearbox", "Установлена КП ЯМЗ-236П")]:
            with self.subTest(case=case):
                self.diagnose(case)
                self.assertFalse(self.ask(case, answer).get("context_id"))
        self.sender.assert_not_called()


    def test_s0024_waits_for_model_and_pedal_observation_in_separate_turns(self):
        start = self.diagnose("S-0024")
        self.assertIn("ЯМЗ-236К, ЯМЗ-181 или другое?", start["text"])
        self.assertIn("полностью выжатой педали", start["text"])
        self.assertFalse(start["context_id"])
        first = self.ask("S-0024", "По маркировке сцепление ЯМЗ-236К.")
        self.assertFalse(first.get("context_id"))
        self.assertIn("педал", first["text"])
        for unchecked in ("Может ли шум появиться при нажатии педали?", "При выжатой педали появляется вибрация.",
                          "При нажатии педали газа шум усиливается."):
            self.assertFalse(self.ask("S-0024", unchecked).get("context_id"))
        after = self.ask("S-0024", "При отпущенной педали шум есть, при полностью выжатой исчезает.")
        cid = after["context_id"]
        draft = self.post("orders/draft", context_id=cid)
        self.assertEqual(draft["payload"]["works"], ["Проверить свободный ход муфты выключения сцепления"])
        self.assertEqual([s["page"] for s in draft["payload"]["sources"]], [63])
        self.assertIn("3,2–4,0 мм", after["text"])
        self.assertTrue(any(s["url"].endswith("#page=63") for s in after["sources"]))
        revoke = self.ask("S-0024", "Модель сцепления пока неизвестна.", context_id=cid)
        self.assertFalse(revoke.get("context_id"))
        with self.assertRaises(HTTPError):
            self.post("orders/draft", context_id=cid)
        self.sender.assert_not_called()

    def test_s0024_accepts_short_replies_about_pedal_noise(self):
        for reply in ("шум усиливется", "шум ослабевает", "шум остается прежним"):
            with self.subTest(reply=reply):
                start = self.diagnose("S-0024")
                self.assertFalse(start["context_id"])
                model = self.ask("S-0024", "ЯМЗ-236К")
                self.assertFalse(model.get("context_id"))
                self.assertIn("полностью выжатой педали", model["text"])
                for unchecked in ("Обороты усиливаются.", "Шум усиливается при нажатии на газ.",
                                  "Шум усиливается?", "Шум усиливается. Шум ослабевает."):
                    self.assertFalse(self.ask("S-0024", unchecked).get("context_id"))
                answer = self.ask("S-0024", reply)
                self.assertTrue(answer.get("context_id"), answer["text"])
                draft = self.post("orders/draft", context_id=answer["context_id"])
                self.assertEqual(draft["payload"]["works"], ["Проверить свободный ход муфты выключения сцепления"])
                self.assertEqual([source["page"] for source in draft["payload"]["sources"]], [63])
                self.assertIn("3,2–4,0 мм", answer["text"])
                self.assertTrue(any(source["url"].endswith("#page=63") for source in answer["sources"]))
        self.sender.assert_not_called()

    def test_s0024_yamz181_and_bearing_replacement_conditions(self):
        self.diagnose("S-0024")
        self.assertFalse(self.ask("S-0024", "Шум усиливается при нажатии педали.").get("context_id"))
        reply = self.ask("S-0024", "Установлено ЯМЗ-181-10")
        self.assertIn("11–13 мм", reply["text"])
        self.assertIn("остановленном двигателе", reply["text"])
        self.assertNotIn("3,2–4,0", reply["text"])
        draft = self.post("orders/draft", context_id=reply["context_id"])
        self.assertEqual(draft["payload"]["works"], ["Проверить работу сцепления и регулировку привода выключения"])
        self.assertEqual({s["page"] for s in draft["payload"]["sources"]}, {120, 121})
        replacement = "Заменить муфту выключения сцепления с подшипником"
        version = {key: draft[key] for key in ("context_id", "draft_id", "revision")}
        with self.assertRaises(HTTPError):
            self.post("orders/update", **version, changes={"works": [replacement]})
        defect = self.ask("S-0024", "Подтвердили износ подшипника муфты выключения сцепления.")
        self.assertIn("после смазки", defect["text"])
        unchanged = self.post("orders/draft", context_id=draft["context_id"])
        self.assertEqual(unchanged["payload"]["works"], draft["payload"]["works"])
        quiet = self.ask("S-0024", "После смазки муфты шум не сохранился.")
        still_check = self.post("orders/draft", context_id=quiet["context_id"])
        self.assertEqual(still_check["payload"]["works"], draft["payload"]["works"])
        approved = self.ask("S-0024", "После смазки муфты шум сохранился.")
        repair = self.post("orders/draft", context_id=approved["context_id"])
        self.assertEqual(repair["payload"]["works"], [replacement])
        self.assertEqual([s["page"] for s in repair["payload"]["sources"]], [125])
        self.ask("S-0024", "Не подтвердили, что подшипник муфты выключения сцепления неисправен.",
                 context_id=approved["context_id"])
        with self.assertRaises(HTTPError):
            self.post("orders/draft", context_id=approved["context_id"])
        self.sender.assert_not_called()

    def test_s0024_uses_gearbox_checks_after_localizing_noise_there(self):
        self.diagnose("S-0024")
        standalone = self.ask("S-0024", "На табличке ЯМЗ-236П.")
        self.assertFalse(standalone.get("context_id"))
        location = self.ask("S-0024", "Шум идёт из коробки передач, а не из сцепления.")
        self.assertIn("модель коробки передач", location["text"])
        self.assertFalse(location.get("context_id"))
        confirmed = self.ask("S-0024", "На табличке указано: ЯМЗ-236П.")
        draft = self.post("orders/draft", context_id=confirmed["context_id"])
        self.assertEqual(draft["payload"]["fault_code"], "F06")
        self.assertEqual([s["page"] for s in draft["payload"]["sources"]], [135])
        self.assertIn("При ослаблении подтянуть", draft["payload"]["works"][0])
        self.sender.assert_not_called()

    def test_sent_belt_order_cannot_be_copied_after_changing_drive(self):
        self.diagnose("belt")
        cid = self.ask("belt", "Ремень компрессора").get("context_id")
        self.assertTrue(cid)
        draft = self.post("orders/draft", context_id=cid)
        sent = self.post("orders/send", context_id=cid, draft_id=draft["draft_id"], revision=draft["revision"],
                         confirmed=True)
        self.assertEqual(sent["number"], "TKT-test")
        copy = self.post("orders/new", context_id=cid, revision=sent["revision"])
        self.assertEqual(copy["payload"]["works"], draft["payload"]["works"])
        self.assertEqual(self.post("orders/new", context_id=cid, revision=sent["revision"])["context_id"],
                         copy["context_id"])
        self.ask("belt", "Ремень генератора", context_id=copy["context_id"])
        with self.assertRaises(HTTPError):
            self.post("orders/draft", context_id=copy["context_id"])
        with self.assertRaises(HTTPError):
            self.post("orders/new", context_id=cid, revision=sent["revision"])
        history = self.post("orders/history", machine_id="V")
        self.assertEqual([o["number"] for o in history["orders"]], ["TKT-test"])
        self.assertEqual(self.sender.call_count, 1)

