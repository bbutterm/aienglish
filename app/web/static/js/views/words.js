// Вкладка «Слова»: словарь из разговоров и повторение по карточкам.
import { api } from "../api.js";
import { button, card, countLabel, errorCard, formatDate, h, loadingCard, mount, pageHead } from "../ui.js";
import { flashcard } from "./lesson.js";

const STATUS = { new: "новое", learning: "учу", known: "выучено" };

export async function renderWords(view, ctx) {
  ctx.setTabsVisible(true);
  mount(view, loadingCard());
  let data;
  try {
    data = await api.vocab();
  } catch (error) {
    mount(view, errorCard(error, () => renderWords(view, ctx)));
    return;
  }
  const parts = [pageHead("Слова", h("span", { class: "badge" }, countLabel(data.items.length, ["слово", "слова", "слов"])))];
  if (!data.items.length) {
    parts.push(
      h(
        "section",
        { class: "card empty" },
        h("div", { class: "emoji" }, "🗂"),
        h("p", {}, "Здесь появятся новые слова из твоих разговоров с ботом."),
      ),
    );
    mount(view, ...parts);
    return;
  }

  const due = data.items.filter((item) => item.due);
  parts.push(
    due.length
      ? h(
          "section",
          { class: "card hero-card stack" },
          h("div", {}, h("h2", {}, `К повторению: ${countLabel(due.length, ["слово", "слова", "слов"])}`), h("p", {}, "Пара минут на карточки — и слова останутся в голове.")),
          button("Повторить", () => review(view, ctx, due)),
        )
      : card(h("p", { class: "center" }, "Всё повторено 👏 Новые карточки появятся по расписанию.")),
  );

  const groups = [
    ["К повторению", data.items.filter((i) => i.due)],
    ["Учу", data.items.filter((i) => !i.due && i.status !== "known")],
    ["Выучено", data.items.filter((i) => !i.due && i.status === "known")],
  ];
  for (const [title, items] of groups) {
    if (!items.length) continue;
    parts.push(
      h("h3", {}, title),
      h(
        "section",
        { class: "card flush" },
        h(
          "ul",
          { class: "list" },
          items.map((item) =>
            h(
              "li",
              { class: "row spread" },
              h("div", {}, h("div", { style: { fontWeight: "600" } }, item.term), h("div", { class: "small muted" }, item.translation_ru || "—")),
              h("span", { class: "badge" }, item.due ? STATUS[item.status] || item.status : `повтор ${formatDate(item.next_review_at)}`),
            ),
          ),
        ),
      ),
    );
  }
  mount(view, ...parts);
}

function review(view, ctx, queue) {
  ctx.setTabsVisible(false);
  let index = 0;
  const finish = () => {
    ctx.setBack(null);
    renderWords(view, ctx);
  };
  ctx.setBack(finish);
  const show = () => {
    if (index >= queue.length) {
      mount(
        view,
        h(
          "section",
          { class: "card empty" },
          h("div", { class: "emoji" }, "🎉"),
          h("h2", { style: { marginTop: "8px" } }, "Готово!"),
          h("p", {}, `Повторено: ${countLabel(queue.length, ["слово", "слова", "слов"])}`),
          h("div", { style: { marginTop: "16px" } }, button("К словам", finish)),
        ),
      );
      return;
    }
    const progress = h(
      "div",
      { class: "lesson-top" },
      h("button", { class: "link-btn", type: "button", "aria-label": "Закончить", onclick: finish }, "✕"),
      h("div", { class: "progress-track" }, h("div", { class: "progress-fill", style: { width: `${(index / queue.length) * 100}%` } })),
      h("span", { class: "tiny muted" }, `${index}/${queue.length}`),
    );
    mount(view, progress, flashcard(queue[index], () => { index += 1; show(); }, "Повторение"));
  };
  show();
}
