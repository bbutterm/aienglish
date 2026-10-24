// Вкладка «Слова»: словарь из разговоров, прокачанные слова, слова с фото и из постов;
// повторение по карточкам.
import { api } from "../api.js";
import { icon } from "../icons.js";
import { button, card, closeApp, countLabel, errorCard, formatDate, h, loadingCard, mount, pageHead } from "../ui.js";
import { flashcard } from "./lesson.js";

const STATUS = { new: "новое", learning: "учу", known: "выучено" };
const FILTERS = [
  { key: "all", label: "Все", test: () => true },
  { key: "upgrade", label: "Прокачка", test: (i) => i.source === "upgrade" },
  { key: "chat", label: "Из разговоров", test: (i) => !i.source || ["bot", "user"].includes(i.source) },
  { key: "photo", label: "С фото", test: (i) => i.source === "photo" },
  { key: "forward", label: "Из постов", test: (i) => i.source === "forward" },
];

let currentFilter = "all";

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
  draw(view, ctx, data);
}

function wordRow(item) {
  const upgrade = item.source === "upgrade" && item.note;
  return h(
    "li",
    {},
    h(
      "div",
      { class: "row spread", style: { alignItems: "flex-start" } },
      h(
        "div",
        { style: { minWidth: "0" } },
        upgrade
          ? h("div", {}, h("span", { class: "pen" }, item.note), " → ", h("strong", { class: "hl" }, item.term))
          : h("div", { style: { fontWeight: "600" } }, item.term),
        h("div", { class: "small muted" }, item.translation_ru || "—"),
        item.example ? h("div", { class: "small example", style: { marginTop: "4px" } }, item.example) : null,
      ),
      h("span", { class: "badge" }, item.due ? STATUS[item.status] || item.status : `повтор ${formatDate(item.next_review_at)}`),
    ),
  );
}

function draw(view, ctx, data) {
  const parts = [pageHead("Слова", h("span", { class: "badge outline" }, countLabel(data.items.length, ["слово", "слова", "слов"])))];
  if (!data.items.length) {
    parts.push(
      h(
        "section",
        { class: "card empty" },
        icon("cards", "art"),
        h("p", {}, "Здесь появятся слова из твоих разговоров, с фото и из пересланных постов."),
        h("div", { style: { marginTop: "14px" } }, button("Открыть чат", closeApp)),
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
          h("div", { class: "eyebrow" }, "Повторение"),
          h("h2", {}, h("span", { class: "hl" }, countLabel(due.length, ["слово", "слова", "слов"])), " ждут тебя"),
          h("p", { style: { marginTop: "0" } }, "Пара минут на карточки — и слова останутся в голове."),
          button("Повторить", () => review(view, ctx, due), "btn big"),
        )
      : card(h("p", { class: "muted" }, "Всё повторено. Новые карточки появятся по расписанию.")),
  );

  const available = FILTERS.filter((f) => f.key === "all" || data.items.some(f.test));
  if (!available.some((f) => f.key === currentFilter)) currentFilter = "all";
  const filter = FILTERS.find((f) => f.key === currentFilter);
  const items = data.items.filter(filter.test);
  if (available.length > 2) {
    parts.push(
      h("div", { style: { height: "18px" } }),
      h(
        "div",
        { class: "filters", role: "toolbar", "aria-label": "Какие слова показать" },
        available.map((f) =>
          h(
            "button",
            {
              class: "filter",
              type: "button",
              "aria-pressed": String(f.key === currentFilter),
              onclick: () => {
                currentFilter = f.key;
                draw(view, ctx, data);
              },
            },
            f.label,
          ),
        ),
      ),
    );
  }
  if (currentFilter === "upgrade") {
    parts.push(h("p", { class: "muted small", style: { margin: "0 4px 10px" } }, "Слова, которыми ты пользуешься слишком часто, и чем их заменить — на твоих же фразах."));
  }

  const groups = [
    ["К повторению", items.filter((i) => i.due)],
    ["Учу", items.filter((i) => !i.due && i.status !== "known")],
    ["Выучено", items.filter((i) => !i.due && i.status === "known")],
  ];
  for (const [title, group] of groups) {
    if (!group.length) continue;
    parts.push(h("h3", {}, title), h("section", { class: "card flush" }, h("ul", { class: "list" }, group.map(wordRow))));
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
          icon("cards", "art"),
          h("h2", { style: { marginTop: "8px" } }, "Готово"),
          h("p", {}, `Повторено: ${countLabel(queue.length, ["слово", "слова", "слов"])}`),
          h("div", { style: { marginTop: "16px" } }, button("К словам", finish)),
        ),
      );
      return;
    }
    const progress = h(
      "div",
      { class: "lesson-top" },
      h("button", { class: "icon-btn", type: "button", "aria-label": "Закончить", onclick: finish }, icon("close")),
      h("div", { class: "progress-track" }, h("div", { class: "progress-fill", style: { width: `${(index / queue.length) * 100}%` } })),
      h("span", { class: "tiny muted" }, `${index}/${queue.length}`),
    );
    mount(view, progress, flashcard(queue[index], () => { index += 1; show(); }, "Повторение"));
  };
  show();
}
