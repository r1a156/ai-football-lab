"use strict";

const stateUrl = `data/state.json?v=${Date.now()}`;

let applicationState = null;
let currentHistoryFilter = "all";

document.addEventListener("DOMContentLoaded", initializeApplication);

async function initializeApplication() {
    try {
        const response = await fetch(stateUrl, {
            cache: "no-store"
        });

        if (!response.ok) {
            throw new Error(`Ошибка загрузки данных: ${response.status}`);
        }

        applicationState = await response.json();

        renderApplication(applicationState);
        initializeFilters();
        initializeScrollEffects();
    } catch (error) {
        console.error(error);

        document.getElementById("predictionsGrid").innerHTML = `
            <div class="loading-card">
                <p>
                    Не удалось загрузить данные. Повторите попытку позднее.
                </p>
            </div>
        `;
    }
}

function renderApplication(state) {
    renderMeta(state);
    renderPredictions(state.predictions || []);
    renderBank(state.bank || {});
    renderStatistics(state.statistics || {}, state.history || []);
    renderHistory(state.history || [], currentHistoryFilter);
}

function renderMeta(state) {
    const updated = state.meta?.updatedAt
        ? formatDateTime(state.meta.updatedAt)
        : "Нет данных";

    setText("lastUpdated", updated);
    setText("appVersion", state.meta?.version || "1.0.0");
    setText("radarCount", state.meta?.analyzedMatches || 0);
}

function renderPredictions(predictions) {
    const container = document.getElementById("predictionsGrid");

    if (!predictions.length) {
        container.innerHTML = `
            <div class="loading-card">
                <p>
                    Сегодня система не нашла событий с достаточным уровнем
                    уверенности.
                </p>
            </div>
        `;

        return;
    }

    container.innerHTML = predictions.map((prediction) => {
        const riskClass = prediction.risk === "Средний"
            ? "risk-medium"
            : "risk-low";

        return `
            <article class="prediction-card">
                <div class="prediction-top">
                    <div class="league-info">
                        <strong>${escapeHtml(prediction.league)}</strong>
                        <span>${escapeHtml(prediction.country)}</span>
                    </div>

                    <span class="risk-badge ${riskClass}">
                        ${escapeHtml(prediction.risk)}
                    </span>
                </div>

                <div class="match-time">
                    ${formatMatchDate(prediction.date, prediction.time)}
                </div>

                <div class="teams">
                    <strong>${escapeHtml(prediction.home)}</strong>
                    <span>—</span>
                    <strong>${escapeHtml(prediction.away)}</strong>
                </div>

                <div class="pick-box">
                    <div class="pick-copy">
                        <span>Прогноз</span>
                        <strong>${escapeHtml(prediction.pick)}</strong>
                    </div>

                    <div class="odds-box">
                        <span>Коэффициент</span>
                        <strong>${formatNumber(prediction.odds, 2)}</strong>
                    </div>
                </div>

                <div class="confidence-row">
                    <div class="confidence-label">
                        <span>Уверенность</span>
                        <strong>${prediction.confidence}%</strong>
                    </div>

                    <div class="confidence-track">
                        <span style="width: ${clamp(prediction.confidence, 0, 100)}%"></span>
                    </div>
                </div>

                <p class="reason">
                    ${escapeHtml(prediction.reason)}
                </p>
            </article>
        `;
    }).join("");
}

function renderBank(bank) {
    const current = Number(bank.current || 0);
    const starting = Number(bank.starting || 0);
    const difference = current - starting;
    const differencePercent = starting
        ? (difference / starting) * 100
        : 0;

    setText("currentBank", formatCurrency(current));
    setText("startingBank", formatCurrency(starting));
    setText("stakePercent", `${bank.stakePercent || 20}%`);
    setText("roiValue", formatSignedPercent(bank.roi || 0));
    setText("drawdownValue", `${formatNumber(bank.maxDrawdown || 0, 1)}%`);

    const changeElement = document.getElementById("bankChange");

    changeElement.textContent = `${difference >= 0 ? "+" : ""}${formatCurrency(difference)} · ${formatSignedPercent(differencePercent)}`;
    changeElement.classList.toggle("negative", difference < 0);

    const history = Array.isArray(bank.history)
        ? bank.history
        : [];

    setText(
        "chartPeriod",
        history.length
            ? `${history.length} контрольных точек`
            : "Нет данных"
    );

    drawBankChart(history);
}

