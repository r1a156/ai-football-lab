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
    state.bank = object(state.bank);
    state.expressBank = object(state.expressBank);
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
    renderPremium(current ? state.dailyAnalysis.filter(row => Boolean(row.premiumQualified)).slice(0, 3) : [], state);
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
    if (daily.length < 3 || daily.length > 15) return false;
    if (state.bestBets.length !== 3) return false;
    if (!state.bestBets.every(row => number(row.stakePercent) === 10 && number(row.stake) > 0)) return false;
    const expectedExpresses = Math.min(3, Math.floor(daily.length / 5));
    if (expresses.length !== expectedExpresses) return false;
    if (!expresses.every(ticket => array(ticket.legs).length === 5)) return false;
    const marker = String(state.meta.sourceMarker || "");
    if (!marker.includes("R15")) return false;
    if (!daily.every(row => odds(row) >= 1.55 && home(row) && away(row))) return false;
    const selectionEnd = Date.parse(state.meta.selectionWindowEnd || state.meta.operationalWindowEnd || "");
    const updated = Date.parse(state.meta.updatedAt || "");
    if (Number.isFinite(selectionEnd) && Number.isFinite(updated)) {
      return selectionEnd > Date.now() - 15 * 60_000 && Date.now() - updated < 30 * 60 * 60_000;
    }
    return Number.isFinite(updated) && Date.now() - updated < 30 * 60 * 60_000;
  }

  function renderMeta(state, current) {
    const quality = current ? average(state.dailyAnalysis.map(row => number(row.dataQuality))) : 0;
    const modelBank = bankSnapshot(state);
    setText("summaryDate", new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW, day: "numeric", month: "long" }).format(new Date()));
    setText("summaryMatches", current ? String(state.dailyAnalysis.length) : "—");
    setText("summaryExpresses", current ? String(state.expresses.length) : "—");
    setText("summarySingles", current ? String(Math.min(3, state.bestBets.length)) : "—");
    setText("summaryPremium", current ? String(state.dailyAnalysis.filter(row => Boolean(row.premiumQualified)).length) : "—");
    setText("summaryBank", currency(modelBank.current));
    setText("summaryExposure", `Доходность ${signedPercent(modelBank.roi)} · ${modelBank.count} закрытых ставок`);
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
    const rankedBet = array(state.bestBets).find(item =>
      String(item.sourceAnalysisId || "") === String(row.id || "") ||
      String(item.eventId || "") === String(row.eventId || "")
    );
    const rankedStake = number(rankedBet?.stake);
    const banked = rankedStake > 0;
    const inTopThree = Boolean(rankedBet);

    setText("spotlightTeams", teamsText(row));
    setText("spotlightPick", pick(row));
    setText("spotlightProbability", `${formatNumber(p,1)}%`);
    setText("spotlightQuality", `${formatNumber(q,0)}/100`);
    setText("spotlightStability", stability ? `${formatNumber(stability,0)}/100` : "—");
    setText("spotlightOdds", formatNumber(odds(row),2));
    setText("spotlightEv", signedPercent(ev * 100));
    setText("spotlightBankroll", banked ? `10% · ${currency(rankedStake)}` : (inTopThree ? "Без ставки" : "Не в Топ‑3"));
    setText("spotlightMode", banked ? "Топ‑3 · банк" : (inTopThree ? "Топ‑3 · контроль" : "Анализ"));
    setBar("spotlightProbabilityBar", p);
    setBar("spotlightQualityBar", q);
    setBar("spotlightStabilityBar", stability);

    const reasons = reasonItems(row).slice(0,3);
    const economicReason = banked
      ? "Прогноз входит в зафиксированный Топ‑3: ставка равна 10% банка на момент публикации."
      : inTopThree
        ? "Прогноз входит в Топ‑3, но эта публикация работает без финансового риска (предпросмотр или recovery-режим)."
        : "Банк используется только для трёх зафиксированных одиночных ставок Топ‑3; этот матч остаётся аналитическим.";
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
    setText("bankrollGuard", "Топ‑3 · 10% × 3");
    setText("guardExplanation", guard.policy
      ? "Калибровка может только снижать модельную уверенность или отклонять кандидата. Размер ставки не подгоняется под недавний результат: для нового Топ‑3 всегда 10% банка на каждую позицию."
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
      const analysis = object(object(runtime.report.diagnostics).analysis);
      const nearMisses = array(analysis.nearMissCandidates)
        .filter(row => number(row.bestOdds) >= 1.55)
        .slice(0, 6);
      if (!nearMisses.length) {
        root.innerHTML = empty("Свежие матчи ещё не опубликованы");
        return;
      }
      root.innerHTML = `<div class="smart-empty"><span>ЛУЧШИЕ МАТЧИ ПО МОДЕЛИ</span><strong>Строгих прогнозов пока нет — показываем лучшие аналитические матчи</strong><p>Эти матчи зафиксированы до начала и участвуют в shadow-learning. Они не считаются официальными ставками, пока не пройдут строгий фильтр.</p></div>` +
        nearMisses.map((row, index) => {
          const failures = [...array(row.hardFailures), ...array(row.marketFailures)].filter(Boolean).slice(0, 2);
          const probabilityValue = number(row.bestProbability) <= 1 ? number(row.bestProbability) * 100 : number(row.bestProbability);
          return `<article class="match-card">
            <div class="rank">${index + 1}</div>
            <div class="match-main">
              <div class="match-meta"><span>${escapeHtml(league(row))}</span><span>•</span><span>аналитический кандидат · обучение</span></div>
              <div class="teams"><span>${escapeHtml(home(row))}</span><i>—</i><span>${escapeHtml(away(row))}</span></div>
              <div class="match-why">${escapeHtml(failures.join("; ") || "Не прошёл полный контроль качества")}</div>
            </div>
            <div class="match-side">
              <div class="pick"><small>Лучший рынок модели</small><strong>${escapeHtml(row.bestCandidate || "—")}</strong><span class="pick-odds">× ${formatNumber(row.bestOdds,2)}</span></div>
              <div class="signal-cluster">
                ${signalHtml("Вероятность", probabilityValue, probabilityValue)}
                ${signalHtml("Качество", number(row.dataQuality), number(row.dataQuality))}
              </div>
            </div>
            <div class="chevron">×</div>
          </article>`;
        }).join("");
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
    const analysis = object(object(runtime.report.diagnostics).analysis);
    const officialIds = new Set(rows.map(row => String(row.eventId || "")));
    const learningCandidates = array(analysis.nearMissCandidates)
      .filter(row => number(row.bestOdds) >= 1.55 && !officialIds.has(String(row.eventId || "")))
      .slice(0, Math.max(0, 8 - rows.length));
    if (learningCandidates.length) {
      root.innerHTML += `<div class="smart-empty"><span>ЛУЧШИЕ МАТЧИ ПО МОДЕЛИ</span><strong>Дополнительные аналитические кандидаты</strong><p>Зафиксированы до начала матча и участвуют в обучении, но не входят в официальный портфель.</p></div>` +
        learningCandidates.map((row, index) => {
          const failures = [...array(row.hardFailures), ...array(row.marketFailures)].filter(Boolean).slice(0, 2);
          const probabilityValue = number(row.bestProbability) <= 1 ? number(row.bestProbability) * 100 : number(row.bestProbability);
          return `<article class="match-card">
            <div class="rank">${rows.length + index + 1}</div>
            <div class="match-main">
              <div class="match-meta"><span>${escapeHtml(league(row))}</span><span>•</span><span>аналитический кандидат · обучение</span></div>
              <div class="teams"><span>${escapeHtml(home(row))}</span><i>—</i><span>${escapeHtml(away(row))}</span></div>
              <div class="match-why">${escapeHtml(failures.join("; ") || "Близок к строгому допуску")}</div>
            </div>
            <div class="match-side">
              <div class="pick"><small>Лучший рынок модели</small><strong>${escapeHtml(row.bestCandidate || "—")}</strong><span class="pick-odds">× ${formatNumber(row.bestOdds,2)}</span></div>
              <div class="signal-cluster">
                ${signalHtml("Вероятность", probabilityValue, probabilityValue)}
                ${signalHtml("Качество", number(row.dataQuality), number(row.dataQuality))}
              </div>
            </div>
            <div class="chevron">○</div>
          </article>`;
        }).join("");
    }
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
        <div class="express-footer"><span>Банк <strong>не используется</strong></span><span>Вероятность <strong>${percent(probability(ticket))}</strong></span></div>
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
        ${row.premiumQualified ? '<div class="premium-mark">Премиум</div>' : ''}
        <div class="single-teams"><small>${escapeHtml(league(row))} · ${escapeHtml(matchTime(row))}</small><span>${escapeHtml(home(row))}</span><span>${escapeHtml(away(row))}</span></div>
        <div class="single-pick"><span>Прогноз</span><strong>${escapeHtml(pick(row))}</strong></div>
        <div class="single-metrics"><div><span>Вероятность</span><strong>${percent(probability(row))}</strong></div><div><span>Коэффициент</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>Ставка</span><strong>${number(row.stake) > 0 ? currency(row.stake) : "без риска"}</strong></div></div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-record]").forEach(node => node.addEventListener("click", () => openDetails(runtime.records.get(node.dataset.record))));
  }

  function renderPremium(rows, state) {
    const root = document.getElementById("premiumGrid");
    if (!root) return;
    if (!rows.length) {
      root.innerHTML = '<div class="smart-empty"><span>ПРЕМИУМ</span><strong>Сегодня премиум‑ставок нет</strong><p>Это нормальный результат: премиум появляется только при одновременном прохождении всех строгих фильтров. Обычный Топ‑3 при этом публикуется каждый день независимо от премиум‑статуса.</p></div>';
      return;
    }
    const topThreeIds = new Set(array(state.bestBets).map(row => String(row.eventId || "")));
    root.innerHTML = rows.map((row, index) => {
      const key = remember(row, `premium-${index}`);
      const covered = topThreeIds.has(String(row.eventId || ""));
      return `<article class="single-card premium-card" data-record="${escapeHtml(key)}" tabindex="0" role="button">
        <div class="premium-mark">Премиум</div>
        <div class="single-rank">${index + 1}</div>
        <div class="single-teams"><small>${escapeHtml(league(row))} · ${escapeHtml(matchTime(row))}</small><span>${escapeHtml(home(row))}</span><span>${escapeHtml(away(row))}</span></div>
        <div class="single-pick"><span>Прогноз</span><strong>${escapeHtml(pick(row))}</strong></div>
        <div class="single-metrics"><div><span>Вероятность</span><strong>${percent(probability(row))}</strong></div><div><span>Коэффициент</span><strong>${formatNumber(odds(row),2)}</strong></div><div><span>Банк</span><strong>${covered ? "учтён в Топ‑3" : "без дополнительной ставки"}</strong></div></div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-record]").forEach(node => node.addEventListener("click", () => openDetails(runtime.records.get(node.dataset.record))));
  }

  function bankSnapshot(state) {
    const bank = object(state.bank);
    const starting = number(bank.starting ?? 10000);
    const current = number(bank.current ?? starting);
    const placed = number(bank.placedAmount ?? bank.activeExposure);
    const available = number(bank.available ?? Math.max(0, current - placed));
    const profit = number(bank.profit ?? current - starting);
    const roi = bank.roi != null ? number(bank.roi) : (starting ? profit / starting * 100 : 0);
    const maxDrawdown = number(bank.maxDrawdown);
    const settled = array(state.history)
      .filter(row => row.recordType === "BEST_BET")
      .filter(row => number(row.stake) > 0)
      .filter(row => ["won","lost","push"].includes(String(row.status || "").toLowerCase()));
    return { bank, starting, current, placed, available, profit, roi, maxDrawdown, count: settled.length };
  }

  function renderBank(state) {
    const snapshot = bankSnapshot(state);
    setText("bankCurrent", currency(snapshot.current));
    setText("bankStarting", currency(snapshot.starting));
    setText("bankPlaced", currency(snapshot.placed));
    setText("bankAvailable", currency(snapshot.available));
    setText("bankProfit", signedCurrency(snapshot.profit));
    setText("bankRoi", signedPercent(snapshot.roi));
    setText("bankDrawdown", `${formatNumber(snapshot.maxDrawdown,1)}%`);
    setText("bankChange", snapshot.profit === 0
      ? "без изменений"
      : `${snapshot.profit > 0 ? "+" : ""}${formatNumber(snapshot.starting ? snapshot.profit / snapshot.starting * 100 : 0, 1)}% от старта`);
    renderBankChart(snapshot);
  }

  function renderBankChart(snapshot) {
    const svg = document.getElementById("bankChart");
    if (!svg) return;
    const raw = array(snapshot.bank.history)
      .filter(row => Number.isFinite(Number(row?.value)))
      .map(row => ({
        value: Number(row.value),
        time: Date.parse(row.timestamp || row.date || "") || 0,
      }));
    const points = raw.length ? raw : [{ value: snapshot.current, time: 0 }];
    setText("bankHistoryCount", `${points.length} ${points.length === 1 ? "точка" : points.length < 5 ? "точки" : "точек"}`);
    setText("bankChartStart", currency(points[0].value));
    setText("bankChartEnd", currency(points[points.length - 1].value));

    const width = 640, height = 180, padX = 10, padY = 18;
    const values = points.map(point => point.value);
    let min = Math.min(...values), max = Math.max(...values);
    if (Math.abs(max - min) < 0.01) {
      const spread = Math.max(1, Math.abs(max) * 0.01);
      min -= spread;
      max += spread;
    }
    const x = index => points.length === 1 ? width / 2 : padX + index * (width - padX * 2) / (points.length - 1);
    const y = value => padY + (max - value) * (height - padY * 2) / (max - min);
    const coords = points.map((point, index) => `${x(index).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
    const startY = y(snapshot.starting).toFixed(2);
    svg.innerHTML = `
      <line class="bank-chart-baseline" x1="${padX}" y1="${startY}" x2="${width-padX}" y2="${startY}"></line>
      <polyline class="bank-chart-line" points="${coords}"></polyline>
      <circle class="bank-chart-point" cx="${x(points.length-1).toFixed(2)}" cy="${y(points[points.length-1].value).toFixed(2)}" r="4"></circle>
    `;
  }

  function renderHistory(state) {
    const rows = array(state.history)
      .filter(row => row.recordType === "BEST_BET")
      .filter(row => number(row.stake) > 0)
      .filter(row => ["won","lost","push","void","cancelled"].includes(String(row.status || "").toLowerCase()))
      .sort((a,b) => Date.parse(b.settledAt || b.commenceTime || b.utcDate || 0) - Date.parse(a.settledAt || a.commenceTime || a.utcDate || 0));
    setText("historyCount", String(rows.length));
    const root = document.getElementById("historyList");
    if (!rows.length) { root.innerHTML = '<div class="empty-mini">Завершённых банковских ставок пока нет</div>'; return; }
    root.innerHTML = rows.slice(0,8).map(row => {
      const status = String(row.status || "").toLowerCase();
      const label = status === "won" ? "Выигрыш" : status === "lost" ? "Проигрыш" : status === "push" ? "Возврат" : "Закрыто";
      const title = teamsText(row);
      const sub = `${pick(row)} · ставка ${currency(row.stake)} · ×${formatNumber(odds(row),2)}`;
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
    if (document.getElementById("premiumGrid")) document.getElementById("premiumGrid").innerHTML = empty("Ожидаем соединение");
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
      .replace("FOOTBALL_DATA_CO_UK","База футбольных результатов")
      .replace("OPENFOOTBALL","Открытый архив матчей")
      .replace("OPENLIGADB","Открытая база лиг")
      .replace("STATSBOMB_OPEN_DATA","Открытая статистика матчей")
      .replace("CLUBELO","Рейтинг силы клубов");
  }

  function healthLabel(value) {
    const text = String(value || "UNKNOWN").toUpperCase();
    return ({GREEN:"Норма",PARTIAL:"Частично",DEGRADED:"Ограничено",RED:"Ошибка",ERROR:"Ошибка",LIMIT:"Лимит",UNKNOWN:"Нет данных"})[text] || text;
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
