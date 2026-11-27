"""Наблюдения механика о давлении масла и аварийном сигнализаторе."""

from dataclasses import dataclass, replace
from decimal import Decimal
import re

from .yamz236_oil import oil_measurement_text, oil_observation_clauses


_READING = re.compile(
    r"(?<![\w.,<>≤≥+\-−])(?:(выше|больше|ниже|меньше)\s+)?(\d+(?:[.,]\d+)?)"
    r"\s*(кпа|мпа|кгс\s*/\s*см(?:\^?2|²))(?!\w)"
)
_TEMPERATURE = re.compile(r"(?<![\w.,])(-?\d+(?:[.,]\d+)?)\s*(?:[°º]\s*[сc]|градус\w*)\b")
_SPEED = re.compile(
    r"\b(?:(?P<idle>(?:минимальн\w*\s+)?холост\w*\s+ход\w*)|"
    r"(?P<nominal>номинальн\w*\s+(?:оборот\w*|частот\w*\s+вращени\w*))|"
    r"(?P<medium>средн\w*\s+оборот\w*)|(?P<high>(?:высок\w*|больш\w*)\s+оборот\w*)|"
    r"(?P<number>\d+\s*(?:об\s*/\s*мин|оборот\w*\s+в\s+минуту)))\b"
)
_SPEED_NAMES = dict(idle="холостой ход", nominal="номинальные обороты",
                    medium="средние обороты", high="высокие обороты")
_INSTRUMENTS = (
    (re.compile(r"\bстрелочн\w+\s+указател\w*|\bстрелк\w+\s+указател\w*"), "стрелочный указатель"),
    (re.compile(r"\bдиагностическ\w+\s+прибор\w*"), "диагностический прибор"),
    (re.compile(r"\b(?:контрольн\w+|эталонн\w+)\s+манометр\w*"), "контрольный манометр"),
    (re.compile(r"\bманометр\w*"), "манометр"),
)
_NO_READING = re.compile(
    r"\bне\s+(?:был[оаи]\s+)?(?:измер\w*|замер\w*)|"
    r"\b(?:замера|измерения|показания)\s+(?:не было|нет|неизвестн\w*)"
)
_UNKNOWN_CONDITION = re.compile(
    r"(?:(?:уточняю|уточнение|поправка)\s*:\s*)?"
    r"(?:(?P<temperature>температур\w*)(?:\s+двигателя)?|(?P<regime>оборот\w*)|(?P<instrument>прибор\w*))"
    r"(?:\s+(?:замера|при замере))?\s+"
    r"(?:не зна\w*|неизвест\w*|не записал\w*|не измерял\w*)[.!]?"
)
_LAMP = re.compile(r"\b(?:ламп\w*|сигнализатор\w*)\b")
_OTHER_LAMP = re.compile(r"\b(?:ламп\w*|сигнализатор\w*)\s+(?:перегрев|заряд|аккумулятор)|диагностическ\w+\s+ламп")
_OFF = re.compile(r"\b(?:не\s+(?:гор\w*|загор\w*|загора\w*)|(?<!не )погас\w*|потух\w*)\b")
_ON = re.compile(r"\b(?:горит|горел\w*|загорел\w*|загора\w*|не\s+гаснет|не\s+погас\w*)\b")


def _regime(text):
    speed = _SPEED.search(text)
    return _SPEED_NAMES.get(speed.lastgroup, speed[0]) if speed else None


def _decimal(text):
    return Decimal(text.replace(",", "."))


@dataclass(frozen=True)
class OilMeasurement:
    """Записанный замер с единицами и условиями, указанными механиком."""

    taken: bool
    value: Decimal | None = None
    unit: str | None = None
    comparison: str | None = None
    instrument: str | None = None
    regime: str | None = None
    temperature: Decimal | None = None


@dataclass(frozen=True)
class OilLamp:
    """Состояние аварийной лампы и режим в её собственной реплике."""

    lit: bool
    regime: str | None


@dataclass(frozen=True)
class OilObservations:
    """Хранит показание прибора отдельно от наблюдения за лампой."""

    measurement: OilMeasurement | None = None
    lamp: OilLamp | None = None

    def with_report(self, message):
        correction = _UNKNOWN_CONDITION.fullmatch(message.casefold().replace("ё", "е").strip())
        if correction and self.measurement is not None:
            return replace(self, measurement=replace(self.measurement, **{correction.lastgroup: None}))
        measurement, lamp = self.measurement, self.lamp
        for clause in oil_observation_clauses(message):
            if _LAMP.search(clause) and not _OTHER_LAMP.search(clause):
                if _OFF.search(clause):
                    lamp = OilLamp(False, _regime(clause))
                elif _ON.search(clause):
                    lamp = OilLamp(True, _regime(clause))
            text = oil_measurement_text(clause)
            if _NO_READING.search(text):
                measurement = OilMeasurement(False)
                continue
            reading = _READING.search(text)
            if not reading or len(_READING.findall(text)) != 1:
                continue
            instrument = next((name for pattern, name in _INSTRUMENTS if pattern.search(text)), None)
            if _LAMP.search(clause) and instrument is None:
                continue
            comparison, value, unit = reading.groups()
            temperature = _TEMPERATURE.search(text)
            measurement = OilMeasurement(
                True, _decimal(value), {"кпа": "кПа", "мпа": "МПа"}.get(unit, "кгс/см²"),
                comparison, instrument, _regime(text), _decimal(temperature[1]) if temperature else None,
            )
        return replace(self, measurement=measurement, lamp=lamp)
