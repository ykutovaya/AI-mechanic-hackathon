"""Выбирает из руководства условия замены масла и ремонта клапанов."""
import re


def _seasonal_oil(search):
    for hit in search.find("Всесезонное моторное масло. Сезонное обслуживание и срок смены масла"):
        text = " ".join(hit["text"].split())
        for match in re.finditer(r"\bЕсли\b[^.!?]+[.!?]", text, re.I):
            rule = match[0]
            if (re.search(r"всесезонн\w*(?:\s+моторн\w*)?\s+масл", rule, re.I)
                    and re.search(r"\b(?:срок|интервал)", rule, re.I)
                    and re.search(r"\b(?:замен|менять|сменить)", rule, re.I)):
                search.add(hit, rule)
                return True
    return False


def _broken_valve_spring(search, valve, section):
    """Сохраняет условие и действие для клапана, названного в этом же абзаце."""
    for hit in search.find("Поломка пружины. " + valve + " клапан системы смазки. Замена"):
        for paragraph in re.split(r"\n\s*\n", hit["text"]):
            paragraph = " ".join(re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", paragraph).split())
            component = re.search(rf"\b{valve}\w*\s+клапан", paragraph, re.I)
            if not component:
                continue
            if section:
                description = re.split(r"[,;.!?]|\bили\b", paragraph[component.end():], maxsplit=1)[0]
                if not re.search(rf"\b{section}\w*\s+секци", description[:120], re.I):
                    continue
            for rule in re.finditer(r"\b(?:При|Если)\s+(?:неисправност|поломк|обрыв|разрушени)\w*"
                                    r"(?:(?:др\.|[^.!?]){0,140}?)\bзамен\w*\s+клапан\b[^.!?]{0,50}[.!?]",
                                    paragraph, re.I):
                if component.end() <= rule.start() and re.search(r"\bпружин", rule[0], re.I):
                    search.add(hit, rule[0])
                    return True
    return False


def select_oil_maintenance(search, question):
    """Возвращает применимую выдержку о запрошенном обслуживании."""
    if ("масл" in question and re.search(r"сезон|весной|осенью", question)
            and re.search(r"(?:смен|меня|замен|интервал|срок)", question)
            and not re.search(r"\b(?:како\w+\s+(?:марк|масл)|вязкост|марк[а-я]*\s+масл)", question)):
        return _seasonal_oil(search)
    if ("пружин" in question and re.search(r"полом|слом|облом|лопну|оборва|обрыв|разруш", question)
            and ("масл" in question or "насос" in question)):
        valve = re.search(r"\b(редукционн|предохранительн|дифференциальн)\w*\s+клапан", question)
        if valve:
            section = re.search(r"\b(основн|радиаторн)\w*\s+секци", question)
            return _broken_valve_spring(search, valve[1], section[1] if section else None)
    return False
