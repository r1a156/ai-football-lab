/* V10_R15F_R3R6_PRODUCTION_REDESIGN */
(() => {
  "use strict";
  const STATE_URL = "data/state.json";
  const LIVE_URL = "data/live-state.json";
  const MIN_QUALITY = 58;
  const MOSCOW = "Europe/Moscow";
  const runtime = { state: null, live: null, records: new Map() };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    bindDialog();
    await refresh();
    window.setInterval(refresh, 60_000);
  }

  async function refresh() {
    setConnection("loading", "Обновление");
    try {
      const stamp = Date.now();
      const [stateResponse, liveResponse] = await Promise.all([
        fetch(`${STATE_URL}?v=${stamp}`, { cache: "no-store" }),
        fetch(`${LIVE_URL}?v=${stamp}`, { cache: "no-store" }).catch(() => null),
      ]);
      if (!stateResponse.ok) throw new Error(`state ${stateResponse.status}`);
      runtime.state = normalize(await stateResponse.json());
      runtime.live = liveResponse && liveResponse.ok ? await liveResponse.json() : {};
      render(runtime.state);
      setConnection("ready", "Актуально");
    } catch (error) {
      console.error(error);
      setConnection("error", "Нет связи");
      renderUnavailable();
    }
  }

  function normalize(value) {
    const state = value && typeof value === "object" ? value : {};
    state.meta = object(state.meta);
    state.dailyAnalysis = array(state.dailyAnalysis);
    state.bestBets = array(state.bestBets).length ? array(state.bestBets) : array(state.predictions);
    state.expresses = array(state.expresses);
    state.expressHistory = array(state.expressHistory);
    state.analysisHistory = array(state.analysisHistory);
    state.history = array(state.history);
    state.expressBank = Object.keys(object(state.expressBank)).length ? object(state.expressBank) : object(state.bank);
    state.statistics = object(state.statistics);
    return state;
  }

  function render(state) {
    runtime.records.clear();
    const current = isCurrentPortfolio(state);
    renderMeta(state, current);
    renderMatches(current ? state.dailyAnalysis : []);
    renderExpresses(current ? state.expresses : []);
    renderSingles(current ? state.bestBets.slice(0, 3) : []);
    renderBank(state);
    renderHistory(state);
    const notice = document.getElementById("staleNotice");
    notice.hidden = current;
    if (!current) {
      const date = state.meta.analysisDateLocal || "";
      setText("staleMessage", date
        ? `Подборка от ${formatDate(date)} больше не показывается как текущая. Новые матчи появятся после завершения проверки данных.`
        : "Предыдущие матчи убраны из текущего экрана. Здесь появятся только свежие проверенные данные.");
    }
  }

  function isCurrentPortfolio(state) {
    const daily = state.dailyAnalysis;
    const expresses = state.expresses;
    if (daily.length !== 15 || expresses.length !== 3) return false;
    if (!expresses.every(ticket => array(ticket.legs).length === 5)) return false;
    const marker = String(state.meta.sourceMarker || "");
    if (!marker.includes("R15")) return false;
    if (!daily.every(row => String(row.dataTier || "").toUpperCase() !== "MARKET" && number(row.dataQuality) >= MIN_QUALITY)) return false;
    const end = Date.parse(state.meta.operationalWindowEnd || "");
    if (Number.isFinite(end)) return end > Date.now() - 15 * 60_000;
    const updated = Date.parse(state.meta.updatedAt || "");
    return Number.isFinite(updated) && Date.now() - updated < 30 * 60 * 60_000;
  }

  function renderMeta(state, current) {
    const quality = current ? average(state.dailyAnalysis.map(row => number(row.dataQuality))) : 0;
    const bank = state.expressBank;
    setText("summaryDate", new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW, day: "numeric", month: "long" }).format(new Date()));
    setText("summaryMatches", current ? "15" : "—");
    setText("summaryExpresses", current ? "3" : "—");
    setText("summarySingles", current ? String(Math.min(3, state.bestBets.length)) : "—");
    setText("summaryQuality", current ? `${formatNumber(quality, 0)}/100` : "—");
    setText("summaryBank", currency(bank.current ?? bank.starting ?? 10000));
    const placed = number(bank.placedAmount ?? bank.activeExposure);
    setText("summaryExposure", placed > 0 ? `${currency(placed)} в работе` : "банк свободен");
    setText("portfolioStatus", current ? "Свежая подборка опубликована" : "Новая подборка формируется");
    setText("portfolioUpdated", state.meta.updatedAt ? `Обновлено ${formatDateTime(state.meta.updatedAt)}` : "Ожидаем обновление");
    setText("matchesUpdated", current && state.meta.updatedAt ? formatShortDateTime(state.meta.updatedAt) : "ожидание");
    setText("footerUpdated", state.meta.updatedAt ? `Данные: ${formatDateTime(state.meta.updatedAt)}` : "Данные загружаются");
    document.getElementById("heroStatus")?.classList.toggle("is-ready", current);
  }

  function renderMatches(rows) {
    const root = document.getElementById("matchList");
    if (!rows.length) {
      root.innerHTML = empty("Свежие матчи ещё не опубликованы");
      return;
    }
    root.innerHTML = rows.map((row, index) => {
      const key = remember(row, `match-${index}`);
      return `<article class="match-card" data-record="${escapeHtml(key)}" tabindex="0" role="button" aria-label="Открыть прогноз ${escapeHtml(teamsText(row))}">
        <div class="rank ${index < 3 ? "top" : ""}">${index + 1}</div>
        <div class="match-main"><div class="match-meta"><span>${escapeHtml(league(row))}</span><span>•</span><time>${escapeHtml(matchTime(row))}</time></div><div class="teams"><span>${escapeHtml(home(row))}</span><i>—</i><span>${escapeHtml(away(row))}</span></div></div>
        <div class="pick"><small>Прогноз</small><strong>${escapeHtml(pick(row))}</strong></div>
        <div class="metric metric-probability"><small>Вероятность</small><strong>${percent(probability(row))}</strong></div>
        <div class="metric metric-quality"><small>Качество</small><strong>${formatNumber(row.dataQuality, 0)}/100</strong></div>
        <div class="chevron">›</div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-record]").forEach(node => {
      node.addEventListener("click", () => openDetails(runtime.records.get(node.dataset.record)));
      node.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") openDetails(runtime.records.get(node.dataset.record)); });
    });
  }

  function renderExpresses(rows) {
    const root = document.getElementById("expressGrid");
    if (!rows.length) { root.innerHTML = empty("Экспрессы появятся вместе с новой подборкой"); return; }
    root.innerHTML = rows.map((ticket, index) => {
      const legs = array(ticket.legs);
      return `<article class="express-card">
        <div class="card-top"><div><span>КУПОН ${index + 1}</span><strong>${escapeHtml(ticket.title || ticket.name || `Экспресс ${index + 1}`)}</strong></div><b class="odds-badge">${formatNumber(ticket.combinedOdds ?? ticket.totalOdds ?? ticket.odds, 2)}</b></div>
        <ol class="leg-list">${legs.map((leg, legIndex) => `<li><b>${legIndex + 1}</b><div><strong>${escapeHtml(teamsText(leg))}</strong><small>${escapeHtml(pick(leg))}</small></div><em>${formatNumber(leg.odds ?? leg.bookmakerOdds, 2)}</em></li>`).join("")}</ol>
        <div class="express-footer"><span>Ставка <strong>${currency(ticket.stake)}</strong></span><span>Вероятность <strong>${percent(probability(ticket))}</strong></span></div>
      </article>`;
    }).join("");
  }

  function renderSingles(rows) {
    const root = document.getElementById("singleGrid");
    if (!rows.length) { root.innerHTML = empty("Топ‑3 появится после публикации свежего рейтинга"); return; }
    root.innerHTML = rows.map((row, index) => {
      const key = remember(row, `single-${index}`);
      return `<article class="single-card" data-record="${escapeHtml(key)}" tabindex="0" role="button">
        <div class="single-rank">${index + 1}</div>
        <div class="single-teams"><small>${escapeHtml(league(row))} · ${escapeHtml(matchTime(row))}</small><span>${escapeHtml(home(row))}</span><span>${escapeHtml(away(row))}</span></div>
        <div class="single-pick"><span>Прогноз</span><strong>${escapeHtml(pick(row))}</strong></div>
        <div class="single-metrics"><div><span>Вероятность</span><strong>${percent(probability(row))}</strong></div><div><span>Коэффициент</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>Качество</span><strong>${formatNumber(row.dataQuality,0)}</strong></div></div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-record]").forEach(node => node.addEventListener("click", () => openDetails(runtime.records.get(node.dataset.record))));
  }

  function renderBank(state) {
    const bank = state.expressBank;
    const current = number(bank.current ?? bank.starting ?? 10000);
    const starting = number(bank.starting ?? 10000);
    const placed = number(bank.placedAmount ?? bank.activeExposure);
    const available = number(bank.available ?? current - placed);
    const profit = number(bank.profit ?? current - starting);
    setText("bankCurrent", currency(current));
    setText("bankStarting", currency(starting));
    setText("bankPlaced", currency(placed));
    setText("bankAvailable", currency(available));
    setText("bankProfit", signedCurrency(profit));
    setText("bankChange", profit === 0 ? "без изменений" : `${profit > 0 ? "+" : ""}${formatNumber(starting ? profit / starting * 100 : 0, 1)}% от старта`);
  }

  function renderHistory(state) {
    const rows = [...state.expressHistory, ...state.history, ...state.analysisHistory]
      .filter(row => ["won","lost","push","void","cancelled"].includes(String(row.status || "").toLowerCase()))
      .sort((a,b) => Date.parse(b.settledAt || b.commenceTime || b.utcDate || 0) - Date.parse(a.settledAt || a.commenceTime || a.utcDate || 0));
    setText("historyCount", String(rows.length));
    const root = document.getElementById("historyList");
    if (!rows.length) { root.innerHTML = '<div class="empty-mini">Завершённых событий пока нет</div>'; return; }
    root.innerHTML = rows.slice(0,6).map(row => {
      const status = String(row.status || "").toLowerCase();
      const label = status === "won" ? "Выигрыш" : status === "lost" ? "Проигрыш" : status === "push" ? "Возврат" : "Закрыто";
      const title = row.legs ? (row.title || row.name || "Экспресс") : teamsText(row);
      const sub = row.legs ? `${array(row.legs).length} событий` : pick(row);
      const profit = number(row.profit ?? row.netProfit);
      return `<div class="history-row"><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(sub)}</small></div><span class="result-badge ${escapeHtml(status)}">${label}</span><b class="history-profit">${signedCurrency(profit)}</b></div>`;
    }).join("");
  }

  function openDetails(row) {
    if (!row) return;
    const dialog = document.getElementById("detailDialog");
    document.getElementById("dialogBody").innerHTML = `<div class="dialog-content">
      <span>${escapeHtml(league(row))} · ${escapeHtml(matchTime(row))}</span>
      <h3>${escapeHtml(home(row))} — ${escapeHtml(away(row))}</h3>
      <p>${escapeHtml(formatDateTime(row.commenceTime || row.utcDate || row.kickoff))}</p>
      <div class="dialog-pick"><span>Прогноз системы</span><strong>${escapeHtml(pick(row))}</strong></div>
      <div class="dialog-grid"><div><span>Вероятность</span><strong>${percent(probability(row))}</strong></div><div><span>Коэффициент</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>Качество данных</span><strong>${formatNumber(row.dataQuality,0)}/100</strong></div><div><span>Преимущество</span><strong>${signedPercent(row.edgePercent ?? number(row.edge)*100)}</strong></div></div>
      <div class="dialog-reason"><span>Почему выбран прогноз</span><p>${escapeHtml(row.reasonRu || row.reason || row.explanation || "Прогноз прошёл отбор по вероятности, качеству данных и рыночному сравнению.")}</p></div>
    </div>`;
    dialog.showModal();
  }

  function bindDialog() {
    const dialog = document.getElementById("detailDialog");
    document.getElementById("dialogClose")?.addEventListener("click", () => dialog.close());
    dialog?.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  }

  function renderUnavailable() {
    document.getElementById("matchList").innerHTML = empty("Не удалось загрузить данные. Страница повторит попытку автоматически.");
    document.getElementById("expressGrid").innerHTML = empty("Ожидаем соединение");
    document.getElementById("singleGrid").innerHTML = empty("Ожидаем соединение");
  }

  function setConnection(mode, text) {
    const node = document.getElementById("connectionState");
    node.classList.toggle("is-ready", mode === "ready");
    node.classList.toggle("is-error", mode === "error");
    node.querySelector("b").textContent = text;
  }

  function remember(row, fallback) { const key = String(row.id || row.eventId || fallback); runtime.records.set(key,row); return key; }
  function object(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
  function array(value) { return Array.isArray(value) ? value : []; }
  function number(value) { const n = Number(value); return Number.isFinite(n) ? n : 0; }
  function average(values) { return values.length ? values.reduce((a,b)=>a+number(b),0)/values.length : 0; }
  function home(row) { return String(row.homeRu || row.home || row.homeTeam?.name || row.homeTeam || "Хозяева"); }
  function away(row) { return String(row.awayRu || row.away || row.awayTeam?.name || row.awayTeam || "Гости"); }
  function teamsText(row) { return `${home(row)} — ${away(row)}`; }
  function league(row) { return String(row.leagueRu || row.league || row.competition || row.sportTitle || "Футбол"); }
  function odds(row) { return number(row.odds ?? row.bookmakerOdds ?? row.price ?? row.fairOdds); }
  function probability(row) { const raw = number(row.probabilityPercent ?? row.confidence ?? row.modelProbability ?? row.probability); return raw <= 1 && raw > 0 ? raw*100 : raw; }
  function pick(row) {
    if (row.pickRu || row.selectionLabelRu || row.marketLabelRu || row.pick) return String(row.pickRu || row.selectionLabelRu || row.marketLabelRu || row.pick);
    const code = String(row.market || row.marketCode || row.selectionCode || "").toUpperCase();
    const map = { HOME_WIN:`Победа ${home(row)}`, AWAY_WIN:`Победа ${away(row)}`, DRAW:"Ничья", HOME_OR_DRAW:`${home(row)} не проиграет`, AWAY_OR_DRAW:`${away(row)} не проиграет`, OVER_1_5:"Тотал больше 1,5", OVER_2_5:"Тотал больше 2,5", UNDER_2_5:"Тотал меньше 2,5", UNDER_3_5:"Тотал меньше 3,5", BOTH_TEAMS_SCORE:"Обе забьют", HOME_OVER_0_5:`${home(row)} забьёт`, AWAY_OVER_0_5:`${away(row)} забьёт` };
    return map[code] || code.replaceAll("_"," ") || "Прогноз матча";
  }
  function matchTime(row) { const value = row.commenceTime || row.utcDate || row.kickoff; if (!value) return "время уточняется"; try { return new Intl.DateTimeFormat("ru-RU",{timeZone:MOSCOW,day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); } catch { return "время уточняется"; } }
  function formatDateTime(value) { if (!value) return "—"; try { return new Intl.DateTimeFormat("ru-RU",{timeZone:MOSCOW,day:"numeric",month:"long",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); } catch { return "—"; } }
  function formatShortDateTime(value) { if (!value) return "—"; try { return new Intl.DateTimeFormat("ru-RU",{timeZone:MOSCOW,day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); } catch { return "—"; } }
  function formatDate(value) { try { const d = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T12:00:00Z`) : new Date(value); return new Intl.DateTimeFormat("ru-RU",{day:"numeric",month:"long",year:"numeric"}).format(d); } catch { return value; } }
  function formatNumber(value,digits=0) { return new Intl.NumberFormat("ru-RU",{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(number(value)); }
  function currency(value) { return `${formatNumber(value,0)} ₽`; }
  function signedCurrency(value) { const n=number(value); return `${n>0?"+":""}${formatNumber(n,0)} ₽`; }
  function percent(value) { return `${formatNumber(value,1)}%`; }
  function signedPercent(value) { const n=number(value); return `${n>0?"+":""}${formatNumber(n,1)}%`; }
  function setText(id,value) { const node=document.getElementById(id); if(node) node.textContent=String(value); }
  function empty(text) { return `<div class="empty-card">${escapeHtml(text)}</div>`; }
  function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]); }
})();
