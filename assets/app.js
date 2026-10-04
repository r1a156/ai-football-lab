/* V10_R15F_R3R6_PRODUCTION_REDESIGN */
(() => {
  "use strict";
  const API_BASE = String(globalThis.FOOTBALL_API_BASE || "").replace(/\/+$/, "");
  const STATE_URL = API_BASE ? `${API_BASE}/data/state.json` : "data/state.json";
  const LIVE_URL = API_BASE ? `${API_BASE}/data/live-state.json` : "data/live-state.json";
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
    setConnection("loading", "РћР±РЅРѕРІР»РµРЅРёРµ");
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
      setConnection("ready", "РђРєС‚СѓР°Р»СЊРЅРѕ");
    } catch (error) {
      console.error(error);
      setConnection("error", "РќРµС‚ СЃРІСЏР·Рё");
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
        ? `РџРѕРґР±РѕСЂРєР° РѕС‚ ${formatDate(date)} Р±РѕР»СЊС€Рµ РЅРµ РїРѕРєР°Р·С‹РІР°РµС‚СЃСЏ РєР°Рє С‚РµРєСѓС‰Р°СЏ. РќРѕРІС‹Рµ РјР°С‚С‡Рё РїРѕСЏРІСЏС‚СЃСЏ РїРѕСЃР»Рµ Р·Р°РІРµСЂС€РµРЅРёСЏ РїСЂРѕРІРµСЂРєРё РґР°РЅРЅС‹С….`
        : "РџСЂРµРґС‹РґСѓС‰РёРµ РјР°С‚С‡Рё СѓР±СЂР°РЅС‹ РёР· С‚РµРєСѓС‰РµРіРѕ СЌРєСЂР°РЅР°. Р—РґРµСЃСЊ РїРѕСЏРІСЏС‚СЃСЏ С‚РѕР»СЊРєРѕ СЃРІРµР¶РёРµ РїСЂРѕРІРµСЂРµРЅРЅС‹Рµ РґР°РЅРЅС‹Рµ.");
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
    setText("summaryMatches", current ? "15" : "вЂ”");
    setText("summaryExpresses", current ? "3" : "вЂ”");
    setText("summarySingles", current ? String(Math.min(3, state.bestBets.length)) : "вЂ”");
    setText("summaryQuality", current ? `${formatNumber(quality, 0)}/100` : "вЂ”");
    setText("summaryBank", currency(bank.current ?? bank.starting ?? 10000));
    const placed = number(bank.placedAmount ?? bank.activeExposure);
    setText("summaryExposure", placed > 0 ? `${currency(placed)} РІ СЂР°Р±РѕС‚Рµ` : "Р±Р°РЅРє СЃРІРѕР±РѕРґРµРЅ");
    setText("portfolioStatus", current ? "РЎРІРµР¶Р°СЏ РїРѕРґР±РѕСЂРєР° РѕРїСѓР±Р»РёРєРѕРІР°РЅР°" : "РќРѕРІР°СЏ РїРѕРґР±РѕСЂРєР° С„РѕСЂРјРёСЂСѓРµС‚СЃСЏ");
    setText("portfolioUpdated", state.meta.updatedAt ? `РћР±РЅРѕРІР»РµРЅРѕ ${formatDateTime(state.meta.updatedAt)}` : "РћР¶РёРґР°РµРј РѕР±РЅРѕРІР»РµРЅРёРµ");
    setText("matchesUpdated", current && state.meta.updatedAt ? formatShortDateTime(state.meta.updatedAt) : "РѕР¶РёРґР°РЅРёРµ");
    setText("footerUpdated", state.meta.updatedAt ? `Р”Р°РЅРЅС‹Рµ: ${formatDateTime(state.meta.updatedAt)}` : "Р”Р°РЅРЅС‹Рµ Р·Р°РіСЂСѓР¶Р°СЋС‚СЃСЏ");
    document.getElementById("heroStatus")?.classList.toggle("is-ready", current);
  }

  function renderMatches(rows) {
    const root = document.getElementById("matchList");
    if (!rows.length) {
      root.innerHTML = empty("РЎРІРµР¶РёРµ РјР°С‚С‡Рё РµС‰С‘ РЅРµ РѕРїСѓР±Р»РёРєРѕРІР°РЅС‹");
      return;
    }
    root.innerHTML = rows.map((row, index) => {
      const key = remember(row, `match-${index}`);
      return `<article class="match-card" data-record="${escapeHtml(key)}" tabindex="0" role="button" aria-label="РћС‚РєСЂС‹С‚СЊ РїСЂРѕРіРЅРѕР· ${escapeHtml(teamsText(row))}">
        <div class="rank ${index < 3 ? "top" : ""}">${index + 1}</div>
        <div class="match-main"><div class="match-meta"><span>${escapeHtml(league(row))}</span><span>вЂў</span><time>${escapeHtml(matchTime(row))}</time></div><div class="teams"><span>${escapeHtml(home(row))}</span><i>вЂ”</i><span>${escapeHtml(away(row))}</span></div></div>
        <div class="pick"><small>РџСЂРѕРіРЅРѕР·</small><strong>${escapeHtml(pick(row))}</strong></div>
        <div class="metric metric-probability"><small>Р’РµСЂРѕСЏС‚РЅРѕСЃС‚СЊ</small><strong>${percent(probability(row))}</strong></div>
        <div class="metric metric-quality"><small>РљР°С‡РµСЃС‚РІРѕ</small><strong>${formatNumber(row.dataQuality, 0)}/100</strong></div>
        <div class="chevron">вЂє</div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-record]").forEach(node => {
      node.addEventListener("click", () => openDetails(runtime.records.get(node.dataset.record)));
      node.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") openDetails(runtime.records.get(node.dataset.record)); });
    });
  }

  function renderExpresses(rows) {
    const root = document.getElementById("expressGrid");
    if (!rows.length) { root.innerHTML = empty("Р­РєСЃРїСЂРµСЃСЃС‹ РїРѕСЏРІСЏС‚СЃСЏ РІРјРµСЃС‚Рµ СЃ РЅРѕРІРѕР№ РїРѕРґР±РѕСЂРєРѕР№"); return; }
    root.innerHTML = rows.map((ticket, index) => {
      const legs = array(ticket.legs);
      return `<article class="express-card">
        <div class="card-top"><div><span>РљРЈРџРћРќ ${index + 1}</span><strong>${escapeHtml(ticket.title || ticket.name || `Р­РєСЃРїСЂРµСЃСЃ ${index + 1}`)}</strong></div><b class="odds-badge">${formatNumber(ticket.combinedOdds ?? ticket.totalOdds ?? ticket.odds, 2)}</b></div>
        <ol class="leg-list">${legs.map((leg, legIndex) => `<li><b>${legIndex + 1}</b><div><strong>${escapeHtml(teamsText(leg))}</strong><small>${escapeHtml(pick(leg))}</small></div><em>${formatNumber(leg.odds ?? leg.bookmakerOdds, 2)}</em></li>`).join("")}</ol>
        <div class="express-footer"><span>РЎС‚Р°РІРєР° <strong>${currency(ticket.stake)}</strong></span><span>Р’РµСЂРѕСЏС‚РЅРѕСЃС‚СЊ <strong>${percent(probability(ticket))}</strong></span></div>
      </article>`;
    }).join("");
  }

  function renderSingles(rows) {
    const root = document.getElementById("singleGrid");
    if (!rows.length) { root.innerHTML = empty("РўРѕРївЂ‘3 РїРѕСЏРІРёС‚СЃСЏ РїРѕСЃР»Рµ РїСѓР±Р»РёРєР°С†РёРё СЃРІРµР¶РµРіРѕ СЂРµР№С‚РёРЅРіР°"); return; }
    root.innerHTML = rows.map((row, index) => {
      const key = remember(row, `single-${index}`);
      return `<article class="single-card" data-record="${escapeHtml(key)}" tabindex="0" role="button">
        <div class="single-rank">${index + 1}</div>
        <div class="single-teams"><small>${escapeHtml(league(row))} В· ${escapeHtml(matchTime(row))}</small><span>${escapeHtml(home(row))}</span><span>${escapeHtml(away(row))}</span></div>
        <div class="single-pick"><span>РџСЂРѕРіРЅРѕР·</span><strong>${escapeHtml(pick(row))}</strong></div>
        <div class="single-metrics"><div><span>Р’РµСЂРѕСЏС‚РЅРѕСЃС‚СЊ</span><strong>${percent(probability(row))}</strong></div><div><span>РљРѕСЌС„С„РёС†РёРµРЅС‚</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>РљР°С‡РµСЃС‚РІРѕ</span><strong>${formatNumber(row.dataQuality,0)}</strong></div></div>
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
    setText("bankChange", profit === 0 ? "Р±РµР· РёР·РјРµРЅРµРЅРёР№" : `${profit > 0 ? "+" : ""}${formatNumber(starting ? profit / starting * 100 : 0, 1)}% РѕС‚ СЃС‚Р°СЂС‚Р°`);
  }

  function renderHistory(state) {
    const rows = [...state.expressHistory, ...state.history, ...state.analysisHistory]
      .filter(row => ["won","lost","push","void","cancelled"].includes(String(row.status || "").toLowerCase()))
      .sort((a,b) => Date.parse(b.settledAt || b.commenceTime || b.utcDate || 0) - Date.parse(a.settledAt || a.commenceTime || a.utcDate || 0));
    setText("historyCount", String(rows.length));
    const root = document.getElementById("historyList");
    if (!rows.length) { root.innerHTML = '<div class="empty-mini">Р—Р°РІРµСЂС€С‘РЅРЅС‹С… СЃРѕР±С‹С‚РёР№ РїРѕРєР° РЅРµС‚</div>'; return; }
    root.innerHTML = rows.slice(0,6).map(row => {
      const status = String(row.status || "").toLowerCase();
      const label = status === "won" ? "Р’С‹РёРіСЂС‹С€" : status === "lost" ? "РџСЂРѕРёРіСЂС‹С€" : status === "push" ? "Р’РѕР·РІСЂР°С‚" : "Р—Р°РєСЂС‹С‚Рѕ";
      const title = row.legs ? (row.title || row.name || "Р­РєСЃРїСЂРµСЃСЃ") : teamsText(row);
      const sub = row.legs ? `${array(row.legs).length} СЃРѕР±С‹С‚РёР№` : pick(row);
      const profit = number(row.profit ?? row.netProfit);
      return `<div class="history-row"><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(sub)}</small></div><span class="result-badge ${escapeHtml(status)}">${label}</span><b class="history-profit">${signedCurrency(profit)}</b></div>`;
    }).join("");
  }

  function openDetails(row) {
    if (!row) return;
    const dialog = document.getElementById("detailDialog");
    document.getElementById("dialogBody").innerHTML = `<div class="dialog-content">
      <span>${escapeHtml(league(row))} В· ${escapeHtml(matchTime(row))}</span>
      <h3>${escapeHtml(home(row))} вЂ” ${escapeHtml(away(row))}</h3>
      <p>${escapeHtml(formatDateTime(row.commenceTime || row.utcDate || row.kickoff))}</p>
      <div class="dialog-pick"><span>РџСЂРѕРіРЅРѕР· СЃРёСЃС‚РµРјС‹</span><strong>${escapeHtml(pick(row))}</strong></div>
      <div class="dialog-grid"><div><span>Р’РµСЂРѕСЏС‚РЅРѕСЃС‚СЊ</span><strong>${percent(probability(row))}</strong></div><div><span>РљРѕСЌС„С„РёС†РёРµРЅС‚</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>РљР°С‡РµСЃС‚РІРѕ РґР°РЅРЅС‹С…</span><strong>${formatNumber(row.dataQuality,0)}/100</strong></div><div><span>РџСЂРµРёРјСѓС‰РµСЃС‚РІРѕ</span><strong>${signedPercent(row.edgePercent ?? number(row.edge)*100)}</strong></div></div>
      <div class="dialog-reason"><span>РџРѕС‡РµРјСѓ РІС‹Р±СЂР°РЅ РїСЂРѕРіРЅРѕР·</span><p>${escapeHtml(row.reasonRu || row.reason || row.explanation || "РџСЂРѕРіРЅРѕР· РїСЂРѕС€С‘Р» РѕС‚Р±РѕСЂ РїРѕ РІРµСЂРѕСЏС‚РЅРѕСЃС‚Рё, РєР°С‡РµСЃС‚РІСѓ РґР°РЅРЅС‹С… Рё СЂС‹РЅРѕС‡РЅРѕРјСѓ СЃСЂР°РІРЅРµРЅРёСЋ.")}</p></div>
    </div>`;
    dialog.showModal();
  }

  function bindDialog() {
    const dialog = document.getElementById("detailDialog");
    document.getElementById("dialogClose")?.addEventListener("click", () => dialog.close());
    dialog?.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  }

  function renderUnavailable() {
    document.getElementById("matchList").innerHTML = empty("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РґР°РЅРЅС‹Рµ. РЎС‚СЂР°РЅРёС†Р° РїРѕРІС‚РѕСЂРёС‚ РїРѕРїС‹С‚РєСѓ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё.");
    document.getElementById("expressGrid").innerHTML = empty("РћР¶РёРґР°РµРј СЃРѕРµРґРёРЅРµРЅРёРµ");
    document.getElementById("singleGrid").innerHTML = empty("РћР¶РёРґР°РµРј СЃРѕРµРґРёРЅРµРЅРёРµ");
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
  function home(row) { return String(row.homeRu || row.home || row.homeTeam?.name || row.homeTeam || "РҐРѕР·СЏРµРІР°"); }
  function away(row) { return String(row.awayRu || row.away || row.awayTeam?.name || row.awayTeam || "Р“РѕСЃС‚Рё"); }
  function teamsText(row) { return `${home(row)} вЂ” ${away(row)}`; }
  function league(row) { return String(row.leagueRu || row.league || row.competition || row.sportTitle || "Р¤СѓС‚Р±РѕР»"); }
  function odds(row) { return number(row.odds ?? row.bookmakerOdds ?? row.price ?? row.fairOdds); }
  function probability(row) { const raw = number(row.probabilityPercent ?? row.confidence ?? row.modelProbability ?? row.probability); return raw <= 1 && raw > 0 ? raw*100 : raw; }
  function pick(row) {
    if (row.pickRu || row.selectionLabelRu || row.marketLabelRu || row.pick) return String(row.pickRu || row.selectionLabelRu || row.marketLabelRu || row.pick);
    const code = String(row.market || row.marketCode || row.selectionCode || "").toUpperCase();
    const map = { HOME_WIN:`РџРѕР±РµРґР° ${home(row)}`, AWAY_WIN:`РџРѕР±РµРґР° ${away(row)}`, DRAW:"РќРёС‡СЊСЏ", HOME_OR_DRAW:`${home(row)} РЅРµ РїСЂРѕРёРіСЂР°РµС‚`, AWAY_OR_DRAW:`${away(row)} РЅРµ РїСЂРѕРёРіСЂР°РµС‚`, OVER_1_5:"РўРѕС‚Р°Р» Р±РѕР»СЊС€Рµ 1,5", OVER_2_5:"РўРѕС‚Р°Р» Р±РѕР»СЊС€Рµ 2,5", UNDER_2_5:"РўРѕС‚Р°Р» РјРµРЅСЊС€Рµ 2,5", UNDER_3_5:"РўРѕС‚Р°Р» РјРµРЅСЊС€Рµ 3,5", BOTH_TEAMS_SCORE:"РћР±Рµ Р·Р°Р±СЊСЋС‚", HOME_OVER_0_5:`${home(row)} Р·Р°Р±СЊС‘С‚`, AWAY_OVER_0_5:`${away(row)} Р·Р°Р±СЊС‘С‚` };
    return map[code] || code.replaceAll("_"," ") || "РџСЂРѕРіРЅРѕР· РјР°С‚С‡Р°";
  }
  function matchTime(row) { const value = row.commenceTime || row.utcDate || row.kickoff; if (!value) return "РІСЂРµРјСЏ СѓС‚РѕС‡РЅСЏРµС‚СЃСЏ"; try { return new Intl.DateTimeFormat("ru-RU",{timeZone:MOSCOW,day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); } catch { return "РІСЂРµРјСЏ СѓС‚РѕС‡РЅСЏРµС‚СЃСЏ"; } }
  function formatDateTime(value) { if (!value) return "вЂ”"; try { return new Intl.DateTimeFormat("ru-RU",{timeZone:MOSCOW,day:"numeric",month:"long",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); } catch { return "вЂ”"; } }
  function formatShortDateTime(value) { if (!value) return "вЂ”"; try { return new Intl.DateTimeFormat("ru-RU",{timeZone:MOSCOW,day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(value)); } catch { return "вЂ”"; } }
  function formatDate(value) { try { const d = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T12:00:00Z`) : new Date(value); return new Intl.DateTimeFormat("ru-RU",{day:"numeric",month:"long",year:"numeric"}).format(d); } catch { return value; } }
  function formatNumber(value,digits=0) { return new Intl.NumberFormat("ru-RU",{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(number(value)); }
  function currency(value) { return `${formatNumber(value,0)} в‚Ѕ`; }
  function signedCurrency(value) { const n=number(value); return `${n>0?"+":""}${formatNumber(n,0)} в‚Ѕ`; }
  function percent(value) { return `${formatNumber(value,1)}%`; }
  function signedPercent(value) { const n=number(value); return `${n>0?"+":""}${formatNumber(n,1)}%`; }
  function setText(id,value) { const node=document.getElementById(id); if(node) node.textContent=String(value); }
  function empty(text) { return `<div class="empty-card">${escapeHtml(text)}</div>`; }
  function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]); }
})();
