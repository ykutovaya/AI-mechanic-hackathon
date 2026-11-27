"use strict";

const el = id => document.getElementById(id);
const machine = el("machine"), check = el("check"), log = el("log");
let machines = [], topic = "", activeCheck = null, pending = null;

function node(tag, text, css) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (css) item.className = css;
  return item;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!body) throw new Error("Сервис вернул ответ в неизвестном формате.");
  if (!response.ok) {
    const error = new Error(body.error || "Не удалось получить ответ.");
    error.status = response.status;
    error.orderStatus = body.status;
    error.draft = body.draft;
    throw error;
  }
  return body;
}

function feedback(text = "", error = false) {
  const status = el("feedback");
  status.textContent = text;
  status.hidden = !text;
  status.classList.toggle("problem", error);
}

function controls() {
  const ready = !!machine.value;
  el("diagnose").disabled = !check.value || !!pending;
  orderDesk.refreshButtons();
  el("question").disabled = !ready;
  el("send").disabled = !ready || !!pending || !el("question").value.trim();
}

const empty = el("empty");
function reset() {
  orderDesk.clear();
  orderDesk.refreshHistory();
  if (pending) pending.abort();
  pending = null;
  topic = ""; activeCheck = null;
  log.replaceChildren(empty);
  el("question").value = "";
  feedback();
  controls();
}

const checkDate = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric"
});

function machineRow(row) {
  return {
    ...row, machine_id: row.machine_id ?? row.vehicle_id,
    history: (row.history ?? row.sessions ?? []).map(item => ({
      ...item, check_id: item.check_id ?? item.session_id
    }))
  };
}

function checkLabel(item) {
  if (!item.checked_at) return item.check_id;
  const date = new Date(item.checked_at);
  return Number.isNaN(date.getTime()) ? item.check_id : item.check_id + " · " + checkDate.format(date);
}

function chooseMachine() {
  const row = machines.find(m => m.machine_id === machine.value);
  check.replaceChildren(new Option("Без проверки", ""));
  for (const item of row?.history ?? []) {
    check.add(new Option(checkLabel(item), item.check_id));
  }
  check.selectedIndex = check.options.length - 1;
  check.disabled = !row?.history.length;
  el("current-engine").textContent = row?.engine_model || "";
  reset();
}

async function loadMachines() {
  el("reload").hidden = true;
  el("load-error").hidden = true;
  try {
    const data = await api("/api/machines");
    machines = (data.machines ?? data.vehicles ?? []).map(machineRow);
    machine.replaceChildren();
    const engines = new Map();
    for (const m of machines) {
      let group = engines.get(m.engine_model);
      if (!group) {
        group = document.createElement("optgroup");
        group.label = m.engine_model;
        machine.append(group);
        engines.set(m.engine_model, group);
      }
      const label = [m.machine_id, m.model].filter(Boolean).join(" · ");
      group.append(new Option(label, m.machine_id));
    }
    machine.disabled = !machines.length;
    if (!machines.length) throw new Error("Список машин пуст.");
    chooseMachine();
  } catch (e) {
    el("load-error").textContent = "Не удалось загрузить список машин. " + e.message;
    el("load-error").hidden = false;
    el("reload").hidden = false;
  }
}

function message(author) {
  const card = node("article", "", "message" + (author === "Вы" ? " user" : ""));
  card.append(node("h3", author));
  return card;
}

const statuses = {
  normal: "Отклонений не обнаружено", deviation: "Обнаружено отклонение",
  anomaly: "Обнаружено отклонение", recording_failed: "Сбой записи", check_failed: "Сбой проверки"
};
const qualities = {good: "Хорошее", warning: "С помехами", noisy: "С помехами", bad: "Непригодная запись"};

function recordingQuality(result) {
  return result.signal_quality ?? result.recording_quality;
}

function diagnosisUsable(result) {
  return ["normal", "deviation", "anomaly"].includes(result.status) && recordingQuality(result) !== "bad";
}

function diagnosticCard(result) {
  const box = node("section", "", "ml");
  const quality = recordingQuality(result), usable = diagnosisUsable(result);
  box.dataset.status = usable ? result.status : "bad";
  const status = !usable && quality === "bad" && !["check_failed", "recording_failed"].includes(result.status)
    ? "Запись непригодна для диагностики" : statuses[result.status] ?? "Результат недоступен";
  box.append(node("h4", "Результат ML · " + status));
  if (usable) {
    const title = result.anomaly_title ?? result.fault_name;
    const description = result.description ?? result.features;
    if (title) box.append(node("p", title));
    if (description) box.append(node("p", description));
  }
  const numbers = node("dl", "", "metrics");
  const pct = n => typeof n === "number" ? (n * 100).toLocaleString("ru-RU", {maximumFractionDigits: 2}) + " %" : null;
  const metrics = [
    ["Код отклонения", usable ? result.anomaly_type ?? result.fault_code : null],
    ["Оценка отклонения", usable ? pct(result.anomaly_score) : null],
    ["Уверенность ML", usable ? pct(result.confidence) : null],
    ["Качество записи", qualities[quality]],
    ["Версия модели", result.model_version]
  ];
  for (const [label, value] of metrics) {
    if (value == null || value === "") continue;
    const entry = node("div");
    entry.append(node("dt", label), node("dd", value));
    numbers.append(entry);
  }
  if (numbers.childElementCount) box.append(numbers);
  return box;
}

