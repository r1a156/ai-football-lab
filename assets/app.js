/* V10_SITE_PREMIUM_DASHBOARD */
/* V10_R6_LIVE_STATISTICS_RUSSIAN_UI */
/* V10_R7_CLEAN_HISTORY_AND_LIVE_EXPIRY */
/* V10_R8_ATOMIC_BATCH_AND_LINKED_BANK */
/* V10_R9_BEST_FOUR_STATS_AND_FRESH_SELECTION */
/* V10_R15_MATCH_INTELLIGENCE_EXPRESS_PORTFOLIO */
/* V10_R15F_R3R4_LAYOUT_POLISH */
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
        historyFilter: "settled",
        historyScope: "all",
        statisticsTab: "market",
        refreshTimer: null,
        freshnessTimer: null,
        chartFrame: null,
        phaseTimer: null,
    };

    document.addEventListener("DOMContentLoaded", initialize);

    async function initialize() {
        initializeRevealObserver();
        initializeFilters();
        initializeDialog();
        initializeMobileNavigation();
        window.addEventListener("resize", debounce(() => renderBankChart(runtime.state?.expressBank || runtime.state?.bank), 120));
        window.addEventListener("online", () => loadState({ notify: true }));
        window.addEventListener("offline", () => setConnectionState("offline"));

        await loadState({ notify: false });
        runtime.refreshTimer = window.setInterval(() => loadState({ notify: false }), REFRESH_INTERVAL_MS);
        runtime.freshnessTimer = window.setInterval(updateFreshnessLabels, 30_000);
        runtime.phaseTimer = window.setInterval(updatePhaseClock, 1_000);
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
            document.body.classList.add("app-ready");
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
        normalized.expressBank = normalized.expressBank && typeof normalized.expressBank === "object" ? normalized.expressBank : normalized.bank;
        normalized.expresses = Array.isArray(normalized.expresses) ? normalized.expresses : [];
        normalized.expressHistory = Array.isArray(normalized.expressHistory) ? normalized.expressHistory : [];
        normalized.dataCoverage = normalized.dataCoverage && typeof normalized.dataCoverage === "object" ? normalized.dataCoverage : {};
        normalized.statistics = normalized.statistics && typeof normalized.statistics === "object" ? normalized.statistics : {};
        normalized.learning = normalized.learning && typeof normalized.learning === "object" ? normalized.learning : {};
        normalized.batch = normalized.batch && typeof normalized.batch === "object" ? normalized.batch : {};
        normalized.dailyAnalysis = Array.isArray(normalized.dailyAnalysis) ? normalized.dailyAnalysis : [];
        normalized.bestBets = Array.isArray(normalized.bestBets)
            ? normalized.bestBets
            : Array.isArray(normalized.predictions)
              ? normalized.predictions
              : [];
        normalized.history = Array.isArray(normalized.history) ? normalized.history : [];
        normalized.analysisHistory = Array.isArray(normalized.analysisHistory) ? normalized.analysisHistory : [];
        normalized.dailyAudit = normalized.dailyAudit && typeof normalized.dailyAudit === "object" ? normalized.dailyAudit : {};
        normalized.systemNarrative = normalized.systemNarrative && typeof normalized.systemNarrative === "object" ? normalized.systemNarrative : {};
        normalized.nextPortfolio = normalized.nextPortfolio && typeof normalized.nextPortfolio === "object" ? normalized.nextPortfolio : {};
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
        const bank = state?.expressBank || state?.bank || {};
        return [
            meta.updatedAt || "",
            meta.version || "",
            state?.dailyAnalysis?.length || 0,
            state?.bestBets?.length || state?.predictions?.length || 0,
            state?.expresses?.length || 0,
            (state?.expresses || []).map((item) => `${item?.id || ""}:${item?.status || ""}:${item?.profit || 0}`).join(","),
            (state?.bestBets || state?.predictions || []).map((item) => item?.id || item?.eventId || "").join(","),
            (state?.dailyAnalysis || []).map((item) => item?.id || item?.eventId || "").join(","),
            state?.statistics?.bestBets?.settled || 0,
            state?.statistics?.bestBets?.profit || 0,
            bank.current || 0,
            bank.placedAmount ?? bank.activeExposure ?? 0,
            bank.available ?? 0,
            state?.batch?.id || "",
            state?.batch?.status || "",
            state?.batch?.terminalAnalysisCount || 0,
            state?.history?.length || 0,
            state?.dailyAudit?.status || "",
            state?.dailyAudit?.modelUsed || "",
            state?.nextPortfolio?.status || "",
            state?.nextPortfolio?.windowStart || "",
            state?.systemNarrative?.updatedAt || "",
            liveState?.updatedAt || "",
            liveState?.events?.length || 0,
            liveState?.activeEventIds?.length || 0,
        ].join("|");
    }

    function renderApplication(state, liveState) {
        const unified = isUnifiedPublication(state);
        document.body.classList.toggle("has-unified-publication", unified);
        document.body.classList.toggle("is-transition-publication", !unified);
        renderMeta(state, liveState);
        renderSystemPhase(state, liveState);
        renderSystemNarrative(state);
        renderLiveMatches(liveState);
        renderDailyAnalysis(state.dailyAnalysis);
        renderExpresses(state.expresses, state.expressBank, state.meta, state.dataCoverage, state.statistics);
        renderBestBets(state.bestBets, state.meta, state.expressBank, state.statistics);
        renderBank(state.expressBank, state.statistics);
        renderStatistics(state.statistics, state.learning);
        renderLearning(state.learning, state.statistics);
        renderHistory(state.history, state.analysisHistory);
        updateFreshnessLabels();
    }

    function isUnifiedPublication(state = runtime.state || {}) {
        const daily = Array.isArray(state.dailyAnalysis) ? state.dailyAnalysis : [];
        const expresses = Array.isArray(state.expresses) ? state.expresses : [];
        const marker = String(state.meta?.sourceMarker || "");
        const markedDaily = daily.length === 15 && daily.every((row) => String(row?.sourceMarker || marker).includes("R15"));
        return markedDaily && expresses.length === 3 && expresses.every((row) => Array.isArray(row?.legs) && row.legs.length === 5);
    }

    function renderMeta(state, liveState) {
        const { meta = {}, statistics = {}, dailyAnalysis = [], bestBets = [], expresses = [] } = state;
        const bank = state.expressBank || state.bank || {};
        const unified = isUnifiedPublication(state);
        const quality = dailyAnalysis.length ? average(dailyAnalysis.map((item) => number(item.dataQuality))) : 0;
        const leagueCount = Number(meta.leaguesAnalyzed ?? new Set(dailyAnalysis.map((item) => item.league).filter(Boolean)).size);
        const placed = number(bank.placedAmount ?? bank.activeExposure ?? 0);

        setText("heroAnalysisCount", unified ? dailyAnalysis.length : 0);
        setText("heroBestCount", unified ? expresses.length : 0);
        setText("singleCount", unified ? Math.min(3, bestBets.length) : 0);
        setText("portfolioQuality", unified ? `${formatNumber(quality, 0)}/100` : "—");
        setText("heroLeagueCount", leagueCount);
        setText("heroBank", formatCurrency(bank.current || bank.starting || 10000));
        setText("portfolioExposure", placed > 0 ? `${formatCurrency(placed)} в работе` : "банк свободен");
        setText("heroUpdated", formatCompactDateTime(meta.updatedAt));
        setText("portfolioUpdated", meta.updatedAt ? `Обновлено ${formatDateTime(meta.updatedAt)}` : "Ожидание обновления");
        const preparing = String(state.nextPortfolio?.status || meta.nextPortfolioStatus || "").includes("PREPARING");
        setText("portfolioStatus", unified ? "Портфель опубликован" : preparing ? "Готовим следующие полные сутки" : dailyAnalysis.length ? "Завершается предыдущая подборка" : "Сканирование операционных суток");
        setText("stripSoccer", unified ? dailyAnalysis.length : 0);
        setText("stripHockey", unified ? Math.min(3, bestBets.length) : 0);
        setText("stripLive", Array.isArray(liveState?.activeEventIds) ? liveState.activeEventIds.length : 0);
        setText("stripAccuracy", formatPercent(statistics?.expresses?.accuracy));
        setText("stripRoi", formatSignedPercent(bank.roi));
        setText("footerUpdated", `Обновлено ${formatDateTime(meta.updatedAt)}`);

        const stateCard = document.getElementById("portfolioStateCard");
        stateCard?.classList.toggle("is-ready", unified);
        stateCard?.classList.toggle("is-waiting", !unified);

        const status = String(meta.status || "DEGRADED").toUpperCase();
        const statusNode = document.getElementById("topbarStatus");
        statusNode?.classList.toggle("is-green", status === "GREEN" || preparing);
        statusNode?.classList.toggle("is-red", status === "RED");
        setText("systemStatus", unified ? "Портфель активен" : preparing ? "Подготовка" : status === "RED" ? "Ошибка" : "Обновляется");
    }


    function countdownText(targetValue) {
        const target = new Date(targetValue || 0).getTime();
        if (!Number.isFinite(target)) return "—";
        const delta = Math.max(0, target - Date.now());
        const totalMinutes = Math.floor(delta / 60_000);
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        const seconds = Math.floor((delta % 60_000) / 1000);
        if (days > 0) return `${days} дн. ${hours} ч. ${minutes} мин.`;
        if (hours > 0) return `${hours} ч. ${minutes} мин. ${seconds} сек.`;
        return `${minutes} мин. ${seconds} сек.`;
    }

    function determineSystemPhase(state, liveState) {
        const active = (liveState?.events || []).filter((item) => item?.status === "LIVE");
        if (active.length) return "LIVE";
        if (isUnifiedPublication(state)) {
            const pending = (state.dailyAnalysis || []).filter((item) => String(item?.status || "pending") === "pending");
            return pending.length ? "PUBLISHED" : "LEARNING";
        }
        const nextStatus = String(state.nextPortfolio?.status || state.meta?.nextPortfolioStatus || "");
        if (nextStatus.includes("PREPARING")) return "PREPARATION";
        const audit = String(state.dailyAudit?.status || "");
        if (audit && !["NOT_CONFIGURED", "DISABLED", "SKIPPED"].includes(audit)) return "AUDIT";
        if (number(state.dataCoverage?.oddsEvents) > 0) return "MODELLING";
        return "SCAN";
    }

    function phaseCopy(phase, state, liveState) {
        const active = (liveState?.events || []).filter((item) => item?.status === "LIVE");
        const nextPortfolio = state.nextPortfolio || {};
        const firstStart = (state.dailyAnalysis || [])
            .map((item) => new Date(item.commenceTime || 0))
            .filter((date) => Number.isFinite(date.getTime()) && date.getTime() > Date.now())
            .sort((a, b) => a - b)[0];
        const map = {
            PREPARATION: ["Готовлю следующий полный операционный день", "Обновляю историю команд, источники и календарь. Частичный день не публикуется.", nextPortfolio.windowStart || state.meta?.firstActiveWindowStart, "до первого полного сбора"],
            SCAN: ["Сканирую все матчи суток", "Сопоставляю реальные события с историей команд и удаляю матчи вне окна 08:00–08:00 МСК.", state.meta?.operationalWindowEnd, "до закрытия окна"],
            MODELLING: ["Сравниваю все допустимые рынки", "Рассчитываю исходы, тоталы, обе забьют и командные тоталы. Слабые данные не проходят в портфель.", null, "идёт расчёт"],
            AUDIT: ["Проверяю портфель большой бесплатной моделью", "AI получает только рассчитанные факты, может снизить уверенность и найти слабые места, но не может придумать события.", null, "один аудит в сутки"],
            PUBLISHED: ["Портфель зафиксирован", firstStart ? "Следующий этап — предматчевая проверка и live-сопровождение каждого плеча." : "Все матчи завершены, готовлю итоговое обучение.", firstStart?.toISOString(), firstStart ? "до ближайшего матча" : "до обучения"],
            LIVE: ["Сопровождаю матчи в реальном времени", `${active.length} ${active.length === 1 ? "матч идёт" : "матча идут"}. Исходные прогнозы не подменяются, меняется только live-оценка.`, null, "live-контроль активен"],
            LEARNING: ["Сверяю прогнозы с фактом", "Один матч даёт один независимый результат. Ошибки раскладываются по рынкам, лигам, качеству данных и диапазону коэффициентов.", state.meta?.firstActiveWindowStart, "до нового цикла"],
        };
        return map[phase] || map.SCAN;
    }

    function renderSystemPhase(state, liveState) {
        const phase = determineSystemPhase(state, liveState);
        const [title, description, target, label] = phaseCopy(phase, state, liveState);
        document.body.dataset.systemPhase = phase.toLowerCase();
        setText("systemPhaseTitle", title);
        setText("systemPhaseDescription", description);
        setText("systemPhaseClock", target ? countdownText(target) : phase === "LIVE" ? "LIVE" : "АКТИВНО");
        setText("systemPhaseClockLabel", label);
        const audit = state.dailyAudit || {};
        setText("auditStatus", audit.schemaValid ? "Проверка выполнена" : audit.status === "NOT_CONFIGURED" ? "Ключ не найден" : audit.status === "FAILED_FALLBACK" ? "Статистический fallback" : "Ожидается");
        setText("auditModel", audit.modelUsed || (audit.status === "FAILED_FALLBACK" ? "математическое ядро" : "openrouter/free"));
        const fonbet = state.dataCoverage?.fonbetMode || "";
        setText("marketGateStatus", fonbet === "REQUIRED_CONFIRMED_POOL" ? "Фонбет подтверждён" : number(state.dataCoverage?.oddsEvents) ? "Линия получена" : "Проверяется");
        setText("marketGateDetails", `${number(state.dataCoverage?.fonbetConfirmedEvents)} подтверждено · ${number(state.dataCoverage?.oddsEvents)} с коэффициентами`);
        const first = state.nextPortfolio?.windowStart || state.meta?.firstActiveWindowStart || state.meta?.operationalWindowStart;
        setText("firstActiveDay", first ? formatShortDate(first) : "—");
        document.querySelectorAll("[data-phase-step]").forEach((node) => {
            const order = ["PREPARATION", "SCAN", "MODELLING", "AUDIT", "PUBLISHED", "LIVE", "LEARNING"];
            const nodeIndex = order.indexOf(node.dataset.phaseStep || "");
            const activeIndex = order.indexOf(phase);
            node.classList.toggle("is-active", nodeIndex === activeIndex);
            node.classList.toggle("is-complete", nodeIndex < activeIndex && activeIndex >= 0);
        });
    }

    function renderSystemNarrative(state) {
        const narrative = state.systemNarrative || {};
        setText("systemNarrativeTitle", narrative.title || "Я учусь на подтверждённых результатах");
        setText("systemNarrativeLead", narrative.lead || "Каждый прогноз проходит единый контроль данных, рынка и риска.");
        setText("systemNarrativeBody", narrative.body || "После завершения матчей система сравнит прогноз с фактом и обновит калибровку.");
        setText("systemNarrativeSource", narrative.modelUsed ? `AI-аудит · ${narrative.modelUsed}` : "Статистическое ядро");
        const focus = Array.isArray(narrative.focus) && narrative.focus.length ? narrative.focus : ["Не завышать уверенность", "Искать повторяющиеся ошибки"];
        const container = document.getElementById("systemNarrativeFocus");
        if (container) container.innerHTML = focus.slice(0, 4).map((item) => `<span>${escapeHtml(item)}</span>`).join("");
    }

    function updatePhaseClock() {
        if (!runtime.state) return;
        const phase = determineSystemPhase(runtime.state, runtime.liveState);
        const [, , target, label] = phaseCopy(phase, runtime.state, runtime.liveState);
        setText("systemPhaseClock", target ? countdownText(target) : phase === "LIVE" ? "LIVE" : "АКТИВНО");
        setText("systemPhaseClockLabel", label);
        const waiting = document.querySelector("#liveWaitingCountdown");
        const nextStart = runtime.state?.nextPortfolio?.windowStart || runtime.state?.meta?.firstActiveWindowStart;
        if (waiting && nextStart) waiting.textContent = countdownText(nextStart);
    }

    function liveEventFor(record) {
        const eventId = String(record?.eventId || "");
        return (runtime.liveState?.events || []).find((item) => String(item?.eventId || "") === eventId) || null;
    }

    function renderLiveMatches(liveState = {}) {
        const grid = document.getElementById("liveMatchGrid");
        const section = document.getElementById("live-matches");
        if (!grid || !section) return;

        const events = (Array.isArray(liveState.events) ? liveState.events : [])
            .filter((item) => item && ["LIVE", "SCHEDULED"].includes(String(item.status || "")));
        const active = events.filter((item) => item.status === "LIVE");
        const scheduled = events
            .filter((item) => item.status === "SCHEDULED")
            .sort((left, right) => String(left.commenceTime || "").localeCompare(String(right.commenceTime || "")));
        const relevant = (active.length ? active : scheduled.slice(0, 2)).slice(0, 3);

        setText("liveUpdated", liveState.updatedAt ? `Обновлено ${formatDateTime(liveState.updatedAt)}` : "Ожидание обновления");
        setText("liveStatus", active.length ? `Сейчас идут: ${active.length}` : scheduled.length ? `Ближайшие: ${Math.min(scheduled.length, 2)}` : "Нет активных матчей");
        section.classList.toggle("has-live-events", Boolean(relevant.length));

        if (!relevant.length) {
            const nextPortfolio = runtime.state?.nextPortfolio || {};
            const nextStart = nextPortfolio.windowStart || runtime.state?.meta?.firstActiveWindowStart;
            grid.innerHTML = `<article class="waiting-live-card"><span class="waiting-radar" aria-hidden="true"><i></i><b></b></span><div><strong>${nextStart ? "Система готовит следующие сутки" : "Live-монитор готов"}</strong><p>${nextStart ? `Первый полный анализ начнётся ${formatDateTime(nextStart)}. До публикации банк не задействуется.` : "Когда начнётся первый матч портфеля, здесь появятся счёт, минута и влияние на экспрессы."}</p><small id="liveWaitingCountdown">${nextStart ? countdownText(nextStart) : "Ожидание ближайшего старта"}</small></div></article>`;
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

    function renderExpresses(expresses = [], bank = {}, meta = {}, coverage = {}, statistics = {}) {
        const grid = document.getElementById("expressGrid");
        if (!grid) return;
        const current = number(bank.current || bank.starting || 10000);
        const placed = number(bank.placedAmount ?? bank.activeExposure ?? 0);
        const available = number(bank.available ?? Math.max(0, current - placed));
        setText("expressBankCurrent", formatCurrency(current));
        setText("expressBankPlaced", formatCurrency(placed));
        setText("expressBankAvailable", formatCurrency(available));
        setText("expressUpdated", meta.analysisGeneratedAt ? `Опубликовано ${formatDateTime(meta.analysisGeneratedAt)}` : `Обновлено ${formatDateTime(meta.updatedAt)}`);
        setText("expressStatus", expresses.length ? `${expresses.length} экспресса · ${expresses.reduce((sum, item) => sum + number(item.legCount || item.legs?.length), 0)} событий` : "Ожидается качественная подборка");
        const windowStart = meta.operationalWindowStart ? formatCompactDateTime(meta.operationalWindowStart) : "08:00";
        const windowEnd = meta.operationalWindowEnd ? formatCompactDateTime(meta.operationalWindowEnd) : "08:00";
        setText("expressOperationalWindow", `${windowStart} — ${windowEnd}`);
        const discovered = number(coverage.discoveredEvents);
        const withOdds = number(coverage.oddsEvents);
        const qualified = number(coverage.qualifiedEvents);
        setText("dataCoverageSummary", discovered ? `${discovered} найдено · ${withOdds} с линией · ${qualified} прошли` : "Ожидание сканирования суток");
        const historyMatches = number(coverage.historyMatches);
        const matched = number(coverage.historyMatchedEvents);
        setText("historyCoverageSummary", historyMatches ? `${historyMatches} матчей в памяти · ${matched} событий сопоставлено` : "Историческая база наполняется");
        const providerValues = coverage.providerHealth && typeof coverage.providerHealth === "object"
            ? Object.values(coverage.providerHealth).filter((item) => item && typeof item === "object")
            : [];
        const freeSources = coverage.freeDataMesh?.sources && typeof coverage.freeDataMesh.sources === "object"
            ? Object.entries(coverage.freeDataMesh.sources)
            : [];
        const greenProviders = providerValues.filter((item) => String(item.status || "").toUpperCase() === "GREEN").length;
        const greenFree = freeSources.filter(([, status]) => ["GREEN", "PARTIAL", "NOT_MODIFIED"].includes(String(status || "").toUpperCase())).length;
        const sourceTotal = providerValues.length + freeSources.length;
        const sourceGreen = greenProviders + greenFree;
        setText("providerHealthSummary", sourceTotal
            ? `${sourceGreen} из ${sourceTotal} источников доступны · без новых ключей`
            : (meta.apiHealth?.status === "GREEN" ? "Источники доступны" : "Контроль источников активен"));

        if (!expresses.length) {
            grid.innerHTML = `<div class="empty-state"><strong>Подборка не зафиксирована</strong><p>Система не добавляет матчи следующих суток и не снижает требования. Публикация появится, когда внутри текущего окна найдутся 15 событий с полноценной историей и качественной линией.</p></div>`;
            return;
        }
        grid.innerHTML = expresses.map((item) => expressTemplate(item)).join("");
        grid.querySelectorAll("[data-analysis-id]").forEach((node) => {
            node.addEventListener("click", () => openAnalysisDialog(findRecord(node.dataset.analysisId)));
        });
    }

    function expressTemplate(item) {
        const legs = Array.isArray(item.legs) ? item.legs : [];
        const settled = legs.filter((leg) => ["won", "lost", "push", "void", "cancelled", "unresolved"].includes(String(leg.status || "pending"))).length;
        const won = legs.filter((leg) => String(leg.status) === "won").length;
        const lost = legs.filter((leg) => String(leg.status) === "lost").length;
        const status = String(item.status || "pending");
        const combinedOdds = number(item.settledCombinedOdds || item.combinedOdds);
        const probability = number(item.jointProbabilityPercent ?? number(item.jointProbability) * 100);
        const open = String(item.label || "").toUpperCase().includes("A") ? " open" : "";
        return `
            <details class="express-card ${statusClass(status)}"${open}>
                <summary>
                    <span class="express-summary-main">
                        <span class="express-summary-label">${escapeHtml(item.label || "Экспресс")}</span>
                        <strong class="express-summary-title">${legs.length} событий · ставка ${formatCurrency(item.stake)}</strong>
                        <small class="express-summary-meta">${settled} завершено · ${won} прошло · ${lost} не прошло</small>
                    </span>
                    <span class="express-summary-side"><strong>${formatNumber(combinedOdds, 3)}</strong><small>${formatNumber(probability, 2)}% совместно</small><span class="status-chip ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span></span>
                </summary>
                <div class="express-body">
                    <div class="express-metrics">
                        <div><span>Ставка</span><strong>${formatCurrency(item.stake)} · ${formatNumber(item.stakePercent, 0)}%</strong></div>
                        <div><span>Возможная выплата</span><strong>${formatCurrency(item.potentialPayout)}</strong></div>
                        <div><span>Совместная вероятность</span><strong>${formatNumber(probability, 2)}%</strong></div>
                        <div><span>Чистая прибыль</span><strong>${status === "won" ? formatSignedCurrency(item.profit) : formatCurrency(item.potentialProfit)}</strong></div>
                    </div>
                    <div class="express-progress"><span>${settled} из ${legs.length} завершены</span><strong>${won} прошло · ${lost} не прошло</strong></div>
                    <div class="express-legs">${legs.map((leg) => expressLegTemplate(leg)).join("")}</div>
                    <div class="express-card-footer"><span>Потенциальная чистая прибыль</span><strong>${status === "won" ? formatSignedCurrency(item.profit) : formatCurrency(item.potentialProfit)}</strong></div>
                </div>
            </details>`;
    }

    function expressLegTemplate(leg) {
        const status = String(leg.status || "pending");
        return `
            <button class="express-leg" type="button" data-analysis-id="${escapeHtml(leg.analysisId || "")}">
                <span class="express-leg-number">${escapeHtml(leg.legNumber || "—")}</span>
                <span class="express-leg-match"><small>${escapeHtml(displayLeague(leg))} · ${formatMatchTime(leg.commenceTime)}</small><strong>${escapeHtml(displayTeam(leg, "home"))} — ${escapeHtml(displayTeam(leg, "away"))}</strong><b>${escapeHtml(displayPick(leg))}</b></span>
                <span class="express-leg-price"><strong>${formatNumber(leg.odds, 2)}</strong><small>${formatNumber(number(leg.probability) * 100, 1)}%</small><span class="status-chip ${statusClass(status)}">${escapeHtml(leg.score || statusLabel(status))}</span></span>
            </button>`;
    }

    function topThreeStatistics() {
        const records = (runtime.state?.history || [])
            .filter((row) => row && String(row.recordType || "") === "BEST_BET" && number(row.rank) >= 1 && number(row.rank) <= 3);
        const canonical = canonicalHistoryRecords(records, "best");
        const settled = canonical.filter((row) => ["won", "lost", "push"].includes(String(row.status || "").toLowerCase()));
        const won = settled.filter((row) => String(row.status).toLowerCase() === "won").length;
        const lost = settled.filter((row) => String(row.status).toLowerCase() === "lost").length;
        const push = settled.filter((row) => String(row.status).toLowerCase() === "push").length;
        const decisive = won + lost;
        return {
            settled: settled.length,
            won,
            lost,
            push,
            accuracy: decisive ? (won / decisive) * 100 : 0,
            profit: settled.reduce((sum, row) => sum + number(row.profit), 0),
        };
    }

    function renderBestBets(bestBets, meta, bank, statistics = {}) {
        const grid = document.getElementById("bestBetsGrid");
        if (!grid) return;
        const unified = isUnifiedPublication();
        const singles = unified ? (Array.isArray(bestBets) ? bestBets.slice(0, 3) : []) : [];
        const batch = runtime.state?.batch || {};
        const sequence = number(batch.sequence);
        const batchLabel = unified ? "Единый портфель активен" : "Ожидается новая качественная подборка";
        setText("bestBetsUpdated", `${sequence ? `Подборка №${sequence} · ` : ""}${batchLabel} · ${formatDateTime(meta?.analysisGeneratedAt || meta?.updatedAt)}`);
        setText("bestBetsExposure", "Топ-3 общего рейтинга · без двойного списания банка");

        const derivedTopThree = topThreeStatistics();
        const bestStatistics = derivedTopThree.settled
            ? derivedTopThree
            : (statistics?.bestBets && typeof statistics.bestBets === "object" ? statistics.bestBets : {});
        setText("bestFourStatsAccuracy", formatPercent(bestStatistics.accuracy));
        setText("bestFourStatsSettled", number(bestStatistics.settled));
        setText("bestFourStatsWon", number(bestStatistics.won));
        setText("bestFourStatsLost", number(bestStatistics.lost));
        setText("bestFourStatsPush", number(bestStatistics.push));
        setText("bestFourStatsProfit", formatSignedCurrency(bestStatistics.profit));
        document.getElementById("bestFourStatsProfit")?.classList.toggle("is-negative", number(bestStatistics.profit) < 0);

        if (!singles.length) {
            grid.innerHTML = `<div class="empty-state"><strong>Три ординара ещё не зафиксированы</strong><p>Старая подборка не смешивается с новой стратегией. Ординары появятся одновременно с новыми 15 матчами и тремя экспрессами.</p></div>`;
            return;
        }

        grid.innerHTML = singles.map((bet, index) => bestBetTemplate(bet, index)).join("");
        grid.querySelectorAll("[data-analysis-id]").forEach((node) => {
            node.addEventListener("click", () => openAnalysisDialog(findRecord(node.dataset.analysisId)));
        });
    }

    function bestBetTemplate(bet, index) {
        const probability = number(bet.conservativeProbability || bet.modelProbability || bet.probability) * 100;
        const edge = number(bet.edge) * 100;
        const ev = number(bet.expectedValue) * 100;
        const dataQuality = number(bet.dataQuality);
        const status = String(bet.status || "pending");
        const rankLabel = index === 0 ? "Ординар №1 · лучший рейтинг" : `Ординар №${index + 1} · рейтинг ${index + 1}`;
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
                    <span>Роль в портфеле</span>
                    <strong>Топ-3 общего рейтинга</strong>
                    <span class="status-chip ${statusClass(status)}">${escapeHtml(statusLabel(status))}</span>
                </div>
            </article>`;
    }

    function renderDailyAnalysis(records) {
        const list = document.getElementById("analysisList");
        if (!list) return;
        const unified = isUnifiedPublication();
        const filtered = records
            .filter((item) => runtime.sportFilter === "all" || item.sport === runtime.sportFilter)
            .slice()
            .sort((left, right) => number(left.rank) - number(right.rank));

        const countries = new Set(records.map((item) => item.countryRu || item.country).filter(Boolean));
        const markets = new Set(records.map((item) => item.marketFamily || item.pickRu || item.pick).filter(Boolean));
        setText("analysisCount", unified ? records.length : 0);
        setText("countryCount", unified ? countries.size : 0);
        setText("marketFamilyCount", unified ? markets.size : 0);
        setText("averageDataQuality", unified && records.length ? `${formatNumber(average(records.map((item) => number(item.dataQuality))), 0)}/100` : "—");

        if (!unified) {
            const legacyRows = filtered.length
                ? `<div class="legacy-analysis-list is-visible-archive">${filtered.map((item) => analysisRowTemplate(item, true)).join("")}</div>`
                : "";
            list.innerHTML = `<div class="transition-state transition-state-compact"><span>ПОСЛЕДНИЙ СОХРАНЁННЫЙ ВЫПУСК</span><strong>${filtered.length ? `Показываем ${filtered.length} последних матчей` : "Новая единая подборка ещё не опубликована"}</strong><p>${filtered.length ? "Матчи доступны для просмотра, но помечены как предыдущая подборка и не считаются текущими ординарами или новыми экспрессами." : "Система ожидает новый полный пакет внутри текущего операционного окна."}</p></div>${legacyRows}`;
        } else {
            list.innerHTML = filtered.map((item) => analysisRowTemplate(item, false)).join("");
        }

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

    function analysisRowTemplate(item, legacy = false) {
        const probability = number(item.conservativeProbability || item.modelProbability || item.probability) * 100;
        const edge = number(item.edge) * 100;
        const quality = number(item.dataQuality);
        const bestSelection = item.bestBetSelection;
        const pick = bestSelection?.pickRu || bestSelection?.pick || item.pickRu || item.pick;
        const odds = bestSelection?.odds || item.bookmakerOdds || item.odds;
        const role = legacy
            ? "Предыдущая подборка"
            : item.expressLabel
              ? `${item.expressLabel}${item.expressLegNumber ? ` · плечо ${item.expressLegNumber}` : ""}`
              : number(item.rank) <= 3
                ? `Ординар №${item.rank}`
                : "Позиция общего рейтинга";
        return `
            <article class="analysis-row ${number(item.rank) <= 3 ? "is-best" : ""} ${legacy ? "is-legacy" : ""}" data-analysis-id="${escapeHtml(item.id)}" tabindex="0" role="button" aria-label="Открыть анализ матча ${escapeHtml(displayTeam(item, "home"))} — ${escapeHtml(displayTeam(item, "away"))}">
                <span class="analysis-rank">${escapeHtml(item.rank || "—")}</span>
                <div class="analysis-match">
                    <small>${escapeHtml(item.sportLabel || sportName(item.sport))} · ${escapeHtml(displayCountry(item))}</small>
                    <strong>${escapeHtml(displayTeam(item, "home"))} — ${escapeHtml(displayTeam(item, "away"))}</strong>
                    <span>${escapeHtml(displayLeague(item))} · ${formatMatchTime(item.commenceTime || item.utcDate)}</span>
                    ${liveInlineTemplate(item)}
                </div>
                <div class="analysis-pick">
                    <small>${escapeHtml(role)}</small>
                    <strong>${escapeHtml(russianDisplayText(pick || "—"))}</strong>
                </div>
                <div class="analysis-stats">
                    <div class="analysis-stat"><small>Вероятность</small><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="analysis-stat"><small>Коэффициент</small><strong>${formatNumber(odds, 2)}</strong></div>
                    <div class="analysis-stat"><small>Преимущество</small><strong class="${edge >= 0 ? "positive" : ""}">${formatSignedNumber(edge, 1)} п.п.</strong></div>
                    <div class="analysis-stat"><small>Данные</small><strong>${formatNumber(quality, 0)}/100</strong></div>
                </div>
                <span class="analysis-chevron">›</span>
            </article>`;
    }

    function renderBank(bank = {}, statistics = {}) {
        const starting = number(bank.starting || 10000);
        const current = number(bank.current || starting);
        const active = Math.max(0, number(bank.placedAmount ?? bank.activeExposure));
        const available = Math.max(0, number(bank.available ?? (current - active)));
        const activeCount = Math.max(0, number(bank.activeExpressCount ?? bank.activeBetsCount));
        const profit = current - starting;
        const roi = number(bank.roi);
        const legacy = runtime.state?.bank && typeof runtime.state.bank === "object" ? runtime.state.bank : {};
        setText("currentBank", formatCurrency(current));
        setText("startingBank", formatCurrency(current));
        setText("legacyBankCurrent", formatCurrency(legacy.current || legacy.starting || 0));
        setText("activeExposure", formatCurrency(active));
        setText("bankExposureInline", formatCurrency(active));
        setText("availableBank", formatCurrency(available));
        setText("availableBankCard", formatCurrency(available));
        setText("bankProfit", formatSignedCurrency(profit));
        setText("activeExposurePercent", current > 0 ? `${formatCount(activeCount, "экспресс", "экспресса", "экспрессов")} · ${formatNumber((active / current) * 100, 0)}% текущего банка` : "—");
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

    function historyRecordIsValid(item) {
        if (!item || typeof item !== "object") return false;
        const eventId = String(item.eventId || item.oddsEventId || item.sourceMatchId || "").trim();
        const home = String(item.homeRu || item.home || "").trim();
        const away = String(item.awayRu || item.away || "").trim();
        const market = String(item.market || item.pickRu || item.pick || "").trim();
        const commence = new Date(item.commenceTime || item.utcDate || 0);
        return Boolean(eventId && home && away && market && Number.isFinite(commence.getTime()));
    }

    function historyCanonicalKey(item, scope) {
        const eventId = String(item.eventId || item.oddsEventId || item.sourceMatchId || "");
        const published = new Date(item.publishedAt || item.createdAt || item.commenceTime || item.utcDate || 0);
        const day = Number.isFinite(published.getTime())
            ? published.toISOString().slice(0, 10)
            : "";
        return `${scope}|${eventId}|${day}`;
    }

    function historyRecordPriority(item) {
        const status = String(item.status || "pending").toLowerCase();
        const statusWeight = ({ won: 100, lost: 100, push: 100, void: 70, cancelled: 70, unresolved: 20, pending: 10 })[status] || 0;
        const scoreWeight = String(item.score || "").trim() ? 30 : 0;
        const settledWeight = item.settledAt ? 20 : 0;
        const stamp = new Date(item.settledAt || item.publishedAt || item.commenceTime || item.utcDate || 0).getTime();
        return statusWeight + scoreWeight + settledWeight + (Number.isFinite(stamp) ? stamp / 1e15 : 0);
    }

    function canonicalHistoryRecords(sourceRecords, scope) {
        const selected = new Map();
        for (const source of Array.isArray(sourceRecords) ? sourceRecords : []) {
            if (!historyRecordIsValid(source)) continue;
            if (scope === "best" && String(source.recordType || "") !== "BEST_BET") continue;
            const key = historyCanonicalKey(source, scope);
            const existing = selected.get(key);
            if (!existing || historyRecordPriority(source) > historyRecordPriority(existing)) {
                selected.set(key, source);
            }
        }
        return [...selected.values()];
    }

    function historyFilterMatches(item, filter) {
        const status = String(item.status || "pending").toLowerCase();
        if (filter === "settled") return ["won", "lost", "push"].includes(status);
        if (filter === "all") return true;
        return status === filter;
    }

    function renderHistory(history, analysisHistory) {
        const container = document.getElementById("historyTable");
        if (!container) return;

        const sourceRecords = runtime.historyScope === "all" ? analysisHistory : history;
        const canonical = canonicalHistoryRecords(sourceRecords, runtime.historyScope);
        const records = canonical
            .filter((item) => historyFilterMatches(item, runtime.historyFilter))
            .slice()
            .sort((left, right) => String(
                right.settledAt ||
                right.commenceTime ||
                right.utcDate ||
                right.publishedAt ||
                "",
            ).localeCompare(String(
                left.settledAt ||
                left.commenceTime ||
                left.utcDate ||
                left.publishedAt ||
                "",
            )))
            .slice(0, 100);

        const summary = document.getElementById("historySummary");
        if (summary) {
            const settled = canonical.filter((item) => ["won", "lost", "push"].includes(String(item.status || "").toLowerCase())).length;
            const pending = canonical.filter((item) => String(item.status || "").toLowerCase() === "pending").length;
            const unresolved = canonical.filter((item) => String(item.status || "").toLowerCase() === "unresolved").length;
            summary.textContent = `Записей: ${canonical.length} · завершено: ${settled} · ожидается: ${pending}${unresolved ? ` · не подтверждено: ${unresolved}` : ""}`;
        }

        if (!records.length) {
            container.innerHTML = `<div class="analysis-loading">Для выбранного фильтра чистых записей нет.</div>`;
            return;
        }

        container.innerHTML = records.map((item) => {
            const status = String(item.status || "pending").toLowerCase();
            const profit = number(item.profit);
            const score = String(item.score || "").trim() || (status === "pending" ? "Ожидается" : "—");
            const resultText = runtime.historyScope === "all"
                ? statusLabel(status)
                : profit
                    ? formatSignedCurrency(profit)
                    : statusLabel(status);
            return `
                <div class="history-row">
                    <div class="history-match">
                        <strong>${escapeHtml(displayTeam(item, "home"))} — ${escapeHtml(displayTeam(item, "away"))}</strong>
                        <span>${escapeHtml(item.sportLabel || sportName(item.sport))} · ${escapeHtml(displayLeague(item))} · ${formatShortDate(item.commenceTime || item.utcDate)}</span>
                    </div>
                    <div class="history-pick">
                        <strong>${escapeHtml(displayPick(item))}</strong>
                        <span>${formatNumber(item.bookmakerOdds || item.odds, 2)} · ${escapeHtml(displayBookmaker(item))}</span>
                    </div>
                    <div class="history-cell">
                        <span>${runtime.historyScope === "all" ? "Вероятность" : "Ставка"}</span>
                        <strong>${runtime.historyScope === "all" ? formatPercent(number(item.modelProbability || item.probability) * 100) : formatCurrency(item.stake)}</strong>
                    </div>
                    <div class="history-cell">
                        <span>Счёт</span>
                        <strong>${escapeHtml(score)}</strong>
                    </div>
                    <div class="history-cell">
                        <span>Результат</span>
                        <strong class="${profit > 0 ? "positive" : ""}">${escapeHtml(resultText)}</strong>
                    </div>
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
        links.forEach((link) => link.addEventListener("click", (event) => {
            const target = document.getElementById(link.dataset.mobileNav || "");
            if (!target) return;
            event.preventDefault();
            target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
            links.forEach((node) => node.classList.toggle("is-active", node === link));
        }));
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
        const probability = number(record.conservativeProbability || record.modelProbability || record.probability) * 100;
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
                    <div class="dialog-metric"><span>Консервативная вероятность</span><strong>${formatNumber(probability, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Рынок</span><strong>${formatNumber(marketProbability, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Преимущество</span><strong>${formatSignedNumber(edge, 1)} п.п.</strong></div>
                    <div class="dialog-metric"><span>Ожидаемая доходность</span><strong>${formatSignedNumber(ev, 1)}%</strong></div>
                    <div class="dialog-metric"><span>Данные</span><strong>${formatNumber(record.dataQuality, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Согласие</span><strong>${formatNumber(record.agreement, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Аномальность</span><strong>${formatNumber(record.anomaly, 0)}/100</strong></div>
                    <div class="dialog-metric"><span>Букмекеры</span><strong>${formatNumber(record.quoteCount, 0)}</strong></div>
                </div>

                ${matchDossierTemplate(record)}
                <div class="dialog-section"><h4>Почему выбран этот прогноз</h4><p>${escapeHtml((record.selectionRationale?.reasons || []).join(" · ") || displayNarrative(record.reasonRu || record.reason || "Аналитическое объяснение отсутствует."))}</p></div>
                <div class="dialog-section"><h4>Почему отклонены альтернативы</h4><div class="alternative-grid">${(record.selectionRationale?.rejectedAlternatives || []).length ? record.selectionRationale.rejectedAlternatives.map((item) => `<div class="alternative-card"><strong>${escapeHtml(russianDisplayText(item.pick || "—"))}</strong><span>${formatNumber(item.probabilityPercent, 1)}% · ${formatNumber(item.odds, 2)} · ${escapeHtml(item.reason || "Уступает выбранному рынку")}</span></div>`).join("") : '<div class="empty-mini">Выбранный рынок доминирует над опубликованными альтернативами</div>'}</div></div>
                <div class="dialog-section"><h4>Наиболее вероятные счета</h4><div class="score-probabilities">${scores.length ? scores.map((item) => `<span>${escapeHtml(item.score)} · ${formatNumber(number(item.probability) * 100, 1)}%</span>`).join("") : "<span>Недостаточно данных</span>"}</div></div>
                <div class="dialog-section"><h4>Альтернативные рынки</h4><div class="alternative-grid">${alternatives.length ? alternatives.map((item) => `<div class="alternative-card"><strong>${escapeHtml(displayPick(item))}</strong><span>${formatNumber(item.probabilityPercent || number(item.probability) * 100, 1)}% · коэффициент ${formatNumber(item.odds || item.bookmakerOdds, 2)}</span></div>`).join("") : '<div class="empty-mini">Альтернативы не опубликованы</div>'}</div></div>
                <div class="dialog-section"><h4>Статус квалификации</h4><p>${record.qualification?.qualified ? "Прогноз прошёл пороги вероятности, преимущества, качества данных и аномальности." : qualificationFailures.length ? escapeHtml(qualificationFailures.join("; ")) : "Используется в аналитической выборке, но не включён в виртуальный банк."}</p></div>
            </div>`;
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
    }

    function matchDossierTemplate(record) {
        const dossier = record?.matchDossier && typeof record.matchDossier === "object" ? record.matchDossier : {};
        const components = dossier.components && typeof dossier.components === "object" ? dossier.components : {};
        const home5 = components.homeForm5 || {};
        const home10 = components.homeRecent || {};
        const home20 = components.homeForm20 || {};
        const away5 = components.awayForm5 || {};
        const away10 = components.awayRecent || {};
        const away20 = components.awayForm20 || {};
        if (!Object.keys(dossier).length) return "";
        const formCell = (label, row) => `<div><span>${escapeHtml(label)}</span><strong>${formatNumber(row.gf, 2)} : ${formatNumber(row.ga, 2)}</strong><small>${formatNumber(number(row.wins) * 100, 0)}% побед · ТБ2,5 ${formatNumber(number(row.over25) * 100, 0)}%</small></div>`;
        return `
            <div class="dialog-section dossier-section">
                <h4>Полное досье матча</h4>
                <div class="dossier-score-grid">
                    <div><span>Ожидаемые голы хозяев</span><strong>${formatNumber(dossier.expectedHomeGoals, 2)}</strong></div>
                    <div><span>Ожидаемые голы гостей</span><strong>${formatNumber(dossier.expectedAwayGoals, 2)}</strong></div>
                    <div><span>Ожидаемый тотал</span><strong>${formatNumber(dossier.expectedTotalGoals, 2)}</strong></div>
                    <div><span>Elo</span><strong>${formatNumber(components.homeElo, 0)} : ${formatNumber(components.awayElo, 0)}</strong></div>
                </div>
                <div class="dossier-form-grid">
                    ${formCell("Хозяева · 5", home5)}${formCell("Хозяева · 10", home10)}${formCell("Хозяева · 20", home20)}
                    ${formCell("Гости · 5", away5)}${formCell("Гости · 10", away10)}${formCell("Гости · 20", away20)}
                </div>
                <p>${escapeHtml((dossier.sources || []).join(" · ") || "Источники статистики ещё не опубликованы")}</p>
            </div>`;
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
            window.setTimeout(() => toast.classList.remove("is-visible"), 1700);
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
        return ({ pending: "Ожидается", won: "Выигрыш", lost: "Проигрыш", push: "Возврат", void: "Отмена", cancelled: "Отмена", postponed: "Перенесён", unresolved: "Результат не подтверждён" })[String(status).toLowerCase()] || "Неизвестный статус";
    }

    function statusClass(status) {
        return ({ won: "is-won", lost: "is-lost", push: "is-push", unresolved: "is-unresolved" })[String(status).toLowerCase()] || "";
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

    function formatCount(value, one, few, many) {
        const count = Math.max(0, Math.trunc(number(value)));
        const mod100 = count % 100;
        const mod10 = count % 10;
        const word = mod100 >= 11 && mod100 <= 14
            ? many
            : mod10 === 1
              ? one
              : mod10 >= 2 && mod10 <= 4
                ? few
                : many;
        return `${count} ${word}`;
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
