"use strict";

const orderDesk = (() => {
  const form = el("order-dialog"), start = el("order-start"), send = el("order-send");
  const priorities = {low: "Низкий", medium: "Средний", high: "Высокий"};
  let context = "", sources = [], draft = null, busy = false, historyRequest = 0;

  const create = el("order-new"), copyReceipt = el("order-new-from-receipt");
  let latest = null, manuals = {}, selection = 0, diagnosed = false;

  function notice(text = "", error = false) {
    el("order-feedback").textContent = text;
    el("order-feedback").classList.toggle("problem", error);
  }

  function controls() {
    const sent = draft?.status === "sent" ? draft : latest;
    const canCopy = sent?.status === "sent" && sent.machine_id === machine.value &&
      sent.check_id === check.value && (!diagnosed || sent.context_id === context) &&
      (!draft || draft.status === "sent");
    create.hidden = !canCopy;
    create.disabled = busy || !!pending;
    copyReceipt.hidden = draft?.status !== "sent";
    copyReceipt.disabled = busy;
    start.hidden = !context || (!draft && canCopy);
    start.disabled = busy || !!pending;
    start.textContent = draft?.status === "sent" ? "Заявка " + draft.number :
      draft && draft.status !== "draft" ? "Результат отправки" : "Подготовить заявку";
    const locked = busy || !draft || draft.status !== "draft";
    el("order-fields").disabled = locked;
    el("order-actions").hidden = draft?.status !== "draft" && copyReceipt.hidden;
    send.hidden = !draft || draft.status !== "draft";
    send.disabled = locked;
  }

  function post(action, fields, options = {}) {
    return api("/api/orders/" + action, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(fields), ...options
    });
  }

  function receiptText(row) {
    if (row.status === "sent") {
      return row.number + " · " + new Date(row.sent_at).toLocaleString("ru-RU");
    }
    return row.status === "sending" ? "Отправляется" : "Результат отправки неизвестен";
  }

  function sourceLink(entry, registry = manuals) {
    const s = sources.find(item => item.document_id === entry.document_id && item.page === entry.page);
    if (s) return link(s, sourceCaption(s));
    const base = "/manuals/" + encodeURIComponent(entry.document_id);
    const manual = registry[entry.document_id] || {title: entry.document_id, edition: "PDF"};
    const source = {...manual, page: entry.page, page_url: base + "/page/" + entry.page,
                    url: base + ".pdf#page=" + entry.page};
    return link(source, sourceCaption(source));
  }

  async function refreshHistory() {
    const generation = ++historyRequest, owner = machine.value, selected = check.value;
    const pane = el("order-history");
    pane.hidden = true;
    pane.replaceChildren();
    latest = null;
    controls();
    if (!owner) return;
    try {
      const result = await post("history", {machine_id: owner});
      if (generation !== historyRequest || owner !== machine.value || selected !== check.value) return;
      manuals = result.manuals;
      latest = result.orders.find(row => row.check_id === selected) || null;
      controls();
      if (!result.orders.length) return;
      pane.append(node("strong", "Заявки этой машины"));
      const rows = result.orders.sort((a,b) => Number(b.check_id === selected) - Number(a.check_id === selected));
      for (const row of rows) {
        const p = row.payload, details = node("details");
        details.append(node("summary", row.check_id + " · " + receiptText(row)),
          node("p", "Код неисправности — " + p.fault_code), node("p", p.diagnosis),
          node("p", "Приоритет — " + priorities[p.priority] + ". " + row.priority_reason));
        for (const [title, list] of [["Работы", p.works], ["Запчасти", p.parts]]) {
          const group = node("ul");
          for (const value of list) {
            const manual = list === p.works && row.manual_works?.includes(value);
            group.append(node("li", manual ? value + " · запись механика" : value));
          }
          details.append(node("strong", title));
          details.append(list.length ? group : node("p", "Не указаны"));
        }
        const refs = node("div", "", "refs");
        refs.append(node("strong", "Источники диагностики и рекомендаций"));
        p.sources.forEach(s => refs.append(sourceLink(s, result.manuals)));
        details.append(refs);
        pane.append(details);
      }
      pane.hidden = false;
    } catch (error) {
      if (generation !== historyRequest) return;
      const retry = node("button", "Обновить историю", "order-link");
      retry.addEventListener("click", refreshHistory);
      pane.replaceChildren(node("p", "История заявок недоступна."), retry);
      pane.hidden = false;
    }
  }

  function clear(notifyServer = true) {
    const previous = context;
    selection++;
    diagnosed = false;
    context = ""; sources = []; draft = null; busy = false;
    form.close();
    controls();
    if (previous && notifyServer) post("close", {context_id: previous}, {keepalive: true}).catch(() => {});
    return previous;
  }

  function attach(reply) {
    diagnosed = true;
    context = reply.context_id || "";
    sources = reply.sources || [];
    controls();
  }

  function paint(row) {
    draft = row;
    el("order-heading").textContent = row.status === "draft" ? "Черновик заявки на дефектовку"
      : row.status === "sent" ? "Отправленная заявка " + row.number : "Результат отправки заявки";
    const p = row.payload, meta = el("order-meta");
    meta.replaceChildren();
    for (const [title, value] of [["Машина", p.machine_id], ["Проверка", p.check_id], ["Код неисправности ML", p.fault_code]]) {
      const group = node("div");
      group.append(node("dt", title), node("dd", value));
      meta.append(group);
    }
    el("order-diagnosis").value = p.diagnosis;
    el("order-priority").value = p.priority;
    el("order-works").value = p.works.join("\n");
    el("order-parts").value = p.parts.join("\n");
    el("order-lamp-row").hidden = !row.lamp_supported;
    el("order-lamp").checked = row.lamp_at_speed;
    el("order-reason").textContent = row.priority_reason;
    const refs = el("order-sources");
    refs.replaceChildren(node("strong", "Источники диагностики и рекомендаций"));
    p.sources.forEach(s => refs.append(sourceLink(s)));
    if (row.manual_works?.length) {
      refs.append(node("p", "Записи механика — " + row.manual_works.join(" · ")));
    }
    notice(row.status === "draft" ? "" : receiptText(row), row.status === "uncertain");
    controls();
  }

  function changed(event) {
    if (!draft || draft.status !== "draft") return;
    const lamp = el("order-lamp").checked;
    if (event.target === el("order-lamp")) el("order-priority").value = lamp ? "high" : draft.payload.priority;
    if (event.target === el("order-lamp") || event.target === el("order-priority")) {
      const priority = el("order-priority").value;
      let reason = priority === "medium" ? "Диагностика выявила отклонение, причина требует проверки."
        : "Приоритет выбран механиком.";
      if (lamp) {
        reason = "Механик сообщил, что лампа аварийного давления масла горела на средних оборотах.";
        if (priority !== "high") reason += " Рекомендован высокий приоритет. Итоговый приоритет выбран механиком.";
      }
      el("order-reason").textContent = reason;
    }
    notice();
    controls();
  }

  function edits() {
    const lines = id => el(id).value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    return {diagnosis: el("order-diagnosis").value.trim(), priority: el("order-priority").value,
            works: lines("order-works"), parts: lines("order-parts")};
  }

  async function openDraft() {
    if (!context || busy) return;
    const owner = context;
    busy = true;
    controls();
    try {
      const result = await post("draft", {context_id: owner});
      if (context !== owner) return;
      paint(result);
      if (!form.open) form.showModal();
    } catch (error) {
      if (context === owner) feedback(error.message, true);
    } finally {
      if (context === owner) { busy = false; controls(); }
    }
  }

  async function newDraft() {
    const source = draft?.status === "sent" ? draft : latest;
    if (pending || busy || source?.status !== "sent" || source.machine_id !== machine.value || source.check_id !== check.value) return;
    const owner = selection;
    busy = true;
    controls();
    feedback();
    try {
      const row = await post("new", {context_id: source.context_id, revision: source.revision});
      if (selection !== owner) return;
      context = row.context_id;
      activeCheck = row.check_id;
      paint(row);
      if (!form.open) form.showModal();
      form.scrollTop = 0;
    } catch (error) {
      if (selection === owner) {
        if (form.open) notice(error.message, true);
        else feedback(error.message, true);
      }
    } finally {
      if (selection === owner) { busy = false; controls(); }
    }
  }

  async function confirmShown() {
    if (!draft || busy || draft.status !== "draft") return;
    const owner = context, changes = edits(), lamp = el("order-lamp").checked;
    if (!draft.payload.sources.length) {
      notice("Для отправки нужна ссылка на руководство по этой диагностике.", true);
      return;
    }
    if (!changes.diagnosis || !changes.works.length) {
      notice("Укажите диагноз и хотя бы одну работу.", true);
      return;
    }
    const shown = {...draft.payload, ...changes};
    busy = true;
    controls();
    let sending = false;
    try {
      if (JSON.stringify(shown) !== JSON.stringify(draft.payload) || lamp !== draft.lamp_at_speed) {
        const revised = await post("update", {context_id: owner, draft_id: draft.draft_id,
          revision: draft.revision, changes, lamp_at_speed: lamp});
        if (context !== owner) return;
        paint(revised);
        if (JSON.stringify(shown) !== JSON.stringify(revised.payload) || revised.lamp_at_speed !== lamp) {
          notice("Состав изменился. Проверьте обновлённую заявку и подтвердите её.");
          return;
        }
      }
      sending = true;
      notice("Отправляем заявку в тестовую ТОиР…");
      const receipt = await post("send", {context_id: owner, draft_id: draft.draft_id,
        revision: draft.revision, confirmed: true});
      if (context !== owner) return;
      paint(receipt);
      if (receipt.status === "sent") {
        const card = message("Цифровой механик");
        card.append(node("p", "Тестовая ТОиР приняла заявку " + receipt.number + "."));
        log.append(card);
        log.scrollTop = log.scrollHeight;
      }
    } catch (error) {
      if (context !== owner) return;
      if (error.draft) {
        paint(error.draft);
        notice(error.message);
      } else if (sending && (!error.status || error.orderStatus === "uncertain")) {
        draft.status = "uncertain";
        notice("Результат отправки неизвестен. Проверьте историю этой машины.", true);
      } else {
        notice(error.message, true);
      }
    } finally {
      if (context === owner) {
        busy = false;
        controls();
        if (sending) refreshHistory();
      }
    }
  }

  start.addEventListener("click", openDraft);
  create.addEventListener("click", newDraft);
  copyReceipt.addEventListener("click", newDraft);
  function closeForm(event) {
    event.preventDefault();
    if (busy) return;
    if (draft?.status === "draft") {
      const changed = JSON.stringify({...draft.payload, ...edits()}) !== JSON.stringify(draft.payload)
        || el("order-lamp").checked !== draft.lamp_at_speed;
      if (changed && !window.confirm("Закрыть черновик и отменить изменения?")) return;
    }
    form.close();
  }
  el("order-close").addEventListener("click", closeForm);
  form.addEventListener("cancel", closeForm);
  el("order-fields").addEventListener("input", changed);
  send.addEventListener("click", confirmShown);
  return {clear, attach, refreshHistory, refreshButtons: controls, current: () => context};
})();