const sourceDialog = el("source-dialog");
el("source-back").addEventListener("click", () => sourceDialog.close());

function sourceCaption(source) {
  const name = [source.title, source.edition].filter(Boolean).join(", ");
  return name + " · стр. " + source.page + " PDF";
}

function link(source, text) {
  const a = node("a", text);
  a.href = source.page_url || source.url;
  a.addEventListener("click", event => {
    if (!source.page_url || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const title = sourceCaption(source);
    el("source-title").textContent = title;
    const sheet = el("source-sheet");
    sheet.alt = title;
    sheet.src = source.page_url + ".png";
    const pdf = el("source-pdf");
    pdf.href = source.url;
    pdf.textContent = "Открыть полный PDF на стр. " + source.page;
    sourceDialog.showModal();
  });
  return a;
}

function addParagraph(card, raw, sources) {
  const line = node("p");
  const start = raw.match(/^(По руководству|Возможные причины|Оценка риска|Общая проверка)\. /);
  if (start) {
    line.append(node("strong", start[1] + ". "));
    raw = raw.slice(start[0].length);
  }
  for (const span of raw.split(/(\[\d+\])/)) {
    const match = span.match(/^\[(\d+)\]$/);
    const source = match && sources[Number(match[1])-1];
    line.append(source ? link(source, span) : document.createTextNode(span));
  }
  card.append(line);
}

function render(reply, isDiagnostic) {
  const card = message("Цифровой механик");
  if (isDiagnostic && reply.diagnosis) {
    card.append(diagnosticCard(reply.diagnosis), node("h4", "Ответ агента"));
  }
  const sources = reply.sources ?? [];
  for (const para of reply.text.split(/\n\s*\n/)) {
    if (reply.diagnosis && para.startsWith("Наблюдение ML.")) continue;
    if (para) addParagraph(card, para, sources);
  }
  if (sources.length) {
    const refs = node("div", "", "refs");
    refs.append(node("strong", "Источники"));
    sources.forEach((source, i) => refs.append(link(source, "[" + (i + 1) + "] " + sourceCaption(source))));
    card.append(refs);
  }
  const labels = {bound: "Проверенная выдержка", route: "Тематический фрагмент", reading: "Страницы для сверки"};
  for (const entry of reply.reading_references ?? []) {
    const details = node("details", "", "refs");
    details.append(node("summary", (entry.section || "Руководство") + " · строка " + entry.row));
    details.append(node("strong", labels[entry.level]));
    for (const source of entry.sources) details.append(link(source, sourceCaption(source)));
    card.append(details);
  }
  for (const warning of reply.warnings ?? []) card.append(node("p", warning, "warning"));
  log.append(card);
  log.scrollTop = card.offsetTop - log.offsetTop;
}

async function requestAnswer(isDiagnostic) {
  if (pending || !machine.value) return;
  const question = el("question").value.trim();
  if (isDiagnostic ? !check.value : !question) return;
  const payload = {machine_id: machine.value, check_id: isDiagnostic ? check.value : activeCheck};
  if (isDiagnostic) {
    const previous = orderDesk.clear(false);
    if (previous) payload.previous_context_id = previous;
    activeCheck = null;
    topic = "";
  } else {
    Object.assign(payload, {question, topic});
    if (orderDesk.current()) payload.context_id = orderDesk.current();
  }
  const controller = new AbortController();
  pending = controller;
  controls();
  feedback(isDiagnostic ? "Запрашиваем диагностику и руководство…" : "Ищем ответ в руководстве…");
  try {
    const reply = await api(isDiagnostic ? "/api/diagnose" : "/api/ask", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload), signal:controller.signal
    });
    if (pending !== controller) return;
    empty.remove();
    if (isDiagnostic) {
      orderDesk.attach(reply);
      const d = reply.diagnosis;
      activeCheck = d && diagnosisUsable(d) ? check.value : null;
    } else {
      const ask = message("Вы"); ask.append(node("p",question)); log.append(ask);
      if (el("question").value.trim() === question) el("question").value = "";
      if ("context_id" in reply && reply.context_id !== orderDesk.current()) {
        orderDesk.clear(false);
        orderDesk.attach(reply);
      }
    }
    topic = reply.topic || "";
    render(reply, isDiagnostic);
    if (reply.order_context_changed) {
      orderDesk.clear(false);
      feedback("Данные ML изменились. Для новой заявки запросите диагностику ещё раз.");
    } else feedback();
  } catch (e) {
    if (pending === controller && e.name !== "AbortError") feedback(e.message || "Не удалось получить ответ.", true);
  } finally {
    if (pending === controller) { pending=null; controls(); }
  }
}

machine.addEventListener("change", chooseMachine);
check.addEventListener("change", reset);
el("diagnose").addEventListener("click", ()=>requestAnswer(true));
el("reload").addEventListener("click", loadMachines);
el("question").addEventListener("input", controls);
el("composer").addEventListener("submit", event=>{event.preventDefault(); requestAnswer(false);});
el("question").addEventListener("keydown", event=>{
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); requestAnswer(false);
  }
});
window.addEventListener("DOMContentLoaded", loadMachines);
