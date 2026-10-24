// Вкладка «Админ»: аналитика, пользователи, премиум, блокировка. Видна только ADMIN_IDS.
import { api } from "../api.js";
import { barList, columnChart, statTile } from "../charts.js";
import {
  button,
  card,
  countLabel,
  errorCard,
  formatDate,
  formatMoney,
  formatNumber,
  h,
  haptic,
  loadingCard,
  mount,
  pageHead,
  relativeTime,
  tg,
  toast,
} from "../ui.js";

const KIND_LABELS = {
  llm_companion: "Собеседник",
  llm_analyst: "Аналитик",
  llm_summary: "Саммари",
  llm_lesson: "Генерация уроков",
  stt: "Распознавание речи",
  tts: "Озвучка ответов",
  tts_lesson: "Аудирование в уроках",
};

const MODES = { chat: "просто болтаем", correct: "с исправлениями", review: "разбор потом" };
const VOICE_MODES = { auto: "голосом на голосовые", always: "все ответы голосом", off: "по кнопке" };

function confirmAction(text) {
  return new Promise((resolve) => {
    if (tg?.showConfirm) {
      try {
        tg.showConfirm(text, (ok) => resolve(Boolean(ok)));
        return;
      } catch {
        /* старый клиент Telegram */
      }
    }
    resolve(window.confirm(text));
  });
}

function userBadges(user) {
  return h(
    "span",
    { class: "chips" },
    user.premium ? h("span", { class: "badge accent" }, "⭐ премиум") : null,
    user.blocked ? h("span", { class: "badge danger" }, "заблокирован") : null,
    !user.onboarded ? h("span", { class: "badge" }, "без онбординга") : null,
  );
}

function userRow(user, ctx, extra) {
  return h(
    "li",
    {
      class: "clickable",
      tabindex: 0,
      role: "button",
      onclick: () => ctx.navigate(`#/admin/user/${user.id}`),
      onkeydown: (event) => event.key === "Enter" && ctx.navigate(`#/admin/user/${user.id}`),
    },
    h(
      "div",
      { class: "row spread" },
      h("div", { style: { minWidth: "0" } }, h("div", { style: { fontWeight: "600" } }, user.name), h("div", { class: "small muted" }, user.username ? `@${user.username}` : `id ${user.id}`)),
      extra,
    ),
    h("div", { class: "row spread", style: { marginTop: "4px" } }, userBadges(user), h("span", { class: "tiny muted" }, relativeTime(user.last_active_at))),
  );
}

export async function renderAdmin(view, ctx, params = {}) {
  ctx.setTabsVisible(true);
  if (!ctx.me.is_admin) {
    mount(view, errorCard({ message: "Раздел только для админов." }));
    return;
  }
  if (params.userId) {
    await renderUser(view, ctx, params.userId);
    return;
  }
  mount(view, loadingCard());
  let data;
  try {
    data = await api.admin.overview(14);
  } catch (error) {
    mount(view, errorCard(error, () => renderAdmin(view, ctx)));
    return;
  }
  const u = data.users;
  const m = data.last_30d;
  const today = data.series[data.series.length - 1];
  const point = (key) => data.series.map((d) => ({ date: d.date, value: d[key] }));
  const sum = (key) => data.series.reduce((acc, d) => acc + d[key], 0);

  const parts = [
    pageHead("Админка"),
    h(
      "div",
      { class: "tiles" },
      statTile({ label: "Пользователи", value: formatNumber(u.total), delta: { text: `+${today.new_users} сегодня · онбординг ${u.onboarded}` } }),
      statTile({ label: "Активные сегодня", value: formatNumber(u.dau), delta: { text: `неделя ${u.wau} · месяц ${u.mau}` } }),
      statTile({ label: "Премиум", value: formatNumber(u.premium), delta: { text: `заблокировано: ${u.blocked}` } }),
      statTile({
        label: "Расход за 30 дней",
        value: formatMoney(m.cost_usd),
        delta: { text: m.cost_per_active_user === null ? "нет активных" : `${formatMoney(m.cost_per_active_user)} на активного` },
      }),
      statTile({ label: "Реплик за 30 дней", value: formatNumber(m.messages), delta: { text: `уроков: ${m.lessons}, пройдено ${m.lessons_done}` } }),
      statTile({ label: "Сегодня", value: formatMoney(today.cost_usd), delta: { text: `${countLabel(today.messages, ["реплика", "реплики", "реплик"])}` } }),
    ),
    h("h3", {}, "14 дней"),
    h(
      "div",
      { class: "stack" },
      columnChart({ title: "Активные пользователи", subtitle: `сегодня ${today.active_users}`, points: point("active_users") }),
      columnChart({ title: "Реплики", subtitle: `всего ${formatNumber(sum("messages"))}`, points: point("messages") }),
      columnChart({ title: "Расход", subtitle: `всего ${formatMoney(sum("cost_usd"))}`, points: point("cost_usd"), format: formatMoney }),
    ),
  ];

  if (data.cost_by_kind.length) {
    parts.push(
      h("h3", {}, "Куда уходят деньги"),
      barList({
        title: "Расход по статьям",
        subtitle: "за 30 дней",
        columns: ["Статья", "Расход"],
        format: formatMoney,
        items: data.cost_by_kind.map((c) => ({
          label: KIND_LABELS[c.kind] || c.kind,
          value: c.cost_usd,
          note: countLabel(c.count, ["запрос", "запроса", "запросов"]),
        })),
      }),
    );
  }
  if (data.top_users.length) {
    parts.push(
      h("h3", {}, "Самые дорогие пользователи · 30 дней"),
      h(
        "section",
        { class: "card flush" },
        h(
          "ul",
          { class: "list" },
          data.top_users.map((user) =>
            h(
              "li",
              { class: "clickable row spread", tabindex: 0, role: "button", onclick: () => ctx.navigate(`#/admin/user/${user.id}`) },
              h("span", {}, user.name, user.username ? h("span", { class: "muted small" }, ` @${user.username}`) : null),
              h("span", { class: "badge" }, formatMoney(user.cost_usd)),
            ),
          ),
        ),
      ),
    );
  }
  parts.push(h("h3", {}, "Пользователи"), usersSection(ctx));
  mount(view, ...parts);
}

