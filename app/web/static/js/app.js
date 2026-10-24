// Точка входа Mini App: тема Telegram, роутинг по hash, нижние вкладки.
import { api } from "./api.js";
import { hideTooltip } from "./charts.js";
import { errorCard, h, loadingCard, mount, tg } from "./ui.js";
import { renderAdmin } from "./views/admin.js";
import { renderLessonTab } from "./views/lesson.js";
import { renderProgress } from "./views/progress.js";
import { renderWords } from "./views/words.js";

const view = document.getElementById("view");
const tabs = document.getElementById("tabs");

const TABS = [
  { route: "#/", icon: "📚", label: "Урок" },
  { route: "#/progress", icon: "📈", label: "Прогресс" },
  { route: "#/words", icon: "🗂", label: "Слова" },
  { route: "#/admin", icon: "🛠", label: "Админ", admin: true },
];

let backHandler = null;

const ctx = {
  me: null,
  navigate(route) {
    if (location.hash === route) route_();
    else location.hash = route;
  },
  setBack(handler) {
    const button = tg?.BackButton;
    if (backHandler && button) button.offClick(backHandler);
    backHandler = handler;
    if (!button) return;
    if (handler) {
      button.onClick(handler);
      button.show();
    } else {
      button.hide();
    }
  },
  setTabsVisible(visible) {
    tabs.hidden = !visible;
  },
};

function applyTheme() {
  const scheme = tg?.colorScheme;
  if (scheme) document.documentElement.dataset.theme = scheme;
  try {
    tg?.setHeaderColor?.("secondary_bg_color");
    tg?.setBackgroundColor?.("secondary_bg_color");
  } catch {
    /* старые клиенты Telegram */
  }
}

function parseRoute() {
  const hash = location.hash;
  const user = hash.match(/^#\/admin\/user\/(\d+)$/);
  if (user) return { name: "admin", params: { userId: Number(user[1]) } };
  if (hash === "#/progress") return { name: "progress" };
  if (hash === "#/words") return { name: "words" };
  if (hash === "#/admin") return { name: "admin" };
  return { name: "lesson" }; // в т.ч. служебный hash от Telegram при запуске
}

function renderTabs(active) {
  const items = TABS.filter((tab) => !tab.admin || ctx.me?.is_admin);
  tabs.replaceChildren(
    ...items.map((tab) =>
      h(
        "a",
        {
          href: tab.route,
          "aria-current": tab.route === active ? "page" : null,
        },
        h("span", { class: "icon", "aria-hidden": "true" }, tab.icon),
        tab.label,
      ),
    ),
  );
}

async function route_() {
  hideTooltip();
  ctx.setBack(null);
  const { name, params } = parseRoute();
  renderTabs({ lesson: "#/", progress: "#/progress", words: "#/words", admin: "#/admin" }[name]);
  ctx.setTabsVisible(true);
  window.scrollTo({ top: 0 });
  try {
    if (name === "progress") await renderProgress(view, ctx);
    else if (name === "words") await renderWords(view, ctx);
    else if (name === "admin") await renderAdmin(view, ctx, params);
    else await renderLessonTab(view, ctx);
  } catch (error) {
    mount(view, errorCard(error, route_));
  }
}

async function start() {
  tg?.ready();
  tg?.expand();
  applyTheme();
  tg?.onEvent?.("themeChanged", applyTheme);

  if (!tg?.initData) {
    mount(
      view,
      h(
        "section",
        { class: "card empty" },
        h("div", { class: "emoji" }, "📱"),
        h("h2", { style: { marginTop: "8px" } }, "Открой приложение в Telegram"),
        h("p", {}, "Оно работает внутри чата с ботом: кнопка «Уроки» внизу или команда /app."),
      ),
    );
    return;
  }

  mount(view, loadingCard());
  try {
    ctx.me = await api.me();
  } catch (error) {
    mount(view, errorCard(error, start));
    return;
  }
  // Бот открывает нужный раздел через ?page=admin (hash занят параметрами запуска Telegram).
  const page = new URLSearchParams(location.search).get("page");
  if (page && !location.hash.startsWith("#/")) {
    history.replaceState(null, "", `${location.pathname}${location.search}#/${page}`);
  }
  window.addEventListener("hashchange", route_);
  await route_();
}

start();
