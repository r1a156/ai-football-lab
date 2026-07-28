/* V10_SITE_PREMIUM_DASHBOARD */
(() => {
    "use strict";

    const STATE_URL = "data/state.json";
    const REFRESH_INTERVAL_MS = 60_000;
    const MOSCOW_TIME_ZONE = "Europe/Moscow";

    const runtime = {
        state: null,
        signature: "",
        sportFilter: "all",
        historyFilter: "all",
        refreshTimer: null,
        freshnessTimer: null,
        chartFrame: null,
    };

    document.addEventListener("DOMContentLoaded", initialize);

    async function initialize() {
        initializeRevealObserver();
        initializeFilters();
        initializeDialog();
        window.addEventListener("resize", debounce(() => renderBankChart(runtime.state?.bank), 120));
        window.addEventListener("online", () => loadState({ notify: true }));
        window.addEventListener("offline", () => setConnectionState("offline"));

        await loadState({ notify: false });
        runtime.refreshTimer = window.setInterval(() => loadState({ notify: false }), REFRESH_INTERVAL_MS);
        runtime.freshnessTimer = window.setInterval(updateFreshnessLabels, 30_000);
    }

    async function loadState({ notify }) {
        setConnectionState("loading", notify ? "Проверяем обновление" : "");
        try {
            const response = await fetch(`${STATE_URL}?v=${Date.now()}`, {
                cache: "no-store",
                headers: { Accept: "application/json" },
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const state = await response.json();
            const signature = createStateSignature(state);
            const changed = signature !== runtime.signature;
            runtime.state = normalizeState(state);
            runtime.signature = signature;
            renderApplication(runtime.state);
            setConnectionState("ready", notify || changed ? "Данные актуализированы" : "");
        } catch (error) {
            console.error("State loading failed", error);
            setConnectionState(navigator.onLine ? "error" : "offline", "Сохранены последние доступные данные");
            if (!runtime.state) {
                renderLoadError();
            }
        }
    }

    function normalizeState(state) {
        const normalized = state && typeof state === "object" ? state : {};
        normalized.meta = normalized.meta && typeof normalized.meta === "object" ? normalized.meta : {};
        normalized.bank = normalized.bank && typeof normalized.bank === "object" ? normalized.bank : {};
        normalized.statistics = normalized.statistics && typeof normalized.statistics === "object" ? normalized.statistics : {};
        normalized.learning = normalized.learning && typeof normalized.learning === "object" ? normalized.learning : {};
        normalized.dailyAnalysis = Array.isArray(normalized.dailyAnalysis) ? normalized.dailyAnalysis : [];
        normalized.bestBets = Array.isArray(normalized.bestBets)
            ? normalized.bestBets
            : Array.isArray(normalized.predictions)
              ? normalized.predictions
              : [];
        normalized.history = Array.isArray(normalized.history) ? normalized.history : [];
        return normalized;
    }

    function createStateSignature(state) {
        const meta = state?.meta || {};
        const bank = state?.bank || {};
        return [
            meta.updatedAt || "",
            meta.version || "",
            state?.dailyAnalysis?.length || 0,
            state?.bestBets?.length || state?.predictions?.length || 0,
            bank.current || 0,
            state?.history?.length || 0,
        ].join("|");
    }

    function renderApplication(state) {
        renderMeta(state);
        renderBestBets(state.bestBets, state.meta, state.bank);
        renderDailyAnalysis(state.dailyAnalysis);
        renderBank(state.bank, state.statistics);
        renderLearning(state.learning, state.statistics);
        renderHistory(state.history);
        updateFreshnessLabels();
    }

    function renderMeta(state) {
        const { meta = {}, bank = {}, statistics = {}, dailyAnalysis = [], bestBets = [] } = state;
        const soccerCount = Number(meta.soccerAnalyses ?? dailyAnalysis.filter((item) => item.sport === "soccer").length);
        const hockeyCount = Number(meta.hockeyAnalyses ?? dailyAnalysis.filter((item) => item.sport === "ice_hockey").length);
        const leagueCount = Number(meta.leaguesAnalyzed ?? new Set(dailyAnalysis.map((item) => item.league).filter(Boolean)).size);

        setText("heroAnalysisCount", dailyAnalysis.length || meta.analysisPublished || 0);
        setText("heroBestCount", bestBets.length);
        setText("heroLeagueCount", leagueCount);
        setText("heroBank", formatCurrency(bank.current));
        setText("heroUpdated", formatCompactDateTime(meta.updatedAt));
        setText("stripSoccer", soccerCount);
        setText("stripHockey", hockeyCount);
        setText("stripAccuracy", formatPercent(statistics.bestBetsAccuracy));
        setText("stripRoi", formatSignedPercent(bank.roi));
        setText("footerUpdated", `Обновлено ${formatDateTime(meta.updatedAt)}`);

        const status = String(meta.status || "DEGRADED").toUpperCase();
        const statusNode = document.getElementById("topbarStatus");
        statusNode?.classList.toggle("is-green", status === "GREEN");
        statusNode?.classList.toggle("is-red", status === "RED");
        setText("systemStatus", status === "GREEN" ? "Данные актуальны" : status === "RED" ? "Ошибка обновления" : "Ограниченные данные");
    }

    function renderBestBets(bestBets, meta, bank) {
        const grid = document.getElementById("bestBetsGrid");
        if (!grid) return;

        setText("bestBetsUpdated", `Сформировано ${formatDateTime(meta?.analysisGeneratedAt || meta?.updatedAt)}`);
        const exposure = bestBets
            .filter((item) => String(item.status || "pending") === "pending")
            .reduce((sum, item) => sum + number(item.stake), 0);
        const bankValue = Math.max(1, number(bank?.current));
        setText("bestBetsExposure", `Экспозиция ${formatCurrency(exposure)} · ${formatNumber((exposure / bankValue) * 100, 0)}%`);

        if (!bestBets.length) {
            grid.innerHTML = `
                <div class="empty-state">
                    <strong>Сегодня нет ставок, прошедших все фильтры</strong>
                    <p>Система не заполняет четыре места искусственно. Аналитическая выборка остаётся доступной ниже, а виртуальный банк не подвергается необоснованному риску.</p>
                </div>`;
            return;
        }

        grid.innerHTML = bestBets.map((bet, index) => bestBetTemplate(bet, index)).join("");
        grid.querySelectorAll("[data-analysis-id]").forEach((node) => {
            node.addEventListener("click", () => openAnalysisDialog(findRecord(node.dataset.analysisId)));
        });
    }

    function bestBetTemplate(bet, index) {
        const probability = number(bet.modelProbability || bet.probability) * 100;
        const edge = number(bet.edge) * 100;
        const ev = number(bet.expectedValue) * 100;
        const dataQuality = number(bet.dataQuality);
        const status = String(bet.status || "pending");
        const rankLabel = bet.rankLabel || (index === 0 ? "Лучшая ставка" : `Ставка №${index + 1}`);
        return `
            <article class="best-bet-card" data-sport="${escapeHtml(bet.sport || "soccer")}" data-analysis-id="${escapeHtml(bet.id)}" tabindex="0" role="button">
                <div class="bet-card-top">
                    <span class="bet-rank">${escapeHtml(rankLabel)}</span>
                    <span class="sport-chip">${escapeHtml(bet.sportLabel || sportName(bet.sport))}</span>
                </div>
                <div class="bet-card-match">
                    <small>${escapeHtml([bet.country, bet.league].filter(Boolean).join(" · "))}</small>
                    <h3>${escapeHtml(bet.home)} — ${escapeHtml(bet.away)}</h3>
                    <time>${formatMatchTime(bet.commenceTime || bet.utcDate)} · ${escapeHtml(runtimeStatus(bet))}</time>
                </div>
                <div class="bet-selection">
                    <div>
                        <span>Выбранный рынок</span>
                        <strong>${escapeHtml(bet.pick || "—")}</strong>
                    </div>
                    <div class="bet-odds">
                        <span>Коэффициент</span>
                        <strong>${formatNumber(bet.bookmakerOdds || bet.odds, 2)}</strong>
                    </div>
                </div>
                <div class="bet-metrics">
                    <div class="metric-block"><span>Вероятность</span><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="metric-block"><span>Преимущество</span><strong class="${edge >= 0 ? "is-positive" : ""}">${formatSignedNumber(edge, 1)} п.п.</strong></div>
                    <div class="metric-block"><span>EV</span><strong class="${ev >= 0 ? "is-positive" : ""}">${formatSignedNumber(ev, 1)}%</strong></div>
                    <div class="metric-block"><span>Данные</span><strong>${formatNumber(dataQuality, 0)}/100</strong></div>
                </div>
                <div class="bet-card-footer">
                    <span>Виртуальная ставка</span>
                    <strong>${formatCurrency(bet.stake)} · ${formatNumber(bet.stakePercent, 0)}%</strong>
                    <span class="status-chip ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span>
                </div>
            </article>`;
    }

    function renderDailyAnalysis(records) {
        const list = document.getElementById("analysisList");
        if (!list) return;
        const filtered = records.filter((item) => runtime.sportFilter === "all" || item.sport === runtime.sportFilter);

        setText("analysisCount", records.length);
        setText("countryCount", new Set(records.map((item) => item.country).filter(Boolean)).size);
        setText("marketFamilyCount", new Set(records.map((item) => item.marketFamily).filter(Boolean)).size);
        setText("averageDataQuality", records.length ? `${formatNumber(average(records.map((item) => number(item.dataQuality))), 0)}/100` : "—");

        if (!filtered.length) {
            list.innerHTML = `<div class="analysis-loading">Для выбранного вида спорта прогнозов нет.</div>`;
            return;
        }

        list.innerHTML = filtered.map((item) => analysisRowTemplate(item)).join("");
        list.querySelectorAll("[data-analysis-id]").forEach((node) => {
            node.addEventListener("click", () => openAnalysisDialog(findRecord(node.dataset.analysisId)));
            node.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openAnalysisDialog(findRecord(node.dataset.analysisId));
                }
            });
        });
    }

    function analysisRowTemplate(item) {
        const probability = number(item.modelProbability || item.probability) * 100;
        const edge = number(item.edge) * 100;
        const bestSelection = item.bestBetSelection;
        const pick = bestSelection?.pick || item.pick;
        const odds = bestSelection?.odds || item.bookmakerOdds || item.odds;
        return `
            <div class="analysis-row ${item.isBestBet ? "is-best" : ""}" data-analysis-id="${escapeHtml(item.id)}" tabindex="0" role="button">
                <span class="analysis-rank">${escapeHtml(item.rank || "—")}</span>
                <div class="analysis-match">
                    <small>${escapeHtml(item.sportLabel || sportName(item.sport))} · ${escapeHtml(item.country || "")}</small>
                    <strong>${escapeHtml(item.home)} — ${escapeHtml(item.away)}</strong>
                    <span>${escapeHtml(item.league || "")} · ${formatMatchTime(item.commenceTime || item.utcDate)}</span>
                </div>
                <div class="analysis-pick">
                    <small>${item.isBestBet ? "Лучшая ставка" : "Лучший рынок матча"}</small>
                    <strong>${escapeHtml(pick || "—")}</strong>
                </div>
                <div class="analysis-stat"><small>Вероятность</small><strong>${formatNumber(probability, 1)}%</strong></div>
                <div class="analysis-stat"><small>Коэфф.</small><strong>${formatNumber(odds, 2)}</strong></div>
                <div class="analysis-stat"><small>Edge</small><strong class="${edge >= 0 ? "positive" : ""}">${formatSignedNumber(edge, 1)}</strong></div>
                <span class="analysis-chevron">›</span>
            </div>`;
    }

    function renderBank(bank = {}, statistics = {}) {
        const starting = number(bank.starting);
        const current = number(bank.current);
        const active = number(bank.activeExposure);
        const roi = number(bank.roi);
        setText("currentBank", formatCurrency(current));
        setText("startingBank", formatCurrency(starting));
        setText("activeExposure", formatCurrency(active));
        setText("activeExposurePercent", current > 0 ? `${formatNumber((active / current) * 100, 0)}% текущего банка` : "—");
        setText("maxDrawdown", `${formatNumber(bank.maxDrawdown, 2)}%`);
        setText("averageOdds", number(statistics.averageOdds) > 0 ? formatNumber(statistics.averageOdds, 2) : "—");
        setText("bankRoi", formatSignedPercent(roi));
        document.getElementById("bankRoi")?.classList.toggle("is-negative", roi < 0);
        const history = Array.isArray(bank.history) ? bank.history : [];
        setText("bankHistoryCaption", history.length ? `${history.length} зафиксированных точек` : "История накапливается");
        renderBankChart(bank);
    }

    function renderBankChart(bank = {}) {
        const canvas = document.getElementById("bankChart");
        if (!(canvas instanceof HTMLCanvasElement)) return;
        const history = Array.isArray(bank.history) ? bank.history : [];
        const values = history.map((item) => number(item.value)).filter((value) => Number.isFinite(value));
        if (!values.length) values.push(number(bank.starting || bank.current || 10000));

        if (runtime.chartFrame) cancelAnimationFrame(runtime.chartFrame);
        runtime.chartFrame = requestAnimationFrame(() => {
            const rect = canvas.getBoundingClientRect();
            const ratio = Math.min(window.devicePixelRatio || 1, 2);
            canvas.width = Math.max(1, Math.floor(rect.width * ratio));
            canvas.height = Math.max(1, Math.floor(rect.height * ratio));
            const ctx = canvas.getContext("2d");
            ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
            const width = rect.width;
            const height = rect.height;
            const padding = { top: 24, right: 10, bottom: 25, left: 10 };
            const innerWidth = width - padding.left - padding.right;
            const innerHeight = height - padding.top - padding.bottom;
            const minimum = Math.min(...values);
            const maximum = Math.max(...values);
            const range = Math.max(1, maximum - minimum);

            ctx.clearRect(0, 0, width, height);
            ctx.strokeStyle = "rgba(255,255,255,.055)";
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i += 1) {
                const y = padding.top + (innerHeight / 4) * i;
                ctx.beginPath();
                ctx.moveTo(padding.left, y);
                ctx.lineTo(width - padding.right, y);
                ctx.stroke();
            }

            const points = values.map((value, index) => ({
                x: padding.left + (values.length === 1 ? innerWidth / 2 : (innerWidth * index) / (values.length - 1)),
                y: padding.top + innerHeight - ((value - minimum) / range) * innerHeight,
            }));

            const gradient = ctx.createLinearGradient(0, padding.top, 0, height);
            gradient.addColorStop(0, "rgba(184,255,74,.24)");
            gradient.addColorStop(1, "rgba(184,255,74,0)");
            ctx.beginPath();
            ctx.moveTo(points[0].x, height - padding.bottom);
            points.forEach((point) => ctx.lineTo(point.x, point.y));
            ctx.lineTo(points[points.length - 1].x, height - padding.bottom);
            ctx.closePath();
            ctx.fillStyle = gradient;
            ctx.fill();

            ctx.beginPath();
            points.forEach((point, index) => {
                if (index === 0) ctx.moveTo(point.x, point.y);
                else ctx.lineTo(point.x, point.y);
            });
            ctx.strokeStyle = "#b8ff4a";
            ctx.lineWidth = 2.2;
            ctx.lineJoin = "round";
            ctx.lineCap = "round";
            ctx.shadowColor = "rgba(184,255,74,.38)";
            ctx.shadowBlur = 12;
            ctx.stroke();
            ctx.shadowBlur = 0;

            const last = points[points.length - 1];
            ctx.beginPath();
            ctx.arc(last.x, last.y, 4.5, 0, Math.PI * 2);
            ctx.fillStyle = "#b8ff4a";
            ctx.fill();
        });
    }

    function renderLearning(learning = {}, statistics = {}) {
        const analysisAccuracy = number(statistics.analysisAccuracy);
        const bestAccuracy = number(statistics.bestBetsAccuracy);
        setText("analysisAccuracy", formatPercent(analysisAccuracy));
        setText("bestAccuracy", formatPercent(bestAccuracy));
        setText("settledAnalysisCount", `${number(statistics.settledAnalyses)} завершено`);
        setText("settledBestCount", `${number(statistics.settledBestBets)} завершено`);
        setText("currentStreak", statistics.currentStreak || "Нет завершённой серии");
        setOrbit("analysisAccuracyOrbit", analysisAccuracy);
        setOrbit("bestAccuracyOrbit", bestAccuracy);
        renderCalibration(learning.calibrationBins || {});
        renderSegments(learning.segments || {});
    }

    function setOrbit(id, value) {
        const node = document.getElementById(id);
        node?.style.setProperty("--score", `${Math.max(0, Math.min(100, number(value))) * 3.6}deg`);
    }

    function renderCalibration(bins) {
        const container = document.getElementById("calibrationChart");
        if (!container) return;
        const entries = Object.entries(bins)
            .filter(([, value]) => number(value?.count) > 0)
            .sort(([left], [right]) => left.localeCompare(right));
        if (!entries.length) {
            container.innerHTML = `<div class="empty-mini">Недостаточно завершённых прогнозов</div>`;
            return;
        }
        container.innerHTML = entries.map(([label, value]) => {
            const predicted = Math.max(2, number(value.averagePredicted) * 100);
            const actual = Math.max(2, number(value.actualRate) * 100);
            return `
                <div class="calibration-bin" title="${escapeHtml(label)}% · ${number(value.count)} прогнозов">
                    <div class="calibration-bars">
                        <i style="height:${predicted}%"></i>
                        <i style="height:${actual}%"></i>
                    </div>
                    <span>${escapeHtml(label)}</span>
                </div>`;
        }).join("");
    }

    function renderSegments(segments) {
        const container = document.getElementById("segmentList");
        if (!container) return;
        const entries = Object.entries(segments)
            .filter(([key, value]) => key.startsWith("MARKET|") && number(value?.settled) >= 10)
            .sort(([, left], [, right]) => number(right.hitRate) - number(left.hitRate))
            .slice(0, 5);
        if (!entries.length) {
            container.innerHTML = `<div class="empty-mini">Данные накапливаются</div>`;
            return;
        }
        container.innerHTML = entries.map(([key, value]) => {
            const parts = key.split("|");
            return `
                <div class="segment-item">
                    <div><strong>${escapeHtml(segmentName(parts[2]))}</strong><span>${escapeHtml(sportName(parts[1]))} · ${number(value.settled)} результатов</span></div>
                    <b>${formatPercent(number(value.hitRate) * 100)}</b>
                </div>`;
        }).join("");
    }

    function renderHistory(history) {
        const container = document.getElementById("historyTable");
        if (!container) return;
        const records = history
            .filter((item) => item && (item.recordType === "BEST_BET" || !item.recordType))
            .filter((item) => runtime.historyFilter === "all" || String(item.status || "pending") === runtime.historyFilter)
            .slice()
            .sort((left, right) => String(right.publishedAt || right.commenceTime || "").localeCompare(String(left.publishedAt || left.commenceTime || "")))
            .slice(0, 30);

        if (!records.length) {
            container.innerHTML = `<div class="analysis-loading">Для выбранного фильтра записей нет.</div>`;
            return;
        }

        container.innerHTML = records.map((item) => {
            const status = String(item.status || "pending");
            const profit = number(item.profit);
            return `
                <div class="history-row">
                    <div class="history-match"><strong>${escapeHtml(item.home || "")} — ${escapeHtml(item.away || "")}</strong><span>${escapeHtml(item.sportLabel || sportName(item.sport))} · ${escapeHtml(item.league || "")} · ${formatShortDate(item.commenceTime || item.utcDate)}</span></div>
                    <div class="history-pick"><strong>${escapeHtml(item.pick || "—")}</strong><span>${formatNumber(item.bookmakerOdds || item.odds, 2)} · ${escapeHtml(item.bookmaker || "коэффициент зафиксирован")}</span></div>
                    <div class="history-cell"><span>Ставка</span><strong>${formatCurrency(item.stake)}</strong></div>
                    <div class="history-cell"><span>Счёт</span><strong>${escapeHtml(item.score || "—")}</strong></div>
                    <div class="history-cell"><span>Результат</span><strong class="${profit > 0 ? "positive" : ""}">${profit ? formatSignedCurrency(profit) : "—"}</strong></div>
                    <span class="status-chip ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span>
                </div>`;
        }).join("");
    }

    function initializeFilters() {
        document.querySelectorAll("[data-sport-filter]").forEach((button) => {
            button.addEventListener("click", () => {
                runtime.sportFilter = button.dataset.sportFilter || "all";
                document.querySelectorAll("[data-sport-filter]").forEach((node) => node.classList.toggle("is-active", node === button));
                renderDailyAnalysis(runtime.state?.dailyAnalysis || []);
            });
        });
        document.querySelectorAll("[data-history-filter]").forEach((button) => {
            button.addEventListener("click", () => {
                runtime.historyFilter = button.dataset.historyFilter || "all";
                document.querySelectorAll("[data-history-filter]").forEach((node) => node.classList.toggle("is-active", node === button));
                renderHistory(runtime.state?.history || []);
            });
        });
    }

    function initializeDialog() {
        const dialog = document.getElementById("analysisDialog");
        document.getElementById("dialogClose")?.addEventListener("click", () => dialog?.close());
        dialog?.addEventListener("click", (event) => {
            if (event.target === dialog) dialog.close();
        });
    }

    function findRecord(id) {
        if (!runtime.state) return null;
        return [...runtime.state.dailyAnalysis, ...runtime.state.bestBets].find((item) => String(item.id) === String(id)) || null;
    }

    function openAnalysisDialog(record) {
        if (!record) return;
        const dialog = document.getElementById("analysisDialog");
        const content = document.getElementById("dialogContent");
        if (!dialog || !content) return;
        const probability = number(record.modelProbability || record.probability) * 100;
        const marketProbability = number(record.marketProbability) * 100;
        const edge = number(record.edge) * 100;
        const ev = number(record.expectedValue) * 100;
        const alternatives = Array.isArray(record.alternatives) ? record.alternatives : [];
        const scores = Array.isArray(record.mostLikelyScores) ? record.mostLikelyScores : [];
        const qualificationFailures = record.qualification?.failures || [];

        content.innerHTML = `
            <div class="dialog-content">
                <div class="dialog-eyebrow">${escapeHtml(record.sportLabel || sportName(record.sport))} · ${escapeHtml(record.country || "")} · ${escapeHtml(record.league || "")}</div>
                <h3>${escapeHtml(record.home || "")} — ${escapeHtml(record.away || "")}</h3>
                <div class="dialog-subline">${formatMatchTime(record.commenceTime || record.utcDate)} · ${escapeHtml(record.expectedResult || "")}</div>

                <div class="dialog-hero-grid">
                    <div class="dialog-pick"><span>Лучший рынок</span><strong>${escapeHtml(record.pick || "—")}</strong><b>${formatNumber(record.bookmakerOdds || record.odds, 2)}</b></div>
                    <div class="dialog-score"><span>Ожидаемый счёт</span><strong>${escapeHtml(record.expectedScore || "—")}</strong></div>
                </div>

                <div class="dialog-metrics">
                    <div class="dialog-metric"><span>Модель</span><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Рынок</span><strong>${formatNumber(marketProbability, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Edge</span><strong>${formatSignedNumber(edge, 1)} п.п.</strong></div>
                    <div class="dialog-metric"><span>EV</span><strong>${formatSignedNumber(ev, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Данные</span><strong>${formatNumber(record.dataQuality, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Согласие</span><strong>${formatNumber(record.agreement, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Аномальность</span><strong>${formatNumber(record.anomaly, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Букмекеры</span><strong>${formatNumber(record.quoteCount, 0)}</strong></div>
                </div>

                <div class="dialog-section"><h4>Почему выбран этот прогноз</h4><p>${escapeHtml(record.reason || "Аналитическое объяснение отсутствует.")}</p></div>
                <div class="dialog-section"><h4>Наиболее вероятные счета</h4><div class="score-probabilities">${scores.length ? scores.map((item) => `<span>${escapeHtml(item.score)} · ${formatNumber(number(item.probability) * 100, 1)}%</span>`).join("") : "<span>Недостаточно данных</span>"}</div></div>
                <div class="dialog-section"><h4>Альтернативные рынки</h4><div class="alternative-grid">${alternatives.length ? alternatives.map((item) => `<div class="alternative-card"><strong>${escapeHtml(item.pick || "—")}</strong><span>${formatNumber(item.probabilityPercent || number(item.probability) * 100, 1)}% · коэффициент ${formatNumber(item.odds || item.bookmakerOdds, 2)}</span></div>`).join("") : '<div class="empty-mini">Альтернативы не опубликованы</div>'}</div></div>
                <div class="dialog-section"><h4>Статус квалификации</h4><p>${record.qualification?.qualified ? "Прогноз прошёл пороги вероятности, преимущества, качества данных и аномальности." : qualificationFailures.length ? escapeHtml(qualificationFailures.join("; ")) : "Используется в аналитической выборке, но не включён в виртуальный банк."}</p></div>
            </div>`;
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
    }

    function initializeRevealObserver() {
        document.body.classList.add("has-motion");
        const nodes = document.querySelectorAll(".reveal");
        if (!("IntersectionObserver" in window)) {
            nodes.forEach((node) => node.classList.add("is-visible"));
            return;
        }
        const observer = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.classList.add("is-visible");
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.08 });
        nodes.forEach((node) => observer.observe(node));
    }

    function updateFreshnessLabels() {
        if (!runtime.state) return;
        const updated = new Date(runtime.state.meta?.updatedAt || 0);
        if (!Number.isFinite(updated.getTime())) return;
        const minutes = Math.max(0, Math.round((Date.now() - updated.getTime()) / 60_000));
        const text = minutes < 2 ? "только что" : minutes < 60 ? `${minutes} мин назад` : minutes < 1440 ? `${Math.floor(minutes / 60)} ч назад` : formatShortDate(updated);
        setText("heroUpdated", text);
    }

    function setConnectionState(state, message = "") {
        const toast = document.getElementById("connectionToast");
        if (!toast) return;
        toast.className = `connection-toast ${message ? "is-visible" : ""}`;
        const label = toast.querySelector("strong");
        if (label) label.textContent = message || "";
        toast.dataset.state = state;
        if (message && state === "ready") {
            window.setTimeout(() => toast.classList.remove("is-visible"), 2200);
        }
    }

    function renderLoadError() {
        const best = document.getElementById("bestBetsGrid");
        const analysis = document.getElementById("analysisList");
        if (best) best.innerHTML = `<div class="empty-state"><strong>Данные временно недоступны</strong><p>Сайт повторит загрузку автоматически. Последнее сохранённое состояние не удаляется.</p></div>`;
        if (analysis) analysis.innerHTML = `<div class="analysis-loading">Ожидаем восстановление соединения…</div>`;
    }

    function runtimeStatus(item) {
        const status = String(item.status || "pending");
        if (status !== "pending") return statusLabel(status);
        const kickoff = new Date(item.commenceTime || item.utcDate || 0);
        if (!Number.isFinite(kickoff.getTime())) return "ожидается";
        const diff = kickoff.getTime() - Date.now();
        if (diff <= 0) return "матч начался";
        const minutes = Math.ceil(diff / 60_000);
        if (minutes < 60) return `через ${minutes} мин`;
        if (minutes < 1440) return `через ${Math.floor(minutes / 60)} ч`;
        return `через ${Math.floor(minutes / 1440)} дн`;
    }

    function statusLabel(status) {
        return ({ pending: "Ожидается", won: "Выигрыш", lost: "Проигрыш", push: "Возврат", void: "Отмена" })[status] || status;
    }

    function statusClass(status) {
        return ({ won: "is-won", lost: "is-lost", push: "is-push" })[status] || "";
    }

    function sportName(value) {
        return value === "ice_hockey" ? "Хоккей" : value === "soccer" ? "Футбол" : value || "Спорт";
    }

    function segmentName(value) {
        return ({ OUTCOME: "Исходы", TOTAL: "Тоталы", HANDICAP: "Форы", BTTS: "Обе забьют", DOUBLE_CHANCE: "Двойной шанс", DRAW_NO_BET: "Фора 0" })[value] || value || "Другой рынок";
    }

    function setText(id, value) {
        const node = document.getElementById(id);
        if (node) node.textContent = String(value ?? "—");
    }

    function number(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function average(values) {
        const valid = values.filter(Number.isFinite);
        return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : 0;
    }

    function formatCurrency(value) {
        return new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB", maximumFractionDigits: 2 }).format(number(value));
    }

    function formatSignedCurrency(value) {
        const amount = number(value);
        return `${amount > 0 ? "+" : ""}${formatCurrency(amount)}`;
    }

    function formatNumber(value, digits = 0) {
        return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(number(value));
    }

    function formatSignedNumber(value, digits = 1) {
        const amount = number(value);
        return `${amount > 0 ? "+" : ""}${formatNumber(amount, digits)}`;
    }

    function formatPercent(value) {
        return `${formatNumber(value, 1)}%`;
    }

    function formatSignedPercent(value) {
        return `${number(value) > 0 ? "+" : ""}${formatNumber(value, 2)}%`;
    }

    function formatDateTime(value) {
        const date = value instanceof Date ? value : new Date(value || 0);
        if (!Number.isFinite(date.getTime())) return "—";
        return new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW_TIME_ZONE, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
    }

    function formatCompactDateTime(value) {
        const date = new Date(value || 0);
        if (!Number.isFinite(date.getTime())) return "—";
        return new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW_TIME_ZONE, hour: "2-digit", minute: "2-digit" }).format(date);
    }

    function formatShortDate(value) {
        const date = value instanceof Date ? value : new Date(value || 0);
        if (!Number.isFinite(date.getTime())) return "—";
        return new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW_TIME_ZONE, day: "2-digit", month: "short", year: "numeric" }).format(date);
    }

    function formatMatchTime(value) {
        const date = new Date(value || 0);
        if (!Number.isFinite(date.getTime())) return "Время уточняется";
        return new Intl.DateTimeFormat("ru-RU", { timeZone: MOSCOW_TIME_ZONE, weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function debounce(callback, delay) {
        let timeout;
        return (...args) => {
            window.clearTimeout(timeout);
            timeout = window.setTimeout(() => callback(...args), delay);
        };
    }
})();