function usersSection(ctx) {
  const input = h("input", { class: "search", type: "search", placeholder: "Имя, @username или id", "aria-label": "Поиск пользователей" });
  const list = h("ul", { class: "list" });
  const listCard = h("section", { class: "card flush", style: { marginTop: "10px" } }, list);
  const more = button("Показать ещё", () => load(false), "btn secondary");
  more.hidden = true;
  const summary = h("p", { class: "tiny muted", style: { margin: "8px 4px 0" } });
  let offset = 0;
  let timer;
  let requestId = 0;

  async function load(reset) {
    const id = ++requestId;
    if (reset) offset = 0;
    try {
      const data = await api.admin.users(input.value.trim(), offset);
      if (id !== requestId) return; // устаревший ответ на прошлый ввод
      if (reset) list.replaceChildren();
      for (const user of data.items) {
        list.append(userRow(user, ctx, h("span", { class: "small muted" }, `${user.messages_30d} · ${formatMoney(user.cost_30d)}`)));
      }
      offset += data.items.length;
      summary.textContent = `Показано ${offset} из ${data.total} · реплики и расход за 30 дней`;
      more.hidden = offset >= data.total;
      if (!data.total) list.replaceChildren(h("li", { class: "muted" }, "Никого не нашли"));
    } catch (error) {
      toast(error.message);
    }
  }
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(true), 300);
  });
  load(true);
  return h("div", {}, input, listCard, summary, h("div", { style: { marginTop: "10px" } }, more));
}

async function renderUser(view, ctx, userId) {
  ctx.setBack(() => {
    ctx.setBack(null);
    ctx.navigate("#/admin");
  });
  mount(view, loadingCard());
  let user;
  try {
    user = await api.admin.user(userId);
  } catch (error) {
    mount(view, errorCard(error, () => renderUser(view, ctx, userId)));
    return;
  }
  drawUser(view, ctx, user);
}

