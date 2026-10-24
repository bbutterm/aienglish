// «Темы для тебя»: личные темы и сцены-миссии. Заголовки — сразу, содержание — по нажатию,
// «Обсудить» / «Начать сцену» — и собеседник пишет в чат.
import { api } from "../api.js";
import { icon } from "../icons.js";
import {
  button,
  card,
  closeApp,
  errorCard,
  h,
  haptic,
  loadingCard,
  mount,
  pageHead,
  spinner,
  toast,
} from "../ui.js";

const KIND = { talk: "Разговор", mission: "Сцена" };

export function ideaCard(idea, ctx) {
  return h(
    "button",
    {
      class: `idea${idea.kind === "mission" ? " mission" : ""}`,
      type: "button",
      onclick: () => ctx.navigate(`#/topics/${idea.id}`),
    },
    h(
      "div",
      { class: "row spread" },
      h("span", { class: "eyebrow" }, KIND[idea.kind] || "Тема"),
      icon(idea.kind === "mission" ? "mask" : "chat"),
    ),
    h("div", { class: "title" }, idea.title_ru),
    idea.focus_ru ? h("div", {}, h("span", { class: "badge" }, idea.focus_ru)) : null,
  );
}

export async function renderTopics(view, ctx, params = {}) {
  ctx.setTabsVisible(true);
  if (params.ideaId) {
    await renderIdea(view, ctx, params.ideaId);
    return;
  }
  mount(view, pageHead("Темы для тебя"), loadingCard("Подбираю темы под тебя…"));
  let data;
  try {
    data = await api.ideas();
  } catch (error) {
    mount(view, errorCard(error, () => renderTopics(view, ctx)));
    return;
  }
  drawList(view, ctx, data);
}

function drawList(view, ctx, data) {
  const more = button([icon("refresh"), "Ещё идеи"], refresh, "btn secondary small");
  more.disabled = data.refreshes_left <= 0;
  async function refresh() {
    more.disabled = true;
    more.replaceChildren(spinnerInline(), "Придумываю…");
    try {
      const fresh = await api.refreshIdeas();
      haptic("success");
      drawList(view, ctx, fresh);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      toast(error.message);
      more.replaceChildren(icon("refresh"), "Ещё идеи");
      more.disabled = false;
    }
  }

  const parts = [
    pageHead("Темы для тебя", more),
    h(
      "p",
      { class: "muted small", style: { margin: "-8px 2px 16px" } },
      "Подобраны по тому, о чём ты рассказывал, и по твоим ошибкам. Нажми — покажу фразы и вопросы, а потом Jake напишет в чат.",
    ),
  ];
  if (data.active_mission) parts.push(activeMission(data.active_mission));
  if (!data.ideas.length) {
    parts.push(
      h(
        "section",
        { class: "card empty" },
        icon("compass", "art"),
        h("p", {}, "Поболтай в чате пару минут — я узнаю тебя получше и предложу темы."),
        h("div", { style: { marginTop: "14px" } }, button("Открыть чат", closeApp)),
      ),
    );
  }
  const talks = data.ideas.filter((i) => i.kind === "talk");
  const missions = data.ideas.filter((i) => i.kind === "mission");
  if (missions.length) {
    parts.push(
      h("h3", {}, "Сцены с целями"),
      h("p", { class: "muted small", style: { margin: "-4px 4px 10px" } }, "Jake играет роль, ты добиваешься своего по-английски. Бот отмечает выполненные цели."),
      ...missions.map((idea) => ideaCard(idea, ctx)),
    );
  }
  if (talks.length) parts.push(h("h3", {}, "Поговорить"), ...talks.map((idea) => ideaCard(idea, ctx)));
  if (data.refreshes_left <= 0 && data.ideas.length) {
    parts.push(h("p", { class: "tiny muted center", style: { marginTop: "16px" } }, "Новые идеи — завтра."));
  }
  mount(view, ...parts);
}

function spinnerInline() {
  const s = spinner();
  s.style.width = "16px";
  s.style.height = "16px";
  s.style.borderWidth = "2px";
  s.style.margin = "0";
  return s;
}

