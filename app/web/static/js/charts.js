// Графики по правилам data-viz: одна серия — один цвет (slot 1), тонкие метки, подсказка
// при наведении/фокусе/тапе и табличный вид у каждого графика.
import { formatDay, formatNumber, h } from "./ui.js";

let tooltipEl;

function tooltip() {
  if (!tooltipEl) {
    tooltipEl = h("div", { class: "tooltip", role: "tooltip", hidden: true });
    document.body.append(tooltipEl);
  }
  return tooltipEl;
}

function showTooltip(target, value, label) {
  const el = tooltip();
  el.replaceChildren(h("strong", {}, value), label);
  el.hidden = false;
  const rect = target.getBoundingClientRect();
  const tip = el.getBoundingClientRect();
  const left = Math.min(
    Math.max(8, rect.left + rect.width / 2 - tip.width / 2),
    window.innerWidth - tip.width - 8,
  );
  const top = rect.top - tip.height - 8 > 0 ? rect.top - tip.height - 8 : rect.bottom + 8;
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
}

export function hideTooltip() {
  if (tooltipEl) tooltipEl.hidden = true;
  document.querySelectorAll(".col.active").forEach((el) => el.classList.remove("active"));
}

window.addEventListener("scroll", hideTooltip, { passive: true });

function withTooltip(el, value, label) {
  el.tabIndex = 0;
  el.setAttribute("aria-label", `${label}: ${value}`);
  const show = () => {
    hideTooltip();
    el.classList.add("active");
    showTooltip(el, value, label);
  };
  el.addEventListener("pointerenter", show);
  el.addEventListener("pointerdown", show);
  el.addEventListener("focus", show);
  el.addEventListener("pointerleave", hideTooltip);
  el.addEventListener("blur", hideTooltip);
  return el;
}

function chartCard({ title, subtitle, plot, table }) {
  let showTable = false;
  const body = h("div", {}, plot);
  const toggle = h(
    "button",
    { class: "link-btn small", type: "button", "aria-pressed": "false" },
    "Таблица",
  );
  toggle.addEventListener("click", () => {
    showTable = !showTable;
    hideTooltip();
    toggle.textContent = showTable ? "График" : "Таблица";
    toggle.setAttribute("aria-pressed", String(showTable));
    body.replaceChildren(showTable ? table() : plot);
  });
  return h(
    "figure",
    { class: "card chart-card", style: { margin: "0" } },
    h(
      "figcaption",
      { class: "chart-head" },
      h("div", {}, h("div", { class: "chart-title" }, title), subtitle ? h("div", { class: "chart-sub" }, subtitle) : null),
      toggle,
    ),
    body,
  );
}

function table(headers, rows) {
  return h(
    "table",
    { class: "chart-table" },
    h("thead", {}, h("tr", {}, headers.map((text, i) => h("th", { class: i ? "num" : "" }, text)))),
    h("tbody", {}, rows.map((row) => h("tr", {}, row.map((cell, i) => h("td", { class: i ? "num" : "" }, cell))))),
  );
}

/** Столбики по дням: points = [{date: "2026-10-24", value: 3}]. */
export function columnChart({ title, subtitle, points, format = formatNumber, unit = "" }) {
  const max = Math.max(0, ...points.map((p) => p.value));
  const plot = h(
    "div",
    { class: "chart-plot" },
    h(
      "div",
      { class: "columns", role: "list" },
      max > 0 ? h("span", { class: "axis-max" }, format(max)) : null,
      points.map((point) => {
        const height = max > 0 ? (point.value / max) * 100 : 0;
        const col = h(
          "div",
          { class: "col", role: "listitem" },
          h("div", { class: "fill", style: { height: `${height}%` } }),
        );
        return withTooltip(col, `${format(point.value)}${unit}`, formatDay(point.date));
      }),
    ),
    h(
      "div",
      { class: "axis-row" },
      h("span", {}, formatDay(points[0].date)),
      h("span", {}, formatDay(points[points.length - 1].date)),
    ),
  );
  return chartCard({
    title,
    subtitle,
    plot,
    table: () =>
      table(
        ["День", title],
        [...points].reverse().map((p) => [formatDay(p.date), `${format(p.value)}${unit}`]),
      ),
  });
}

/** Горизонтальные бары: items = [{label, value, note?}] — значение у конца бара. */
export function barList({ title, subtitle, items, format = formatNumber, columns = ["", ""] }) {
  const max = Math.max(0, ...items.map((i) => i.value));
  const plot = h(
    "div",
    { class: "bars" },
    items.map((item) => {
      // Длина считается от области без подписи (64px под значение у конца бара).
      const ratio = max > 0 ? item.value / max : 0;
      const track = h(
        "div",
        { class: "bar-track" },
        h("div", { class: "fill", style: { width: `calc((100% - 64px) * ${ratio.toFixed(4)})` } }),
        h("span", { class: "value" }, format(item.value)),
      );
      const row = h("div", { class: "bar-row" }, h("span", { class: "bar-label" }, item.label), track);
      return item.note ? withTooltip(row, format(item.value), `${item.label} · ${item.note}`) : row;
    }),
  );
  return chartCard({
    title,
    subtitle,
    plot,
    table: () =>
      table(
        [columns[0] || title, columns[1] || "Значение"],
        items.map((i) => [i.note ? `${i.label} (${i.note})` : i.label, format(i.value)]),
      ),
  });
}

/** Календарь активности за 4 недели; последняя клетка — сегодня. */
export function activityCalendar({ title, subtitle, activeDays, today = new Date() }) {
  const active = new Set(activeDays);
  const days = [];
  for (let i = 27; i >= 0; i -= 1) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    days.push({ iso, on: active.has(iso), today: i === 0 });
  }
  const plot = h(
    "div",
    {},
    h(
      "div",
      { class: "calendar", role: "list" },
      days.map((day) =>
        withTooltip(
          h("div", { class: `day${day.on ? " on" : ""}${day.today ? " today" : ""}`, role: "listitem" }),
          day.on ? "Занимался" : "Пропуск",
          formatDay(day.iso),
        ),
      ),
    ),
    h(
      "div",
      { class: "calendar-legend" },
      h("span", { class: "swatch" }),
      "занимался",
      h("span", { class: "swatch off", style: { marginLeft: "10px" } }),
      "нет",
    ),
  );
  return chartCard({
    title,
    subtitle,
    plot,
    table: () =>
      table(
        ["День", "Активность"],
        [...days].reverse().map((d) => [formatDay(d.iso), d.on ? "да" : "—"]),
      ),
  });
}

/** KPI-плитка: подпись, значение, необязательная дельта {text, tone: good|bad|neutral}. */
export function statTile({ label, value, delta }) {
  return h(
    "div",
    { class: "tile" },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, value),
    delta ? h("div", { class: `delta ${delta.tone || ""}` }, delta.text) : null,
  );
}
