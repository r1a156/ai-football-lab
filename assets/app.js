/* V10_R15F_R3R6_PRODUCTION_REDESIGN */
(() => {
  "use strict";
  const API_BASE = String(globalThis.FOOTBALL_API_BASE || "").replace(/\/+$/, "");
  const LOCAL_STATE_URL = "data/state.json";
  const LOCAL_LIVE_URL = "data/live-state.json";
  const LOCAL_REPORT_URL = "data/last-update-report.json";
  const RAW_STATE_URL = "https://raw.githubusercontent.com/r1a156/ai-football-lab/main/data/state.json";
  const RAW_LIVE_URL = "https://raw.githubusercontent.com/r1a156/ai-football-lab/main/data/live-state.json";
  const RAW_REPORT_URL = "https://raw.githubusercontent.com/r1a156/ai-football-lab/main/data/last-update-report.json";
  const STATE_URL = API_BASE ? `${API_BASE}/data/state.json` : LOCAL_STATE_URL;
  const LIVE_URL = API_BASE ? `${API_BASE}/data/live-state.json` : LOCAL_LIVE_URL;
  const REPORT_URL = API_BASE ? `${API_BASE}/data/last-update-report.json` : LOCAL_REPORT_URL;
  const MIN_QUALITY = 58;
  const MOSCOW = "Europe/Moscow";
  const runtime = { state: null, live: null, report: {}, records: new Map(), installPrompt: null };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    bindDialog();
    setupPwa();
    await refresh();
    window.setInterval(refresh, 60_000);
  }

  async function refresh() {
    setConnection("loading", "Обновление");
    try {
      const stamp = Date.now();
      const [state, live, report] = await Promise.all([
        loadBestState(stamp),
        loadLiveState(stamp),
        loadReport(stamp),
      ]);
      runtime.state = state;
      runtime.live = live;
      runtime.report = report;
      render(runtime.state);
      setConnection("ready", "Актуально");
    } catch (error) {
      console.error(error);
      setConnection("error", "Нет связи");
      renderUnavailable();
    }
  }

  async function fetchJson(url, stamp) {
    const separator = url.includes("?") ? "&" : "?";
    const response = await fetch(`${url}${separator}v=${stamp}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`${url} ${response.status}`);
    return response.json();
  }

  async function loadBestState(stamp) {
    const urls = API_BASE
      ? [LOCAL_STATE_URL, RAW_STATE_URL, STATE_URL]
      : [LOCAL_STATE_URL, RAW_STATE_URL];
    const candidates = [];
    let lastError = null;
    for (const url of urls) {
      try {
        candidates.push(normalize(await fetchJson(url, stamp)));
      } catch (error) {
        lastError = error;
      }
    }
    if (!candidates.length) throw lastError || new Error("state unavailable");
    candidates.sort((a, b) => {
      const currentDelta = Number(isCurrentPortfolio(b)) - Number(isCurrentPortfolio(a));
      if (currentDelta) return currentDelta;
      const bUpdated = Date.parse(b.meta.updatedAt || "") || 0;
      const aUpdated = Date.parse(a.meta.updatedAt || "") || 0;
      return bUpdated - aUpdated;
    });
    return candidates[0];
  }

  async function loadLiveState(stamp) {
    const urls = API_BASE
      ? [LOCAL_LIVE_URL, RAW_LIVE_URL, LIVE_URL]
      : [LOCAL_LIVE_URL, RAW_LIVE_URL];
    for (const url of urls) {
      try {
        return await fetchJson(url, stamp);
      } catch {}
    }
    return {};
  }

  async function loadReport(stamp) {
    const urls = API_BASE
      ? [LOCAL_REPORT_URL, RAW_REPORT_URL, REPORT_URL]
      : [LOCAL_REPORT_URL, RAW_REPORT_URL];
    for (const url of urls) {
      try {
        const value = await fetchJson(url, stamp);
        return value && typeof value === "object" ? value : {};
      } catch {}
    }
    return {};
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
    renderSpotlight(state, runtime.report, current);
    renderDecisionSummary(state, runtime.report, current);
    renderHealth(state, runtime.report);
    renderModelStats(state);
    renderMatches(current ? state.dailyAnalysis : []);
    renderExpresses(current ? state.expresses : [], state, current);
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
    if (daily.length < 1 || daily.length > 15) return false;
    const expectedExpresses = Math.min(3, Math.floor(daily.length / 5));
    if (expresses.length !== expectedExpresses) return false;
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
    const modelBank = modelBankSnapshot(state);
    setText("summaryDate", new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW, day: "numeric", month: "long" }).format(new Date()));
    setText("summaryMatches", current ? String(state.dailyAnalysis.length) : "—");
    setText("summaryExpresses", current ? String(state.expresses.length) : "—");
    setText("summarySingles", current ? String(Math.min(3, state.bestBets.length)) : "—");
    setText("summaryQuality", current ? `${formatNumber(quality, 0)}/100` : "—");
    setText("summaryBank", currency(modelBank.current));
    setText("summaryExposure", `ROI ${signedPercent(modelBank.roi * 100)} · ${modelBank.count} расчётов`);
    const preview = Boolean(state.meta.bootstrapPreview) && !Boolean(state.meta.recoveryDay);
    setText("portfolioStatus", current
      ? (preview ? "Предпросмотр текущей подборки" : "Свежая подборка опубликована")
      : "Новая подборка формируется");
    setText("portfolioUpdated", state.meta.updatedAt ? `Обновлено ${formatDateTime(state.meta.updatedAt)}` : "Ожидаем обновление");
    setText("matchesUpdated", current && state.meta.updatedAt ? formatShortDateTime(state.meta.updatedAt) : "ожидание");
    setText("footerUpdated", state.meta.updatedAt ? `Данные: ${formatDateTime(state.meta.updatedAt)}` : "Данные загружаются");
    document.getElementById("heroStatus")?.classList.toggle("is-ready", current);
  }

  function renderSpotlight(state, report, current) {
    const row = current ? state.dailyAnalysis[0] : null;
    if (!row) {
      setText("spotlightTeams", "Новая подборка формируется");
      setText("spotlightPick", "—");
      setText("spotlightProbability", "—");
      setText("spotlightQuality", "—");
      setText("spotlightStability", "—");
      setText("spotlightOdds", "—");
      setText("spotlightEv", "—");
      setText("spotlightBankroll", "Отключён");
      setText("spotlightMode", "Ожидание");
      setHtml("spotlightReasons", "<li>Система не публикует слабые события ради заполнения списка.</li>");
      setBar("spotlightProbabilityBar", 0);
      setBar("spotlightQualityBar", 0);
      setBar("spotlightStabilityBar", 0);
      return;
    }

    const p = probability(row);
    const q = number(row.dataQuality);
    const stability = number(row.marketStability);
    const ev = conservativeEv(row);
    const guard = object(state.meta.calibrationGuard);
    const bankrollAllowed = Boolean(guard.bankrollAllowed) && ev >= 0.03;
    const informational = !bankrollAllowed;

    setText("spotlightTeams", teamsText(row));
    setText("spotlightPick", pick(row));
    setText("spotlightProbability", `${formatNumber(p,1)}%`);
    setText("spotlightQuality", `${formatNumber(q,0)}/100`);
    setText("spotlightStability", stability ? `${formatNumber(stability,0)}/100` : "—");
    setText("spotlightOdds", formatNumber(odds(row),2));
    setText("spotlightEv", signedPercent(ev * 100));
    setText("spotlightBankroll", informational ? "Пауза" : "Разрешён");
    setText("spotlightMode", informational ? "Информационный" : "Допущен");
    setBar("spotlightProbabilityBar", p);
    setBar("spotlightQualityBar", q);
    setBar("spotlightStabilityBar", stability);

    const reasons = reasonItems(row).slice(0,3);
    const economicReason = informational
      ? (ev < 0
          ? "Экспресс‑риск поставлен на паузу: консервативное EV отрицательное. Аналитический банк продолжает считать результат одиночных прогнозов."
          : "Экспресс‑риск поставлен на паузу calibration guard. Аналитический банк продолжает считать прибыльность модели.")
      : "Экспресс‑риск разрешён: положительное консервативное EV прошло финансовый фильтр.";
    setHtml("spotlightReasons", [...reasons, economicReason].map(item => `<li>${escapeHtml(item)}</li>`).join(""));
  }

  function renderDecisionSummary(state, report, current) {
    const diagnostics = object(report.diagnostics);
    const analysis = object(diagnostics.analysis);
    const discovery = object(diagnostics.discovery);
    const rejectionReasons = object(analysis.rejectionReasons);
    const checked = number(state.meta.candidateMatchesAnalyzed || discovery.events || analysis.oddsEvents);
    const qualified = number(analysis.eventsQualified || (current ? state.dailyAnalysis.length : 0));
    const published = current ? state.dailyAnalysis.length : 0;
    const rejected = Math.max(0, checked - qualified);
    const top = Object.entries(rejectionReasons).sort((a,b) => number(b[1]) - number(a[1]))[0];

    setText("checkedCount", checked || "—");
    setText("qualifiedCount", qualified || "0");
    setText("publishedCount", published || "0");
    setText("topRejectionReason", top ? top[0] : "Нет достаточных данных");
    setText("rejectedCount", checked ? `${rejected} матчей не прошли основной фильтр` : "Ожидаем цикл анализа");
  }

  function renderHealth(state, report) {
    const diagnostics = object(report.diagnostics);
    const mesh = object(diagnostics.freeDataMesh);
    const sources = object(mesh.sources);
    const sportsDb = object(diagnostics.theSportsDbCurrentHistory);
    const quota = object(diagnostics.quotaPlan);
    const apiHealth = object(state.meta.apiHealth);
    const items = Object.entries(sources).map(([name,status]) => [sourceLabel(name), String(status || "UNKNOWN")]);
    if (sportsDb.status) items.push(["TheSportsDB", String(sportsDb.status)]);
    items.push(["Odds/API", apiHealth.status || (quota.quotaExhaustedAfterAcquisition ? "LIMIT" : "GREEN")]);

    const root = document.getElementById("healthGrid");
    if (root) {
      root.innerHTML = items.slice(0,6).map(([name,status]) =>
        `<div class="health-line"><span>${escapeHtml(name)}</span><strong class="${healthClass(status)}">${escapeHtml(healthLabel(status))}</strong></div>`
      ).join("");
    }
    const statuses = items.map(item => item[1].toUpperCase());
    const overall = statuses.some(x => x === "RED" || x === "ERROR") ? "DEGRADED"
      : statuses.some(x => x === "PARTIAL" || x === "LIMIT" || x === "DEGRADED") ? "PARTIAL" : "GREEN";
    setText("healthOverall", healthLabel(overall));
    setText("healthUpdated", state.meta.updatedAt ? `Обновлено ${formatShortDateTime(state.meta.updatedAt)}` : "—");
    const remaining = number(quota.quotaRemainingBeforeOdds);
    setText("quotaStatus", remaining ? `Квота: ${formatNumber(remaining,0)}` : "Квота контролируется");
  }

  function renderModelStats(state) {
    const rows = array(state.analysisHistory).filter(row => ["won","lost"].includes(String(row.status || "").toLowerCase()));
    const seven = statsForDays(rows, 7);
    const thirty = statsForDays(rows, 30);
    renderStatsWindow("7", seven);
    renderStatsWindow("30", thirty);

    const guard = object(state.meta.calibrationGuard);
    const total = object(object(guard.marketFamilyHaircuts));
    setText("calibrationMode", guard.mode || "AUTO GUARD");
    setText("guardStatus", guard.mode || "LEARNING");
    setText("guardMargin", guard.additionalUncertaintyMargin != null ? `+${formatNumber(number(guard.additionalUncertaintyMargin)*100,1)} п.п.` : "—");
    setText("totalHaircut", total.TOTAL != null ? `−${formatNumber(number(total.TOTAL)*100,1)} п.п.` : "0 п.п.");
    setText("bankrollGuard", guard.bankrollAllowed === false ? "Пауза risk‑guard" : "По EV‑фильтру");
    setText("guardExplanation", guard.policy
      ? "Автоконтур ограничивает только новые рискованные экспрессы. Аналитический банк одиночных прогнозов продолжает считать ROI, прибыль и просадку ежедневно."
      : "Система сравнивает фактический результат с заявленной вероятностью и не подгоняет модель под один день.");
  }

  function renderStatsWindow(prefix, stats) {
    setText(`stat${prefix}Count`, `${stats.n} событий`);
    setText(`stat${prefix}Hit`, stats.n ? `${formatNumber(stats.hitRate*100,1)}%` : "—");
    setText(`stat${prefix}Predicted`, stats.n ? `${formatNumber(stats.avgPredicted*100,1)}%` : "—");
    setText(`stat${prefix}Brier`, stats.n ? formatNumber(stats.brier,3) : "—");
  }

  function statsForDays(rows, days) {
    const cutoff = Date.now() - days * 86400_000;
    const windowRows = rows.filter(row => {
      const time = Date.parse(row.settledAt || row.commenceTime || "");
      return Number.isFinite(time) && time >= cutoff;
    });
    if (!windowRows.length) return { n:0, hitRate:0, avgPredicted:0, brier:0 };
    let wins = 0, pSum = 0, brier = 0;
    for (const row of windowRows) {
      const y = String(row.status).toLowerCase() === "won" ? 1 : 0;
      const p = probabilityFraction(row);
      wins += y;
      pSum += p;
      brier += (p-y)*(p-y);
    }
    return {
      n: windowRows.length,
      hitRate: wins/windowRows.length,
      avgPredicted: pSum/windowRows.length,
      brier: brier/windowRows.length,
    };
  }

  function renderMatches(rows) {
    const root = document.getElementById("matchList");
    if (!rows.length) {
      root.innerHTML = empty("Свежие матчи ещё не опубликованы");
      return;
    }
    root.innerHTML = rows.map((row, index) => {
      const key = remember(row, `match-${index}`);
      const prob = probability(row);
      const quality = number(row.dataQuality);
      const stability = number(row.marketStability);
      return `<article class="match-card" data-record="${escapeHtml(key)}" tabindex="0" role="button" aria-label="Открыть прогноз ${escapeHtml(teamsText(row))}">
        <div class="rank ${index < 3 ? "top" : ""}">${index + 1}</div>
        <div class="match-main">
          <div class="match-meta"><span>${escapeHtml(league(row))}</span><span>•</span><time>${escapeHtml(matchTime(row))}</time></div>
          <div class="teams"><span>${escapeHtml(home(row))}</span><i>—</i><span>${escapeHtml(away(row))}</span></div>
          <div class="match-why">${escapeHtml(reasonSummary(row))}</div>
        </div>
        <div class="match-side">
          <div class="pick"><small>Прогноз</small><strong>${escapeHtml(pick(row))}</strong><span class="pick-odds">× ${formatNumber(odds(row),2)}</span></div>
          <div class="signal-cluster">
            ${signalHtml("Вероятность", prob, prob)}
            ${signalHtml("Качество", quality, quality)}
            ${signalHtml("Стабильность", stability, stability)}
          </div>
        </div>
        <div class="chevron">›</div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-record]").forEach(node => {
      node.addEventListener("click", () => openDetails(runtime.records.get(node.dataset.record)));
      node.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") openDetails(runtime.records.get(node.dataset.record)); });
    });
  }

  function renderExpresses(rows, state, current) {
    const root = document.getElementById("expressGrid");
    if (!rows.length) {
      const dailyCount = current ? array(state.dailyAnalysis).length : 0;
      const message = dailyCount > 0
        ? `Экспресс не сформирован: для безопасного купона нужно минимум 5 прошедших фильтр событий. Сегодня опубликовано ${dailyCount}.`
        : "Экспрессы появятся только после публикации достаточного числа качественных событий.";
      root.innerHTML = `<div class="smart-empty"><span>РИСК-КОНТРОЛЬ</span><strong>Экспресс пропущен системой</strong><p>${escapeHtml(message)}</p></div>`;
      return;
    }
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

  function modelBankSnapshot(state) {
    const starting = 10000;
    const stake = 100;
    const rows = array(state.analysisHistory)
      .filter(row => ["won","lost","push"].includes(String(row.status || "").toLowerCase()))
      .filter(row => odds(row) > 1)
      .sort((a,b) => Date.parse(a.settledAt || a.commenceTime || 0) - Date.parse(b.settledAt || b.commenceTime || 0));

    let current = starting;
    let totalStaked = 0;
    let peak = starting;
    let maxDrawdown = 0;

    for (const row of rows) {
      const status = String(row.status || "").toLowerCase();
      const price = odds(row);
      totalStaked += stake;
      if (status === "won") current += stake * (price - 1);
      else if (status === "lost") current -= stake;
      peak = Math.max(peak, current);
      if (peak > 0) maxDrawdown = Math.max(maxDrawdown, (peak - current) / peak);
    }

    const profit = current - starting;
    return {
      starting,
      current,
      profit,
      totalStaked,
      roi: totalStaked > 0 ? profit / totalStaked : 0,
      maxDrawdown,
      count: rows.length,
    };
  }

  function renderModelBank(state) {
    const model = modelBankSnapshot(state);
    setText("modelBankCurrent", currency(model.current));
    setText("modelBankStarting", currency(model.starting));
    setText("modelBankBets", String(model.count));
    setText("modelBankRoi", signedPercent(model.roi * 100));
    setText("modelBankDrawdown", `${formatNumber(model.maxDrawdown * 100,1)}%`);
    setText("modelBankChange", `${signedCurrency(model.profit)} · ${model.count} рассчитанных прогнозов`);
  }

  function renderBank(state) {
    renderModelBank(state);
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
      const sub = row.legs
        ? `${array(row.legs).length} событий`
        : `${pick(row)} · прогноз ${formatNumber(probability(row),1)}%`;
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
      <div class="dialog-grid"><div><span>Вероятность</span><strong>${percent(probability(row))}</strong></div><div><span>Коэффициент</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>Качество данных</span><strong>${formatNumber(row.dataQuality,0)}/100</strong></div><div><span>Стабильность линии</span><strong>${formatNumber(row.marketStability,0)}/100</strong></div><div><span>Согласованность</span><strong>${formatNumber(row.agreement,0)}/100</strong></div><div><span>Консервативный EV</span><strong>${signedPercent(conservativeEv(row)*100)}</strong></div></div>
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

  function setupPwa() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("./sw.js").catch(() => {});
    }
    const button = document.getElementById("installApp");
    window.addEventListener("beforeinstallprompt", event => {
      event.preventDefault();
      runtime.installPrompt = event;
      if (button) button.hidden = false;
    });
    button?.addEventListener("click", async () => {
      if (!runtime.installPrompt) return;
      runtime.installPrompt.prompt();
      try { await runtime.installPrompt.userChoice; } catch {}
      runtime.installPrompt = null;
      button.hidden = true;
    });
    window.addEventListener("appinstalled", () => {
      runtime.installPrompt = null;
      if (button) button.hidden = true;
    });
  }

  function reasonItems(row) {
    const rationale = object(row.selectionRationale);
    const explicit = array(rationale.reasons).filter(Boolean).map(String);
    if (explicit.length) return explicit;
    const notes = array(row.sourceNotes).filter(Boolean).map(String);
    if (notes.length) return notes;
    if (row.reasonRu || row.reason) return [String(row.reasonRu || row.reason)];
    return [
      `Консервативная вероятность ${formatNumber(probability(row),1)}%`,
      `Качество данных ${formatNumber(row.dataQuality,0)}/100`,
      `Подтверждение ${formatNumber(row.quoteCount,0)} букмекерами`,
    ];
  }

  function reasonSummary(row) {
    const items = reasonItems(row);
    return items[0] || "Прогноз прошёл текущий фильтр качества.";
  }

  function conservativeEv(row) {
    const p = probabilityFraction(row);
    const price = odds(row);
    return price > 0 ? p * price - 1 : 0;
  }

  function probabilityFraction(row) {
    const raw = number(row.conservativeProbability ?? row.confidence ?? row.probabilityPercent ?? row.probability ?? row.modelProbability);
    return raw > 1 ? raw / 100 : raw;
  }

  function signalHtml(label, value, width) {
    const safeWidth = Math.max(0, Math.min(100, number(width)));
    return `<div class="signal"><span>${escapeHtml(label)}</span><b>${formatNumber(value, value % 1 ? 1 : 0)}${label === "Вероятность" ? "%" : "/100"}</b><i><em style="width:${safeWidth}%"></em></i></div>`;
  }

  function setBar(id, value) {
    const node = document.getElementById(id);
    if (node) node.style.width = `${Math.max(0,Math.min(100,number(value)))}%`;
  }

  function setHtml(id, value) {
    const node = document.getElementById(id);
    if (node) node.innerHTML = String(value);
  }

  function sourceLabel(value) {
    return String(value || "")
      .replace("FOOTBALL_DATA_CO_UK","Football-data")
      .replace("OPENFOOTBALL","OpenFootball")
      .replace("OPENLIGADB","OpenLigaDB")
      .replace("STATSBOMB_OPEN_DATA","StatsBomb")
      .replace("CLUBELO","ClubElo");
  }

  function healthLabel(value) {
    const text = String(value || "UNKNOWN").toUpperCase();
    return ({GREEN:"OK",PARTIAL:"Частично",DEGRADED:"Ограничено",RED:"Ошибка",ERROR:"Ошибка",LIMIT:"Лимит",UNKNOWN:"Нет данных"})[text] || text;
  }

  function healthClass(value) {
    const text = String(value || "").toUpperCase();
    if (text === "GREEN") return "health-ok";
    if (text === "RED" || text === "ERROR") return "health-bad";
    return "health-warn";
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
  function probability(row) { const raw = number(row.conservativeProbability ?? row.confidence ?? row.probabilityPercent ?? row.modelProbability ?? row.probability); return raw <= 1 && raw > 0 ? raw*100 : raw; }
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
