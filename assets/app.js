/* V10_SITE_PREMIUM_DASHBOARD */
/* V10_R6_LIVE_STATISTICS_RUSSIAN_UI */
(() => {
    "use strict";

    const STATE_URL = "data/state.json";
    const LIVE_URL = "data/live-state.json";
    const REFRESH_INTERVAL_MS = 60_000;
    const MOSCOW_TIME_ZONE = "Europe/Moscow";

    const runtime = {
        state: null,
        liveState: null,
        signature: "",
        sportFilter: "all",
        historyFilter: "all",
        historyScope: "best",
        statisticsTab: "market",
        refreshTimer: null,
        freshnessTimer: null,
        chartFrame: null,
    };

    document.addEventListener("DOMContentLoaded", initialize);

    async function initialize() {
        initializeRevealObserver();
        initializeFilters();
        initializeDialog();
        initializeMobileNavigation();
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
            const [stateResult, liveResult] = await Promise.allSettled([
                fetch(`${STATE_URL}?v=${Date.now()}`, {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                }),
                fetch(`${LIVE_URL}?v=${Date.now()}`, {
                    cache: "no-store",
                    headers: { Accept: "application/json" },
                }),
            ]);
            if (stateResult.status !== "fulfilled" || !stateResult.value.ok) {
                const status = stateResult.status === "fulfilled" ? stateResult.value.status : "сеть";
                throw new Error(`Не удалось загрузить основное состояние: ${status}`);
            }
            const state = await stateResult.value.json();
            let liveState = runtime.liveState || normalizeLiveState({});
            if (liveResult.status === "fulfilled" && liveResult.value.ok) {
                liveState = normalizeLiveState(await liveResult.value.json());
            }
            const signature = createStateSignature(state, liveState);
            const changed = signature !== runtime.signature;
            runtime.state = normalizeState(state);
            runtime.liveState = liveState;
            runtime.signature = signature;
            renderApplication(runtime.state, runtime.liveState);
            setConnectionState("ready", notify || changed ? "Данные актуализированы" : "");
        } catch (error) {
            console.error("Не удалось загрузить состояние", error);
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
        normalized.analysisHistory = Array.isArray(normalized.analysisHistory) ? normalized.analysisHistory : [];
        return normalized;
    }

    function normalizeLiveState(value) {
        const normalized = value && typeof value === "object" ? value : {};
        normalized.events = Array.isArray(normalized.events) ? normalized.events : [];
        normalized.activeEventIds = Array.isArray(normalized.activeEventIds) ? normalized.activeEventIds : [];
        normalized.providerHealth = normalized.providerHealth && typeof normalized.providerHealth === "object" ? normalized.providerHealth : {};
        return normalized;
    }

    function createStateSignature(state, liveState) {
        const meta = state?.meta || {};
        const bank = state?.bank || {};
        return [
            meta.updatedAt || "",
            meta.version || "",
            state?.dailyAnalysis?.length || 0,
            state?.bestBets?.length || state?.predictions?.length || 0,
            bank.current || 0,
            state?.history?.length || 0,
            liveState?.updatedAt || "",
            liveState?.events?.length || 0,
            liveState?.activeEventIds?.length || 0,
        ].join("|");
    }

    function renderApplication(state, liveState) {
        renderMeta(state, liveState);
        renderLiveMatches(liveState);
        renderBestBets(state.bestBets, state.meta, state.bank);
        renderDailyAnalysis(state.dailyAnalysis);
        renderBank(state.bank, state.statistics);
        renderStatistics(state.statistics, state.learning);
        renderLearning(state.learning, state.statistics);
        renderHistory(state.history, state.analysisHistory);
        updateFreshnessLabels();
    }

    function renderMeta(state, liveState) {
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
        setText("stripLive", Array.isArray(liveState?.activeEventIds) ? liveState.activeEventIds.length : 0);
        setText("stripAccuracy", formatPercent(statistics.bestBetsAccuracy));
        setText("stripRoi", formatSignedPercent(bank.roi));
        setText("footerUpdated", `Обновлено ${formatDateTime(meta.updatedAt)}`);

        const status = String(meta.status || "DEGRADED").toUpperCase();
        const statusNode = document.getElementById("topbarStatus");
        statusNode?.classList.toggle("is-green", status === "GREEN");
        statusNode?.classList.toggle("is-red", status === "RED");
        setText("systemStatus", status === "GREEN" ? "Актуально" : status === "RED" ? "Ошибка" : "Ограничено");
    }

    function liveEventFor(record) {
        const eventId = String(record?.eventId || "");
        return (runtime.liveState?.events || []).find((item) => String(item?.eventId || "") === eventId) || null;
    }

    function renderLiveMatches(liveState = {}) {
        const grid = document.getElementById("liveMatchGrid");
        if (!grid) return;
        const events = Array.isArray(liveState.events) ? liveState.events : [];
        const active = events.filter((item) => item.status === "LIVE");
        const relevant = (active.length ? active : events.filter((item) => item.status === "SCHEDULED").slice(0, 4)).slice(0, 6);
        setText("liveUpdated", liveState.updatedAt ? `Обновлено ${formatDateTime(liveState.updatedAt)}` : "Ожидание обновления");
        setText("liveStatus", active.length ? `Сейчас идут: ${active.length}` : events.length ? "Отслеживание расписания" : "Нет активных матчей");
        if (!relevant.length) {
            grid.innerHTML = `<div class="analysis-loading">Система автоматически покажет счёт, когда начнётся один из 15 отслеживаемых матчей.</div>`;
            return;
        }
        grid.innerHTML = relevant.map((item) => liveMatchTemplate(item)).join("");
    }

    function liveMatchTemplate(item) {
        const probability = number(item.liveProbability) * 100;
        const hasScore = item.homeScore !== null && item.homeScore !== undefined && item.awayScore !== null && item.awayScore !== undefined;
        const status = item.status === "LIVE" ? "Матч идёт" : item.statusRu || "Ожидается";
        return `
            <article class="live-match-card ${item.status === "LIVE" ? "is-live" : ""}">
                <div class="live-match-top">
                    <span class="live-pulse">${escapeHtml(status)}</span>
                    <span>${escapeHtml(item.sportLabel || sportName(item.sport))}</span>
                </div>
                <small>${escapeHtml([russianDisplayText(item.countryRu || item.country), russianDisplayText(item.leagueRu || item.league)].filter(Boolean).join(" · "))}</small>
                <div class="live-score-line">
                    <strong>${escapeHtml(russianDisplayText(item.homeRu || item.home))}</strong>
                    <b>${hasScore ? escapeHtml(item.score) : "— : —"}</b>
                    <strong>${escapeHtml(russianDisplayText(item.awayRu || item.away))}</strong>
                </div>
                <div class="live-clock">${escapeHtml(item.clockLabel || formatMatchTime(item.commenceTime))}</div>
                <div class="live-prediction-row">
                    <div><span>Исходный прогноз</span><strong>${escapeHtml(russianDisplayText(item.pickRu || item.pick || "—"))}</strong></div>
                    <div><span>Текущая вероятность</span><strong>${formatNumber(probability, 1)}%</strong></div>
                </div>
                <p>${escapeHtml(item.liveReason || "Текущая оценка появится после получения счёта.")}</p>
            </article>`;
    }

    function liveInlineTemplate(record) {
        const live = liveEventFor(record);
        if (!live || live.status !== "LIVE") return "";
        return `<div class="inline-live"><span>Матч идёт · ${escapeHtml(live.clockLabel || "сейчас")}</span><strong>${escapeHtml(live.score || "— : —")}</strong><b>${formatNumber(number(live.liveProbability) * 100, 1)}%</b></div>`;
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
                    <small>${escapeHtml([displayCountry(bet), displayLeague(bet)].filter(Boolean).join(" · "))}</small>
                    <h3>${escapeHtml(displayTeam(bet, "home"))} — ${escapeHtml(displayTeam(bet, "away"))}</h3>
                    <time>${formatMatchTime(bet.commenceTime || bet.utcDate)} · ${escapeHtml(runtimeStatus(bet))}</time>
                </div>
                <div class="bet-selection">
                    <div>
                        <span>Выбранный рынок</span>
                        <strong>${escapeHtml(displayPick(bet))}</strong>
                    </div>
                    <div class="bet-odds">
                        <span>Коэффициент</span>
                        <strong>${formatNumber(bet.bookmakerOdds || bet.odds, 2)}</strong>
                    </div>
                </div>
                <div class="bet-metrics">
                    <div class="metric-block"><span>Вероятность</span><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="metric-block"><span>Преимущество</span><strong class="${edge >= 0 ? "is-positive" : ""}">${formatSignedNumber(edge, 1)} п.п.</strong></div>
                    <div class="metric-block"><span>Ожидаемая доходность</span><strong class="${ev >= 0 ? "is-positive" : ""}">${formatSignedNumber(ev, 1)}%</strong></div>
                    <div class="metric-block"><span>Данные</span><strong>${formatNumber(dataQuality, 0)}/100</strong></div>
                </div>
                ${liveInlineTemplate(bet)}
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
                    <small>${escapeHtml(item.sportLabel || sportName(item.sport))} · ${escapeHtml(displayCountry(item))}</small>
                    <strong>${escapeHtml(displayTeam(item, "home"))} — ${escapeHtml(displayTeam(item, "away"))}</strong>
                    <span>${escapeHtml(displayLeague(item))} · ${formatMatchTime(item.commenceTime || item.utcDate)}</span>
                    ${liveInlineTemplate(item)}
                </div>
                <div class="analysis-pick">
                    <small>${item.isBestBet ? "Лучшая ставка" : "Лучший рынок матча"}</small>
                    <strong>${escapeHtml(russianDisplayText(pick || "—"))}</strong>
                </div>
                <div class="analysis-stats">
                    <div class="analysis-stat analysis-probability"><small>Вероятность</small><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="analysis-stat analysis-odds"><small>Коэффициент</small><strong>${formatNumber(odds, 2)}</strong></div>
                    <div class="analysis-stat analysis-edge"><small>Преимущество</small><strong class="${edge >= 0 ? "positive" : ""}">${formatSignedNumber(edge, 1)} п.п.</strong></div>
                </div>
                <span class="analysis-chevron">›</span>
            </div>`;
    }

    function renderBank(bank = {}, statistics = {}) {
        const starting = number(bank.starting);
        const current = number(bank.current);
        const active = Math.max(0, number(bank.activeExposure));
        const available = Math.max(0, current - active);
        const profit = current - starting;
        const roi = number(bank.roi);
        setText("currentBank", formatCurrency(current));
        setText("startingBank", formatCurrency(starting));
        setText("activeExposure", formatCurrency(active));
        setText("bankExposureInline", formatCurrency(active));
        setText("availableBank", formatCurrency(available));
        setText("availableBankCard", formatCurrency(available));
        setText("bankProfit", formatSignedCurrency(profit));
        setText("activeExposurePercent", current > 0 ? `${formatNumber((active / current) * 100, 0)}% текущего банка` : "—");
        setText("maxDrawdown", `${formatNumber(bank.maxDrawdown, 2)}%`);
        setText("bankRoi", formatSignedPercent(roi));
        document.getElementById("bankRoi")?.classList.toggle("is-negative", roi < 0);
        document.getElementById("bankProfit")?.classList.toggle("is-negative", profit < 0);
        const history = Array.isArray(bank.history) ? bank.history : [];
        setText("bankHistoryCaption", history.length ? `${history.length} закрытых точек` : "История накапливается");
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
            const compact = width < 520;
            const padding = { top: 24, right: 12, bottom: compact ? 30 : 34, left: compact ? 44 : 60 };
            const innerWidth = Math.max(1, width - padding.left - padding.right);
            const innerHeight = Math.max(1, height - padding.top - padding.bottom);
            const minimumRaw = Math.min(...values);
            const maximumRaw = Math.max(...values);
            const visualMargin = Math.max(1, (maximumRaw - minimumRaw) * 0.12);
            const minimum = minimumRaw - visualMargin;
            const maximum = maximumRaw + visualMargin;
            const range = Math.max(1, maximum - minimum);
            const ruble = new Intl.NumberFormat("ru-RU", {
                notation: "compact",
                maximumFractionDigits: 1,
            });

            ctx.clearRect(0, 0, width, height);
            ctx.font = `${compact ? 9 : 10}px Inter, sans-serif`;
            ctx.textBaseline = "middle";
            ctx.fillStyle = "rgba(183,192,207,.72)";
            ctx.strokeStyle = "rgba(255,255,255,.055)";
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i += 1) {
                const y = padding.top + (innerHeight / 4) * i;
                const labelValue = maximum - (range / 4) * i;
                ctx.beginPath();
                ctx.moveTo(padding.left, y);
                ctx.lineTo(width - padding.right, y);
                ctx.stroke();
                ctx.textAlign = "right";
                ctx.fillText(ruble.format(labelValue), padding.left - 8, y);
            }

            const points = values.map((value, index) => ({
                x: padding.left + (values.length === 1 ? innerWidth / 2 : (innerWidth * index) / (values.length - 1)),
                y: padding.top + innerHeight - ((value - minimum) / range) * innerHeight,
            }));

            const starting = number(bank.starting);
            if (starting > 0 && starting >= minimum && starting <= maximum) {
                const baselineY = padding.top + innerHeight - ((starting - minimum) / range) * innerHeight;
                ctx.save();
                ctx.setLineDash([5, 5]);
                ctx.strokeStyle = "rgba(95,224,255,.26)";
                ctx.beginPath();
                ctx.moveTo(padding.left, baselineY);
                ctx.lineTo(width - padding.right, baselineY);
                ctx.stroke();
                ctx.restore();
            }

            const gradient = ctx.createLinearGradient(0, padding.top, 0, height);
            gradient.addColorStop(0, "rgba(184,255,74,.25)");
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
            ctx.lineWidth = compact ? 2 : 2.3;
            ctx.lineJoin = "round";
            ctx.lineCap = "round";
            ctx.shadowColor = "rgba(184,255,74,.35)";
            ctx.shadowBlur = 10;
            ctx.stroke();
            ctx.shadowBlur = 0;

            points.forEach((point, index) => {
                ctx.beginPath();
                ctx.arc(point.x, point.y, index === points.length - 1 ? 4.5 : 2.6, 0, Math.PI * 2);
                ctx.fillStyle = index === points.length - 1 ? "#b8ff4a" : "rgba(184,255,74,.72)";
                ctx.fill();
            });

            const firstDate = history[0]?.date;
            const lastDate = history[history.length - 1]?.date;
            ctx.fillStyle = "rgba(183,192,207,.66)";
            ctx.textBaseline = "alphabetic";
            ctx.textAlign = "left";
            if (firstDate) ctx.fillText(formatChartDate(firstDate), padding.left, height - 7);
            ctx.textAlign = "right";
            if (lastDate) ctx.fillText(formatChartDate(lastDate), width - padding.right, height - 7);
        });
    }

    function formatChartDate(value) {
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) return "";
        return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short" })
            .format(date)
            .replace(".", "");
    }

    function renderStatistics(statistics = {}, learning = {}) {
        const all = statistics.allPredictions || {};
        const best = statistics.bestBets || {};
        const thirty = statistics.windows?.["30"]?.allPredictions || {};
        const readiness = learning.modelReadiness || {};

        setText("allPredictionAccuracy", formatPercent(all.accuracy));
        setText("allPredictionSettled", `${number(all.settled)} завершено`);
        setText("allPredictionWon", number(all.won));
        setText("allPredictionLost", number(all.lost));
        setText("allPredictionPush", number(all.push));
        setText("allPredictionPending", number(all.pending));

        setText("bestPredictionAccuracy", formatPercent(best.accuracy));
        setText("bestPredictionSettled", `${number(best.settled)} завершено`);
        setText("bestPredictionWon", number(best.won));
        setText("bestPredictionLost", number(best.lost));
        setText("bestPredictionPush", number(best.push));
        setText("bestPredictionPending", number(best.pending));

        setText("windowThirtyAccuracy", formatPercent(thirty.accuracy));
        setText("windowThirtySettled", `${number(thirty.settled)} завершено`);
        setText("modelLearningStage", readiness.stage || "Сбор выборки");
        setText("modelLearningSamples", `${number(readiness.settledSamples)} результатов`);
        renderStatisticsBreakdown(statistics);
    }

    function renderStatisticsBreakdown(statistics = {}) {
        const container = document.getElementById("statisticsBreakdownList");
        if (!container) return;
        const source = runtime.statisticsTab === "sport"
            ? statistics.bySport
            : runtime.statisticsTab === "odds"
              ? statistics.byOddsBand
              : runtime.statisticsTab === "league"
                ? statistics.byLeague
                : statistics.byMarket;
        const rows = Array.isArray(source) ? source.slice(0, 12) : [];
        if (!rows.length) {
            container.innerHTML = `<div class="empty-mini">Для этого раздела пока недостаточно завершённых прогнозов</div>`;
            return;
        }
        container.innerHTML = rows.map((item) => {
            const key = runtime.statisticsTab === "market" ? segmentName(item.key) : russianDisplayText(item.key);
            return `
                <div class="statistics-breakdown-row">
                    <div><strong>${escapeHtml(key)}</strong><span>${number(item.decided)} решений · ${number(item.push)} возвратов</span></div>
                    <div><span>Прошло</span><strong>${number(item.won)}</strong></div>
                    <div><span>Не прошло</span><strong>${number(item.lost)}</strong></div>
                    <b>${formatPercent(item.accuracy)}</b>
                </div>`;
        }).join("");
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

    function renderHistory(history, analysisHistory) {
        const container = document.getElementById("historyTable");
        if (!container) return;
        const sourceRecords = runtime.historyScope === "all" ? analysisHistory : history;
        const records = (Array.isArray(sourceRecords) ? sourceRecords : [])
            .filter((item) => item && (runtime.historyScope === "all" || item.recordType === "BEST_BET" || !item.recordType))
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
                    <div class="history-match"><strong>${escapeHtml(displayTeam(item, "home"))} — ${escapeHtml(displayTeam(item, "away"))}</strong><span>${escapeHtml(item.sportLabel || sportName(item.sport))} · ${escapeHtml(displayLeague(item))} · ${formatShortDate(item.commenceTime || item.utcDate)}</span></div>
                    <div class="history-pick"><strong>${escapeHtml(displayPick(item))}</strong><span>${formatNumber(item.bookmakerOdds || item.odds, 2)} · ${escapeHtml(displayBookmaker(item))}</span></div>
                    <div class="history-cell"><span>${runtime.historyScope === "all" ? "Вероятность" : "Ставка"}</span><strong>${runtime.historyScope === "all" ? formatPercent(number(item.modelProbability || item.probability) * 100) : formatCurrency(item.stake)}</strong></div>
                    <div class="history-cell"><span>Счёт</span><strong>${escapeHtml(item.score || "—")}</strong></div>
                    <div class="history-cell"><span>Результат</span><strong class="${profit > 0 ? "positive" : ""}">${runtime.historyScope === "all" ? statusLabel(status) : profit ? formatSignedCurrency(profit) : statusLabel(status)}</strong></div>
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
        document.querySelectorAll("[data-history-scope]").forEach((button) => {
            button.addEventListener("click", () => {
                runtime.historyScope = button.dataset.historyScope || "best";
                document.querySelectorAll("[data-history-scope]").forEach((node) => node.classList.toggle("is-active", node === button));
                renderHistory(runtime.state?.history || [], runtime.state?.analysisHistory || []);
            });
        });
        document.querySelectorAll("[data-statistics-tab]").forEach((button) => {
            button.addEventListener("click", () => {
                runtime.statisticsTab = button.dataset.statisticsTab || "market";
                document.querySelectorAll("[data-statistics-tab]").forEach((node) => node.classList.toggle("is-active", node === button));
                renderStatisticsBreakdown(runtime.state?.statistics || {});
            });
        });
        document.querySelectorAll("[data-history-filter]").forEach((button) => {
            button.addEventListener("click", () => {
                runtime.historyFilter = button.dataset.historyFilter || "all";
                document.querySelectorAll("[data-history-filter]").forEach((node) => node.classList.toggle("is-active", node === button));
                renderHistory(runtime.state?.history || [], runtime.state?.analysisHistory || []);
            });
        });
    }

    function initializeMobileNavigation() {
        const links = [...document.querySelectorAll("[data-mobile-nav]")];
        if (!links.length || !("IntersectionObserver" in window)) return;
        const sections = links
            .map((link) => document.getElementById(link.dataset.mobileNav || ""))
            .filter(Boolean);
        const observer = new IntersectionObserver((entries) => {
            const visible = entries
                .filter((entry) => entry.isIntersecting)
                .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
            if (!visible) return;
            links.forEach((link) => {
                const active = link.dataset.mobileNav === visible.target.id;
                link.classList.toggle("is-active", active);
                if (active) link.setAttribute("aria-current", "page");
                else link.removeAttribute("aria-current");
            });
        }, {
            rootMargin: "-22% 0px -58% 0px",
            threshold: [0.01, 0.15, 0.35],
        });
        sections.forEach((section) => observer.observe(section));
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

    function dialogLiveTemplate(record) {
        const live = liveEventFor(record);
        if (!live || live.status !== "LIVE") return "";
        return `
            <div class="dialog-live-panel">
                <div><span>Матч идёт</span><strong>${escapeHtml(live.score || "— : —")}</strong></div>
                <div><span>${escapeHtml(live.clockLabel || "сейчас")}</span><strong>${formatNumber(number(live.liveProbability) * 100, 1)}%</strong></div>
                <p>${escapeHtml(live.liveReason || "Вероятность пересчитана по текущему счёту.")}</p>
            </div>`;
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
                <div class="dialog-eyebrow">${escapeHtml(record.sportLabel || sportName(record.sport))} · ${escapeHtml(displayCountry(record))} · ${escapeHtml(displayLeague(record))}</div>
                <h3>${escapeHtml(displayTeam(record, "home"))} — ${escapeHtml(displayTeam(record, "away"))}</h3>
                <div class="dialog-subline">${formatMatchTime(record.commenceTime || record.utcDate)} · ${escapeHtml(displayNarrative(record.expectedResultRu || record.expectedResult || ""))}</div>
                ${dialogLiveTemplate(record)}

                <div class="dialog-hero-grid">
                    <div class="dialog-pick"><span>Лучший рынок</span><strong>${escapeHtml(displayPick(record))}</strong><b>${formatNumber(record.bookmakerOdds || record.odds, 2)}</b></div>
                    <div class="dialog-score"><span>Ожидаемый счёт</span><strong>${escapeHtml(record.expectedScore || "—")}</strong></div>
                </div>

                <div class="dialog-metrics">
                    <div class="dialog-metric"><span>Модель</span><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Рынок</span><strong>${formatNumber(marketProbability, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Преимущество</span><strong>${formatSignedNumber(edge, 1)} п.п.</strong></div>
                    <div class="dialog-metric"><span>Ожидаемая доходность</span><strong>${formatSignedNumber(ev, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Данные</span><strong>${formatNumber(record.dataQuality, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Согласие</span><strong>${formatNumber(record.agreement, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Аномальность</span><strong>${formatNumber(record.anomaly, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Букмекеры</span><strong>${formatNumber(record.quoteCount, 0)}</strong></div>
                </div>

                <div class="dialog-section"><h4>Почему выбран этот прогноз</h4><p>${escapeHtml(displayNarrative(record.reasonRu || record.reason || "Аналитическое объяснение отсутствует."))}</p></div>
                <div class="dialog-section"><h4>Наиболее вероятные счета</h4><div class="score-probabilities">${scores.length ? scores.map((item) => `<span>${escapeHtml(item.score)} · ${formatNumber(number(item.probability) * 100, 1)}%</span>`).join("") : "<span>Недостаточно данных</span>"}</div></div>
                <div class="dialog-section"><h4>Альтернативные рынки</h4><div class="alternative-grid">${alternatives.length ? alternatives.map((item) => `<div class="alternative-card"><strong>${escapeHtml(displayPick(item))}</strong><span>${formatNumber(item.probabilityPercent || number(item.probability) * 100, 1)}% · коэффициент ${formatNumber(item.odds || item.bookmakerOdds, 2)}</span></div>`).join("") : '<div class="empty-mini">Альтернативы не опубликованы</div>'}</div></div>
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
        const live = liveEventFor(item);
        if (live?.status === "LIVE") {
            return `${live.statusRu || "Матч идёт"}${live.score ? ` · ${live.score}` : ""}`;
        }
        if (live?.status === "FINISHED") {
            return `матч завершён${live.score ? ` · ${live.score}` : ""}`;
        }
        const status = String(item.status || "pending");
        if (status !== "pending") return statusLabel(status).toLowerCase();
        const kickoff = new Date(item.commenceTime || item.utcDate || 0);
        if (!Number.isFinite(kickoff.getTime())) return "ожидается";
        const diff = kickoff.getTime() - Date.now();
        if (diff <= 0) return "ожидается текущий счёт";
        const minutes = Math.ceil(diff / 60_000);
        if (minutes < 60) return `через ${minutes} мин`;
        if (minutes < 1440) return `через ${Math.floor(minutes / 60)} ч`;
        return `через ${Math.floor(minutes / 1440)} дн`;
    }

    function statusLabel(status) {
        return ({ pending: "Ожидается", won: "Выигрыш", lost: "Проигрыш", push: "Возврат", void: "Отмена", cancelled: "Отмена", postponed: "Перенесён" })[String(status).toLowerCase()] || "Неизвестный статус";
    }

    function statusClass(status) {
        return ({ won: "is-won", lost: "is-lost", push: "is-push" })[status] || "";
    }

    function sportName(value) {
        return value === "ice_hockey" ? "Хоккей" : value === "soccer" ? "Футбол" : "Другой вид спорта";
    }

    function segmentName(value) {
        return ({ OUTCOME: "Исходы", TOTAL: "Тоталы", HANDICAP: "Форы", BTTS: "Обе забьют", DOUBLE_CHANCE: "Двойной шанс", DRAW_NO_BET: "Фора 0" })[value] || "Другой рынок";
    }

    const RUSSIAN_EXACT_NAMES = Object.freeze({
        "english premier league": "Английская Премьер-лига",
        "premier league": "Премьер-лига",
        "uefa champions league": "Лига чемпионов УЕФА",
        "uefa europa league": "Лига Европы УЕФА",
        "uefa conference league": "Лига конференций УЕФА",
        "copa libertadores": "Кубок Либертадорес",
        "copa sudamericana": "Южноамериканский кубок",
        "major league soccer": "Высшая футбольная лига США",
        "national hockey league": "Национальная хоккейная лига",
        "nhl": "НХЛ",
        "ahl": "АХЛ",
        "serie a": "Серия А",
        "serie b": "Серия Б",
        "la liga": "Ла Лига",
        "bundesliga": "Бундеслига",
        "ligue 1": "Лига 1",
        "ligue one": "Лига 1",
        "championship": "Чемпионшип",
        "world cup": "Чемпионат мира",
        "england": "Англия",
        "germany": "Германия",
        "spain": "Испания",
        "italy": "Италия",
        "france": "Франция",
        "brazil": "Бразилия",
        "argentina": "Аргентина",
        "united states": "США",
        "usa": "США",
        "canada": "Канада",
        "mexico": "Мексика",
        "netherlands": "Нидерланды",
        "portugal": "Португалия",
        "turkey": "Турция",
        "belgium": "Бельгия",
        "scotland": "Шотландия",
        "sweden": "Швеция",
        "finland": "Финляндия",
        "norway": "Норвегия",
        "denmark": "Дания",
        "australia": "Австралия",
        "japan": "Япония",
        "south korea": "Южная Корея",
        "china": "Китай",
        "chile": "Чили",
        "colombia": "Колумбия"
    });

    const RUSSIAN_WORDS = Object.freeze({
        "fc": "ФК", "cf": "ФК", "sc": "СК", "ac": "АК", "hc": "ХК",
        "united": "Юнайтед", "city": "Сити", "town": "Таун", "county": "Каунти",
        "athletic": "Атлетик", "athletics": "Атлетик", "sporting": "Спортинг",
        "club": "Клуб", "football": "Футбол", "hockey": "Хоккей",
        "women": "Женщины", "woman": "Женщины", "reserve": "Резерв",
        "reserves": "Резерв", "youth": "Молодёжная команда", "academy": "Академия",
        "under": "Младше", "over": "Больше", "draw": "Ничья", "home": "Хозяева",
        "away": "Гости", "total": "Тотал", "totals": "Тоталы", "spread": "Фора",
        "spreads": "Форы", "cup": "Кубок", "league": "Лига", "premier": "Премьер",
        "national": "Национальная", "international": "Международный",
        "conference": "Конференция", "division": "Дивизион", "championship": "Чемпионшип",
        "north": "Север", "south": "Юг", "east": "Восток", "west": "Запад",
        "central": "Центр", "regional": "Региональная", "state": "Штат",
        "university": "Университет", "college": "Колледж", "real": "Реал"
    });

    const TRANSLIT_PAIRS = Object.freeze([
        ["shch", "щ"], ["sch", "щ"], ["yo", "ё"], ["zh", "ж"],
        ["kh", "х"], ["ts", "ц"], ["ch", "ч"], ["sh", "ш"],
        ["yu", "ю"], ["ya", "я"], ["ye", "е"], ["ph", "ф"],
        ["th", "т"], ["ck", "к"], ["qu", "кв"]
    ]);

    function transliterateLatinWord(word) {
        const lower = String(word || "").toLowerCase();
        if (RUSSIAN_WORDS[lower]) return RUSSIAN_WORDS[lower];
        let source = lower;
        let result = "";
        const singles = {
            a: "а", b: "б", c: "к", d: "д", e: "е", f: "ф", g: "г",
            h: "х", i: "и", j: "дж", k: "к", l: "л", m: "м", n: "н",
            o: "о", p: "п", q: "к", r: "р", s: "с", t: "т", u: "у",
            v: "в", w: "в", x: "кс", y: "й", z: "з"
        };
        while (source.length) {
            let matched = false;
            for (const [latin, russian] of TRANSLIT_PAIRS) {
                if (source.startsWith(latin)) {
                    result += russian;
                    source = source.slice(latin.length);
                    matched = true;
                    break;
                }
            }
            if (!matched) {
                const char = source[0];
                result += singles[char] || char;
                source = source.slice(1);
            }
        }
        if (/^[A-Z]/.test(word)) {
            result = result.charAt(0).toUpperCase() + result.slice(1);
        }
        return result;
    }

    function russianDisplayText(value) {
        const original = String(value ?? "").trim();
        if (!original) return "";
        const exact = RUSSIAN_EXACT_NAMES[original.toLowerCase()];
        if (exact) return exact;
        return original
            .replace(/\b1X\b/gi, "1Х")
            .replace(/\bX2\b/gi, "Х2")
            .replace(/\bBTTS\b/gi, "Обе забьют")
            .replace(/\bDNB\b/gi, "Фора 0")
            .replace(/[A-Za-z]+/g, transliterateLatinWord)
            .replace(/\s+/g, " ")
            .trim();
    }

    function displayTeam(record, side) {
        const field = side === "home" ? "homeRu" : "awayRu";
        return russianDisplayText(record?.[field] || record?.[side] || "");
    }

    function displayCountry(record) {
        return russianDisplayText(record?.countryRu || record?.country || "");
    }

    function displayLeague(record) {
        return russianDisplayText(record?.leagueRu || record?.league || "");
    }

    function displayPick(record) {
        return russianDisplayText(record?.pickRu || record?.pick || "—");
    }

    function displayBookmaker(record) {
        const source = record?.bookmakerRu || record?.bookmaker || "";
        return source ? russianDisplayText(source) : "коэффициент зафиксирован";
    }

    function displayNarrative(value) {
        return russianDisplayText(value || "");
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