function renderStatistics(statistics, history) {
    const won = history.filter((item) => item.status === "won").length;
    const lost = history.filter((item) => item.status === "lost").length;
    const pending = history.filter((item) => item.status === "pending").length;
    const completed = won + lost;

    const accuracy = completed
        ? (won / completed) * 100
        : 0;

    setText("accuracyValue", `${formatNumber(accuracy, 1)}%`);
    setText("wonCount", won);
    setText("lostCount", lost);
    setText("pendingCount", pending);
    setText("totalPredictions", history.length);

    setText(
        "averageOdds",
        formatNumber(
            statistics.averageOdds || calculateAverageOdds(history),
            2
        )
    );

    setText("currentStreak", statistics.currentStreak || "—");
    setText("bestSegment", statistics.bestSegment || "Недостаточно данных");

    const ring = document.getElementById("accuracyRing");
    const degrees = clamp(accuracy, 0, 100) * 3.6;

    ring.style.background = `
        conic-gradient(
            var(--accent) 0deg,
            var(--accent) ${degrees}deg,
            rgba(255, 255, 255, 0.055) ${degrees}deg
        )
    `;
}

function renderHistory(history, filter) {
    const body = document.getElementById("historyBody");

    const filtered = filter === "all"
        ? history
        : history.filter((item) => item.status === filter);

    if (!filtered.length) {
        body.innerHTML = `
            <tr>
                <td colspan="6" class="table-loading">
                    В выбранной категории пока нет прогнозов.
                </td>
            </tr>
        `;

        return;
    }

    body.innerHTML = filtered.map((item) => {
        const status = getStatusDisplay(item.status);

        return `
            <tr>
                <td>${formatShortDate(item.date)}</td>

                <td>
                    <div class="match-cell">
                        <strong>
                            ${escapeHtml(item.home)} — ${escapeHtml(item.away)}
                        </strong>
                        <span>${escapeHtml(item.league)}</span>
                    </div>
                </td>

                <td>${escapeHtml(item.pick)}</td>
                <td>${formatNumber(item.odds, 2)}</td>
                <td>${escapeHtml(item.score || "—")}</td>

                <td>
                    <span class="status-badge ${status.className}">
                        ${status.label}
                    </span>
                </td>
            </tr>
        `;
    }).join("");
}

function initializeFilters() {
    const buttons = document.querySelectorAll(".filter-button");

    buttons.forEach((button) => {
        button.addEventListener("click", () => {
            currentHistoryFilter = button.dataset.filter || "all";

            buttons.forEach((item) => {
                item.classList.toggle(
                    "active",
                    item === button
                );
            });

            renderHistory(
                applicationState?.history || [],
                currentHistoryFilter
            );
        });
    });
}

function initializeScrollEffects() {
    const elements = document.querySelectorAll(
        ".prediction-card, .stat-card, .metric-card, .method-grid article"
    );

    if (!("IntersectionObserver" in window)) {
        return;
    }

    const observer = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) {
                    return;
                }

                entry.target.animate(
                    [
                        {
                            opacity: 0,
                            transform: "translateY(18px)"
                        },
                        {
                            opacity: 1,
                            transform: "translateY(0)"
                        }
                    ],
                    {
                        duration: 550,
                        easing: "cubic-bezier(.2,.8,.2,1)",
                        fill: "both"
                    }
                );

                observer.unobserve(entry.target);
            });
        },
        {
            threshold: 0.08
        }
    );

    elements.forEach((element) => observer.observe(element));
}

