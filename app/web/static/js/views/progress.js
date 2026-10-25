// Вкладка «Прогресс»: серия, темы разговоров, ошибки, слова, сильные и слабые стороны.
import { api } from "../api.js";
import { activityCalendar, barList, statTile } from "../charts.js";
import { icon } from "../icons.js";
import {
  button,
  card,
  countLabel,
  errorCard,
  formatDate,
  formatNumber,
  h,
  haptic,
  loadingCard,
  mount,
  pageHead,
  tg,
  toast,
} from "../ui.js";

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
      insights.tip_ru ? h("div", { class: "callout" }, h("strong", {}, "Совет"), h("span", {}, insights.tip_ru)) : null,
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
        value: String(p.activity.streak),
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
  if (ctx.me.features?.referrals) parts.push(inviteCard());
  mount(view, ...parts);
}

// «Пригласи друга»: ссылка с ref_<id>, обоим неделя премиума, когда друг освоится.
function inviteCard() {
  const box = h("section", { class: "card stack", style: { marginTop: "26px" } }, h("div", { class: "spinner" }));
  api
    .referral()
    .then((r) => {
      const share = button([icon("send"), "Пригласить друга"], () => {
        const text = "Болтаю по-английски с другом-носителем в Telegram — он замечает ошибки и делает из них уроки. Попробуй:";
        const url = `https://t.me/share/url?url=${encodeURIComponent(r.link)}&text=${encodeURIComponent(text)}`;
        haptic("light");
        if (tg?.openTelegramLink) tg.openTelegramLink(url);
        else window.open(url, "_blank");
      });
      const copy = button("Скопировать ссылку", async () => {
        try {
          await navigator.clipboard.writeText(r.link);
          toast("Ссылка скопирована");
        } catch {
          toast(r.link);
        }
      }, "btn secondary");
      mount(
        box,
        h("div", { class: "row spread" }, h("div", { class: "eyebrow" }, "Вместе веселее"), icon("gift")),
        h("h2", {}, "Неделя премиума тебе и другу"),
        h("p", { class: "muted", style: { marginTop: "0" } }, `Когда друг сделает ${r.activation} реплик, вам обоим откроется ${countLabel(r.days, ["день", "дня", "дней"])} премиума.`),
        share,
        copy,
        r.invited ? h("p", { class: "tiny muted center" }, `Пришло по твоей ссылке: ${r.invited} · освоились: ${r.rewarded}`) : null,
      );
    })
    .catch(() => (box.hidden = true));
  return box;
}
