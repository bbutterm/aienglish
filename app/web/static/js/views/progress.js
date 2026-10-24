// Вкладка «Прогресс»: серия, темы разговоров, ошибки, слова, сильные и слабые стороны.
import { api } from "../api.js";
import { activityCalendar, barList, statTile } from "../charts.js";
import { card, countLabel, errorCard, formatDate, formatNumber, h, loadingCard, mount, pageHead } from "../ui.js";

const decimal = (value) => String(value).replace(".", ",");

function rateDelta(rate) {
  if (rate.week === null) return { text: "мало реплик за неделю" };
  if (rate.prev_week === null) return { text: "первая неделя — сравним позже" };
  const diff = Math.round((rate.week - rate.prev_week) * 10) / 10;
  if (diff === 0) return { text: "как на прошлой неделе" };
  // меньше ошибок — хорошо
  return {
    text: `${diff < 0 ? "↓" : "↑"} ${decimal(Math.abs(diff))} к прошлой неделе`,
    tone: diff < 0 ? "good" : "bad",
  };
}

function insightsCard(insights) {
  if (!insights) {
    return card(
      h("p", {}, "Пройди урок дня — по нему я разберу твои сильные и слабые стороны."),
    );
  }
  const list = (items, cls, icon) =>
    items.length
      ? h("ul", { class: `points ${cls}` }, items.map((text) => h("li", {}, h("span", { class: "icon", "aria-hidden": "true" }, icon), h("span", {}, text))))
      : null;
  return card(
    h("div", { class: "stack" },
      insights.strengths.length ? h("div", {}, h("div", { class: "small muted" }, "Получается"), list(insights.strengths, "good", "✓")) : null,
      insights.weaknesses.length ? h("div", {}, h("div", { class: "small muted" }, "Стоит подтянуть"), list(insights.weaknesses, "bad", "!")) : null,
      insights.tip_ru ? h("p", {}, `💡 ${insights.tip_ru}`) : null,
      h("p", { class: "tiny muted" }, `По уроку от ${formatDate(insights.date)}`),
    ),
  );
}

function topicsBlock(topics) {
  if (!topics.length) return null;
  return [
    h("h3", {}, "О чём говорили"),
    barList({
      title: "Темы разговоров",
      subtitle: "реплик за 30 дней",
      columns: ["Тема", "Реплик"],
      items: topics.map((t) => ({
        label: t.topic[0].toUpperCase() + t.topic.slice(1),
        value: t.messages,
        note: `ошибок: ${t.mistakes}`,
      })),
    }),
  ];
}

export async function renderProgress(view, ctx) {
  ctx.setTabsVisible(true);
  mount(view, loadingCard());
  let p;
  try {
    p = await api.progress();
  } catch (error) {
    mount(view, errorCard(error, () => renderProgress(view, ctx)));
    return;
  }
  const t = p.totals;
  const level = p.level.estimated && p.level.estimated !== p.level.self
    ? h("span", { class: "badge" }, `${p.level.self} → по речи ≈ ${p.level.estimated}`)
    : h("span", { class: "badge" }, p.level.self);

  const parts = [
    pageHead("Прогресс", level),
    h(
      "div",
      { class: "tiles" },
      statTile({
        label: "Дней подряд",
        value: `${p.activity.streak}${p.activity.streak >= 3 ? " 🔥" : ""}`,
        delta: { text: `за 30 дней: ${countLabel(p.activity.active_days_30, ["день", "дня", "дней"])}` },
      }),
      statTile({
        label: "Ошибок на 10 реплик",
        value: p.mistake_rate.week === null ? "—" : decimal(p.mistake_rate.week),
        delta: rateDelta(p.mistake_rate),
      }),
      statTile({
        label: "Слов выучено",
        value: formatNumber(t.words_known),
        delta: { text: `в работе: ${t.words_learning + t.words_new}` },
      }),
      statTile({
        label: "Уроков пройдено",
        value: formatNumber(t.lessons_done),
        delta: { text: `голосом: ${decimal(t.voice_minutes)} мин` },
      }),
    ),
    topicsBlock(p.topics),
    h("h3", {}, "Сильные и слабые стороны"),
    insightsCard(p.insights),
    h("h3", {}, "Активность"),
    activityCalendar({ title: "Последние 4 недели", subtitle: `${formatNumber(t.messages)} реплик всего`, activeDays: p.activity.days }),
  ];

  if (p.mistakes_by_type.length) {
    parts.push(
      h("h3", {}, "Ошибки"),
      barList({
        title: "По типам",
        subtitle: "за 30 дней",
        columns: ["Тип", "Ошибок"],
        items: p.mistakes_by_type.map((m) => ({
          label: m.label_ru,
          value: m.count,
          note: `месяцем раньше: ${m.prev}`,
        })),
      }),
    );
  }
  if (p.recurring.length) {
    parts.push(
      h("h3", {}, "Повторяются чаще всего"),
      h(
        "section",
        { class: "card flush" },
        h(
          "ul",
          { class: "list" },
          p.recurring.map((m) =>
            h(
              "li",
              { class: "mistake-pair" },
              h("div", { class: "wrong-text" }, m.original),
              h("div", { class: "row spread" }, h("span", { class: "right-text" }, m.corrected), h("span", { class: "badge" }, `×${m.times_seen}`)),
            ),
          ),
        ),
      ),
    );
  }
  mount(view, ...parts);
}