function drawUser(view, ctx, user) {
  const refresh = (updated, message) => {
    haptic("success");
    toast(message);
    drawUser(view, ctx, updated);
  };
  const act = async (fn, message, buttons) => {
    buttons.forEach((b) => (b.disabled = true));
    try {
      refresh(await fn(), message);
    } catch (error) {
      buttons.forEach((b) => (b.disabled = false));
      toast(error.message);
    }
  };

  const premiumButtons = [];
  const grant = (days, label) => {
    const b = button(label, () => act(() => api.admin.premium(user.id, days), `Премиум продлён на ${countLabel(days, ["день", "дня", "дней"])}`, premiumButtons), "btn secondary small");
    premiumButtons.push(b);
    return b;
  };
  const revoke = button("Снять", async () => {
    if (await confirmAction(`Снять премиум у ${user.name}?`)) {
      act(() => api.admin.premium(user.id, 0), "Премиум снят", premiumButtons);
    }
  }, "btn danger small");
  premiumButtons.push(revoke);

  const blockBtn = button(user.blocked ? "Разблокировать" : "Заблокировать", async () => {
    const text = user.blocked
      ? `Разблокировать ${user.name}?`
      : `Заблокировать ${user.name}? Бот и приложение перестанут ему отвечать.`;
    if (await confirmAction(text)) {
      act(() => api.admin.block(user.id, !user.blocked), user.blocked ? "Разблокирован" : "Заблокирован", [blockBtn]);
    }
  }, user.blocked ? "btn secondary" : "btn danger");

  const p = user.progress;
  const parts = [
    pageHead(user.name, userBadges(user)),
    card(
      h(
        "dl",
        { class: "kv" },
        h("dt", {}, "Telegram"), h("dd", {}, user.username ? `@${user.username}` : "—"),
        h("dt", {}, "ID"), h("dd", {}, String(user.id)),
        h("dt", {}, "Уровень"), h("dd", {}, user.estimated_level ? `${user.level} → ≈ ${user.estimated_level}` : user.level),
        h("dt", {}, "Режим"), h("dd", {}, MODES[user.mode] || user.mode),
        h("dt", {}, "Озвучка"), h("dd", {}, VOICE_MODES[user.voice_mode] || user.voice_mode),
        h("dt", {}, "Собеседник"), h("dd", {}, user.persona === "uk_casual" ? "Emma 🇬🇧" : "Jake 🇺🇸"),
        h("dt", {}, "С нами с"), h("dd", {}, formatDate(user.created_at, true)),
        h("dt", {}, "Был в сети"), h("dd", {}, relativeTime(user.last_active_at)),
        h("dt", {}, "Реплик в день"), h("dd", {}, String(user.limits.messages)),
        h("dt", {}, "Озвучек в день"), h("dd", {}, String(user.limits.tts)),
        h("dt", {}, "Уроков в день"), h("dd", {}, String(user.limits.lessons)),
      ),
    ),
    h("h3", {}, "Премиум"),
    card(
      h("p", {}, user.premium ? `Активен до ${formatDate(user.premium_until, true)}` : "Нет премиума"),
      h("div", { class: "row wrap", style: { marginTop: "12px" } }, grant(7, "+7 дней"), grant(30, "+30 дней"), grant(365, "+1 год"), user.premium ? revoke : null),
      h("p", { class: "tiny muted", style: { marginTop: "10px" } }, "Пользователь получит сообщение от бота о продлении."),
    ),
    h("h3", {}, "Учёба"),
    h(
      "div",
      { class: "tiles" },
      statTile({ label: "Реплик всего", value: formatNumber(p.totals.messages), delta: { text: `голосом ${String(p.totals.voice_minutes).replace(".", ",")} мин` } }),
      statTile({ label: "Дней подряд", value: String(p.activity.streak), delta: { text: `за 30 дней: ${p.activity.active_days_30}` } }),
      statTile({ label: "Ошибок на 10 реплик", value: p.mistake_rate.week === null ? "—" : String(p.mistake_rate.week).replace(".", ","), delta: { text: p.mistake_rate.prev_week === null ? "нет данных за прошлую неделю" : `прошлая неделя: ${String(p.mistake_rate.prev_week).replace(".", ",")}` } }),
      statTile({ label: "Слова", value: formatNumber(p.totals.words), delta: { text: `выучено ${p.totals.words_known} · уроков ${p.totals.lessons_done}` } }),
    ),
    h("h3", {}, "Расходы"),
    h(
      "div",
      { class: "tiles" },
      statTile({ label: "За 30 дней", value: formatMoney(user.cost_30d) }),
      statTile({ label: "За всё время", value: formatMoney(user.cost_total) }),
    ),
  ];
  if (user.cost_by_kind.length) {
    parts.push(
      h("div", { style: { marginTop: "12px" } },
        barList({
          title: "По статьям",
          subtitle: "за всё время",
          columns: ["Статья", "Расход"],
          format: formatMoney,
          items: user.cost_by_kind.map((c) => ({ label: KIND_LABELS[c.kind] || c.kind, value: c.cost_usd, note: `за 30 дней ${formatMoney(c.cost_30d)}` })),
        }),
      ),
    );
  }
  if (user.mistakes.length) {
    parts.push(
      h("h3", {}, "Последние ошибки"),
      h("section", { class: "card flush" }, h("ul", { class: "list" }, user.mistakes.map((mistake) =>
        h("li", { class: "mistake-pair" },
          h("div", { class: "wrong-text" }, mistake.original),
          h("div", { class: "row spread" }, h("span", { class: "right-text" }, mistake.corrected), h("span", { class: "tiny muted" }, formatDate(mistake.created_at))),
        ),
      ))),
    );
  }
  if (user.lessons.length) {
    parts.push(
      h("h3", {}, "Уроки"),
      h("section", { class: "card flush" }, h("ul", { class: "list" }, user.lessons.map((lesson) =>
        h("li", { class: "row spread" },
          h("div", {}, h("div", {}, lesson.title || "Урок"), h("div", { class: "tiny muted" }, formatDate(lesson.created_at))),
          h("span", { class: "badge" }, lesson.status === "done" ? `${lesson.score}/${lesson.total}` : "не завершён"),
        ),
      ))),
    );
  }
  parts.push(h("h3", {}, "Доступ"), card(blockBtn));
  mount(view, ...parts);
}