function activeMission(mission) {
  const done = new Set(mission.done || []);
  return h(
    "section",
    { class: "card inverse stack", style: { marginBottom: "12px" } },
    h("div", { class: "eyebrow", style: { color: "var(--marker)" } }, "Сцена идёт"),
    h("h2", {}, mission.title_ru),
    h(
      "p",
      { class: "small" },
      `Целей выполнено: ${done.size} из ${mission.goals.length}. Продолжай в чате, выйти — /stop.`,
    ),
    button("Вернуться в чат", closeApp),
  );
}

async function renderIdea(view, ctx, ideaId) {
  ctx.setBack(() => {
    ctx.setBack(null);
    ctx.navigate("#/topics");
  });
  mount(view, h("section", { class: "card empty", style: { marginTop: "16vh" } }, spinner(), h("p", {}, "Готовлю тему: фразы, вопросы, цели…")));
  let idea;
  try {
    idea = await api.openIdea(ideaId);
  } catch (error) {
    mount(view, errorCard(error, () => renderIdea(view, ctx, ideaId)));
    return;
  }
  const c = idea.content || {};
  const mission = idea.kind === "mission";
  const persona = ctx.me.persona.name;
  const parts = [
    h("div", { class: "eyebrow", style: { margin: "0 2px 6px" } }, mission ? "Сцена с целями" : "Тема для разговора"),
    h("h1", { style: { margin: "0 2px 12px" } }, idea.title_ru),
    idea.focus_ru ? h("div", { class: "chips", style: { margin: "0 2px 16px" } }, h("span", { class: "badge accent" }, `Тренируем: ${idea.focus_ru}`)) : null,
    c.intro_ru ? h("p", { class: "lead", style: { margin: "0 2px 16px" } }, c.intro_ru) : null,
  ];
  if (mission) {
    parts.push(
      card(h("div", { class: "question-label" }, "Ситуация"), h("p", {}, c.setting_ru || c.setting_en)),
      card(
        h("div", { class: "question-label" }, "Твои цели"),
        h("ol", { class: "goals" }, (c.goals || []).map((g) => h("li", {}, h("div", {}, h("div", { style: { fontWeight: "600" } }, g.ru), h("div", { class: "small muted" }, g.en))))),
      ),
    );
  }
  if (c.phrases?.length) {
    parts.push(
      card(
        h("div", { class: "question-label" }, "Пригодится"),
        h("ul", { class: "phrases" }, c.phrases.map((p) => h("li", {}, h("div", { class: "row spread wrap" }, h("strong", {}, p.en), p.ru ? h("span", { class: "small muted" }, p.ru) : null)))),
      ),
    );
  }
  if (c.questions?.length) {
    parts.push(
      card(
        h("div", { class: "question-label" }, `${persona} может спросить`),
        h("ul", { class: "phrases" }, c.questions.map((q) => h("li", {}, q))),
      ),
    );
  }
  if (c.tip_ru) parts.push(h("div", { class: "callout", style: { marginTop: "12px" } }, h("strong", {}, "Подсказка"), h("span", { class: "small" }, c.tip_ru)));

  const start = button([mission ? "Начать сцену в чате" : `Обсудить с ${persona}`, icon("send")], go, "btn big");
  parts.push(
    h("div", { style: { marginTop: "18px" } }, start),
    h("p", { class: "tiny muted center", style: { marginTop: "8px" } }, mission ? `${persona} начнёт сцену в чате — отвечай голосом или текстом.` : `${persona} напишет в чат первым.`),
  );
  mount(view, ...parts);

  async function go() {
    start.disabled = true;
    try {
      await api.startIdea(idea.id);
      haptic("success");
      mount(
        view,
        h(
          "section",
          { class: "card empty", style: { marginTop: "14vh" } },
          icon(mission ? "mask" : "chat", "art"),
          h("h2", {}, `${persona} уже пишет тебе`),
          h("p", {}, mission ? "Сцена началась в чате. Цели отмечу по ходу, выйти — /stop." : "Открой чат — продолжим там."),
          h("div", { style: { marginTop: "16px" } }, button("Открыть чат", closeApp)),
        ),
      );
    } catch (error) {
      start.disabled = false;
      toast(error.message);
    }
  }
}
