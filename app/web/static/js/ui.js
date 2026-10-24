// Маленький DOM-конструктор и общие помощники. Строки всегда вставляются как текст (не HTML).

export const tg = window.Telegram?.WebApp;

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
}

export function button(label, onClick, cls = "btn") {
  return h("button", { class: cls, type: "button", onclick: onClick }, label);
}

export function card(...children) {
  return h("section", { class: "card" }, ...children);
}

export function spinner() {
  return h("div", { class: "spinner", role: "progressbar", "aria-label": "Загрузка" });
}

export function loadingCard(text) {
  return h("section", { class: "card empty" }, spinner(), text ? h("p", {}, text) : null);
}

export function errorCard(error, retry) {
  return h(
    "section",
    { class: "card empty" },
    h("div", { class: "emoji" }, "😕"),
    h("p", {}, error?.message || "Что-то пошло не так."),
    retry ? h("div", { style: { marginTop: "14px" } }, button("Повторить", retry, "btn secondary")) : null,
  );
}

let toastTimer;
export function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2600);
}

export function haptic(kind) {
  try {
    if (kind === "success" || kind === "error" || kind === "warning") {
      tg?.HapticFeedback?.notificationOccurred(kind);
    } else {
      tg?.HapticFeedback?.impactOccurred(kind || "light");
    }
  } catch {
    /* вне Telegram — без вибрации */
  }
}

export function closeApp() {
  if (tg?.close) tg.close();
}

export function plural(n, [one, few, many]) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function countLabel(n, forms) {
  return `${formatNumber(n)} ${plural(n, forms)}`;
}

const numberFormat = new Intl.NumberFormat("ru-RU");
export function formatNumber(n) {
  if (n === null || n === undefined) return "—";
  if (Math.abs(n) >= 10000) {
    return new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 }).format(n);
  }
  return numberFormat.format(n);
}

export function formatMoney(value) {
  if (value === null || value === undefined) return "—";
  const digits = value !== 0 && Math.abs(value) < 1 ? 3 : 2;
  return `$${value.toFixed(digits)}`;
}

export function formatDate(iso, withYear = false) {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "short",
    ...(withYear ? { year: "numeric" } : {}),
  });
}

export function formatDay(isoDate) {
  // "2026-10-24" → «24 окт» без сдвига часового пояса
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

export function relativeTime(iso) {
  if (!iso) return "никогда";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 2) return "только что";
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${countLabel(days, ["день", "дня", "дней"])} назад`;
  return formatDate(iso, true);
}

export function pageHead(title, ...extra) {
  return h("header", { class: "page-head" }, h("h1", {}, title), ...extra);
}