function drawBankChart(history) {
    const canvas = document.getElementById("bankChart");
    const context = canvas.getContext("2d");

    const ratio = window.devicePixelRatio || 1;
    const rectangle = canvas.getBoundingClientRect();

    canvas.width = Math.max(1, Math.round(rectangle.width * ratio));
    canvas.height = Math.max(1, Math.round(rectangle.height * ratio));

    context.setTransform(ratio, 0, 0, ratio, 0, 0);

    const width = rectangle.width;
    const height = rectangle.height;

    context.clearRect(0, 0, width, height);

    if (!history.length) {
        return;
    }

    const values = history.map((item) => Number(item.value));
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    const range = maximum - minimum || 1;

    const padding = {
        top: 22,
        right: 12,
        bottom: 28,
        left: 10
    };

    const drawableWidth = width - padding.left - padding.right;
    const drawableHeight = height - padding.top - padding.bottom;

    context.lineWidth = 1;
    context.strokeStyle = "rgba(143, 255, 201, 0.075)";

    for (let index = 0; index <= 4; index += 1) {
        const y = padding.top + (drawableHeight / 4) * index;

        context.beginPath();
        context.moveTo(padding.left, y);
        context.lineTo(width - padding.right, y);
        context.stroke();
    }

    const points = values.map((value, index) => {
        const x = padding.left +
            (index / Math.max(values.length - 1, 1)) * drawableWidth;

        const y = padding.top +
            (1 - (value - minimum) / range) * drawableHeight;

        return {
            x,
            y
        };
    });

    const gradient = context.createLinearGradient(0, padding.top, 0, height);

    gradient.addColorStop(0, "rgba(83, 243, 166, 0.24)");
    gradient.addColorStop(1, "rgba(83, 243, 166, 0)");

    context.beginPath();
    context.moveTo(points[0].x, height - padding.bottom);

    points.forEach((point) => {
        context.lineTo(point.x, point.y);
    });

    context.lineTo(points[points.length - 1].x, height - padding.bottom);
    context.closePath();

    context.fillStyle = gradient;
    context.fill();

    context.beginPath();

    points.forEach((point, index) => {
        if (index === 0) {
            context.moveTo(point.x, point.y);
        } else {
            context.lineTo(point.x, point.y);
        }
    });

    context.lineWidth = 2.5;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.strokeStyle = "#64f6ad";
    context.shadowColor = "rgba(83, 243, 166, 0.5)";
    context.shadowBlur = 13;
    context.stroke();
    context.shadowBlur = 0;

    points.forEach((point, index) => {
        if (
            index !== 0 &&
            index !== points.length - 1 &&
            index % 2 !== 0
        ) {
            return;
        }

        context.beginPath();
        context.arc(point.x, point.y, 3.3, 0, Math.PI * 2);
        context.fillStyle = "#9affc9";
        context.fill();
    });
}

function calculateAverageOdds(history) {
    if (!history.length) {
        return 0;
    }

    const values = history
        .map((item) => Number(item.odds))
        .filter(Number.isFinite);

    if (!values.length) {
        return 0;
    }

    return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function getStatusDisplay(status) {
    const map = {
        won: {
            label: "Успешный",
            className: "status-won"
        },
        lost: {
            label: "Неуспешный",
            className: "status-lost"
        },
        pending: {
            label: "Ожидается",
            className: "status-pending"
        }
    };

    return map[status] || map.pending;
}

function setText(id, value) {
    const element = document.getElementById(id);

    if (element) {
        element.textContent = value;
    }
}

function formatCurrency(value) {
    return new Intl.NumberFormat("ru-RU", {
        maximumFractionDigits: 0
    }).format(Number(value || 0)) + " ед.";
}

function formatNumber(value, fractionDigits = 0) {
    return new Intl.NumberFormat("ru-RU", {
        minimumFractionDigits: fractionDigits,
        maximumFractionDigits: fractionDigits
    }).format(Number(value || 0));
}

function formatSignedPercent(value) {
    const number = Number(value || 0);

    return `${number >= 0 ? "+" : ""}${formatNumber(number, 1)}%`;
}

function formatDateTime(value) {
    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return value;
    }

    return new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "long",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit"
    }).format(date);
}

function formatShortDate(value) {
    const date = new Date(`${value}T12:00:00`);

    if (Number.isNaN(date.getTime())) {
        return value;
    }

    return new Intl.DateTimeFormat("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric"
    }).format(date);
}

function formatMatchDate(dateValue, timeValue) {
    const date = new Date(`${dateValue}T${timeValue || "00:00"}:00`);

    if (Number.isNaN(date.getTime())) {
        return `${dateValue} · ${timeValue || ""}`;
    }

    return new Intl.DateTimeFormat("ru-RU", {
        weekday: "long",
        day: "2-digit",
        month: "long",
        hour: "2-digit",
        minute: "2-digit"
    }).format(date);
}

function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, Number(value || 0)));
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

window.addEventListener("resize", () => {
    if (applicationState?.bank?.history) {
        drawBankChart(applicationState.bank.history);
    }
});
