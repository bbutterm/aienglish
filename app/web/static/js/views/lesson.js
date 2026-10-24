// Вкладка «Урок»: урок по последним разговорам — тема, разбор ошибок и грамматика, слова, итог.
import { api } from "../api.js";
import {
  button,
  card,
  closeApp,
  countLabel,
  errorCard,
  h,
  haptic,
  loadingCard,
  mount,
  pageHead,
  spinner,
  toast,
} from "../ui.js";

const GENERATION_HINTS = [
  "Перечитываю ваш разговор…",
  "Подбираю фразы по теме…",
  "Разбираю твои ошибки…",
  "Объясняю правило и готовлю упражнения…",
  "Почти готово…",
];

const capitalize = (text) => (text ? text[0].toUpperCase() + text.slice(1) : text);

// План урока: 1) тема разговора, 2) разбор ошибок и грамматика.
function lessonPlan({ topic, mistakes, level, words }) {
  const item = (num, title, note) =>
    h(
      "li",
      {},
      h("span", { class: "num", "aria-hidden": "true" }, num),
      h("div", {}, h("strong", {}, title), note ? h("div", { class: "small muted" }, note) : null),
    );
  return h(
    "ol",
    { class: "plan" },
    item("1", topic ? `Тема: ${topic}` : "Тема разговора", "полезные фразы и практика по теме"),
    item(
      "2",
      "Разбор ошибок и грамматика",
      mistakes
        ? `${countLabel(mistakes, ["твоя ошибка", "твои ошибки", "твоих ошибок"])} и правило к ним`
        : `типичные трудности уровня ${level}`,
    ),
    words ? item("+", "Слова", `${countLabel(words, ["слово", "слова", "слов"])} на повторение`) : null,
  );
}

function topicChips(topics) {
  return h(
    "div",
    { class: "chips" },
    topics.map((topic) => h("span", { class: "badge" }, `💬 ${topic}`)),
  );
}

export async function renderLessonTab(view, ctx) {
  ctx.setTabsVisible(true);
  mount(view, loadingCard());
  let today;
  try {
    today = await api.today();
  } catch (error) {
    mount(view, errorCard(error, () => renderLessonTab(view, ctx)));
    return;
  }
  renderHome(view, ctx, today);
}

function renderHome(view, ctx, today) {
  const { me } = ctx;
  const head = pageHead(
    `Привет, ${me.name}!`,
    me.premium ? h("span", { class: "badge accent" }, "⭐ Премиум") : null,
  );
  if (!me.onboarded) {
    mount(
      view,
      head,
      h(
        "section",
        { class: "card empty" },
        h("div", { class: "emoji" }, "👋"),
        h("h2", { style: { marginTop: "8px" } }, "Сначала познакомимся"),
        h("p", {}, "Нажми /start в чате с ботом: имя, уровень и собеседник — и можно учиться."),
        h("div", { style: { marginTop: "16px" } }, button("Открыть чат", closeApp)),
      ),
    );
    return;
  }

  const parts = [head];
  const inProgress = today.lessons.find((lesson) => lesson.status !== "done");
  const done = today.lessons.filter((lesson) => lesson.status === "done");
  if (inProgress) {
    const answered = inProgress.exercises.filter((e) => e.result).length;
    parts.push(
      h(
        "section",
        { class: "card hero-card stack" },
        h(
          "div",
          {},
          h("h2", {}, inProgress.title),
          h("p", {}, `Урок начат: ${answered} из ${inProgress.exercises.length} упражнений.`),
        ),
        button(answered ? "Продолжить урок" : "Начать урок", () => playLesson(view, ctx, inProgress)),
      ),
    );
  }
  for (const lesson of done) parts.push(doneCard(lesson));
  if (!inProgress && today.remaining > 0) parts.push(newLessonCard(view, ctx, today, done.length > 0));
  if (!inProgress && today.remaining === 0) {
    parts.push(
      card(
        h("p", { class: "center" }, "Следующий урок — завтра."),
        h("p", { class: "center muted small", style: { marginTop: "4px" } }, "А пока можно поболтать в чате 💬"),
        h("div", { style: { marginTop: "14px" } }, button("Вернуться в чат", closeApp, "btn secondary")),
      ),
    );
  }
  mount(view, ...parts);
}

function newLessonCard(view, ctx, today, another) {
  const { mistakes, words, messages, topics } = today.material;
  const talked = messages > 0;
  const about = talked
    ? topics.length
      ? h("div", { class: "stack tight" }, h("div", { class: "small muted" }, "О чём вы говорили"), topicChips(topics))
      : h("p", {}, `Урок соберу из вашего разговора с ${ctx.me.persona.name}.`)
    : h("p", {}, `Поболтай с ${ctx.me.persona.name} в чате — и урок будет про ваш разговор. А пока соберу его по твоим интересам и уровню.`);
  return h(
    "section",
    { class: "card hero-card stack" },
    h("div", {}, h("h2", {}, another ? "Ещё один урок" : "Урок по твоим разговорам")),
    about,
    lessonPlan({ topic: topics[0], mistakes, level: ctx.me.level, words }),
    h("button", { class: "btn big", type: "button", onclick: () => generate(view, ctx) }, "✨ Сгенерировать урок"),
    h(
      "p",
      { class: "tiny muted center" },
      `Сегодня доступно: ${countLabel(today.remaining, ["урок", "урока", "уроков"])} · около 5 минут`,
    ),
  );
}

function doneCard(lesson) {
  const insights = lesson.insights;
  return h(
    "section",
    { class: "card stack" },
    h(
      "div",
      { class: "row spread" },
      h("div", {}, h("div", { class: "small muted" }, "Урок пройден ✅"), h("h2", {}, lesson.title)),
      h("div", { class: "badge" }, `${lesson.score ?? 0}/${lesson.total ?? 0}`),
    ),
    lesson.topic_ru ? topicChips([lesson.topic_ru]) : null,
    insights?.tip_ru ? h("p", { class: "small" }, `💡 ${insights.tip_ru}`) : null,
  );
}

async function generate(view, ctx) {
  ctx.setTabsVisible(false);
  const hint = h("p", {}, GENERATION_HINTS[0]);
  mount(
    view,
    h(
      "section",
      { class: "card empty", style: { marginTop: "20vh" } },
      spinner(),
      h("h2", { style: { marginTop: "18px" } }, "Собираю урок"),
      hint,
    ),
  );
  let i = 0;
  const timer = setInterval(() => {
    i = Math.min(i + 1, GENERATION_HINTS.length - 1);
    hint.textContent = GENERATION_HINTS[i];
  }, 3500);
  try {
    const lesson = await api.createLesson();
    haptic("success");
    playLesson(view, ctx, lesson);
  } catch (error) {
    haptic("error");
    ctx.setTabsVisible(true);
    mount(view, errorCard(error, () => renderLessonTab(view, ctx)));
  } finally {
    clearInterval(timer);
  }
}

// Шаги урока по разделам: тема → разбор ошибок и правила → слова.
function lessonSteps(lesson) {
  const exercises = (section) =>
    lesson.exercises.filter((e) => e.section === section).map((exercise) => ({ kind: "exercise", section, exercise }));
  const reviewed = new Set(lesson.mistakes_review.map((m) => m.id));
  return [
    { kind: "intro" },
    ...(lesson.topic_block ? [{ kind: "topic", section: "topic" }] : []),
    ...exercises("topic"),
    ...(reviewed.size ? [{ kind: "review", section: "grammar" }] : []),
    ...lesson.grammar.map((rule) => ({ kind: "rule", section: "grammar", rule })),
    ...exercises("grammar"),
    ...lesson.cards.map((item) => ({ kind: "card", section: "words", item })),
    { kind: "finish" },
  ];
}

function sectionLabel(lesson, steps, section) {
  const both = steps.some((s) => s.section === "topic") && steps.some((s) => s.section === "grammar");
  if (section === "topic") {
    return `${both ? "Часть 1 · " : ""}Тема${lesson.topic_ru ? `: ${lesson.topic_ru}` : ""}`;
  }
  if (section === "grammar") return `${both ? "Часть 2 · " : ""}Ошибки и грамматика`;
  if (section === "words") return "Слова";
  return null;
}

export function playLesson(view, ctx, lesson) {
  ctx.setTabsVisible(false);
  const steps = lessonSteps(lesson);
  let position = 0;
  if (lesson.exercises.some((e) => e.result)) {
    // Продолжаем с первого нерешённого упражнения, показав заново теорию его раздела.
    position = steps.findIndex((s) => s.kind === "exercise" && !s.exercise.result);
    if (position === -1) position = steps.findIndex((s) => s.kind === "card" || s.kind === "finish");
    while (["topic", "review", "rule"].includes(steps[position - 1]?.kind)) position -= 1;
  }
  const leave = () => {
    ctx.setBack(null);
    renderLessonTab(view, ctx);
  };
  ctx.setBack(leave);

  const show = () => {
    const step = steps[position];
    const work = steps.length - 2; // без вступления и итога
    const done = Math.min(Math.max(position - 1, 0), work);
    const next = () => {
      position += 1;
      show();
      window.scrollTo({ top: 0 });
    };
    const top = h(
      "div",
      { class: "lesson-top" },
      h("button", { class: "link-btn", type: "button", "aria-label": "Выйти из урока", onclick: leave }, "✕"),
      h(
        "div",
        {
          class: "progress-track",
          role: "progressbar",
          "aria-valuemin": 0,
          "aria-valuemax": work,
          "aria-valuenow": done,
        },
        h("div", { class: "progress-fill", style: { width: `${work ? (done / work) * 100 : 100}%` } }),
      ),
      h("span", { class: "tiny muted" }, `${done}/${work}`),
    );
    let body;
    if (step.kind === "intro") body = introCard(lesson, ctx, next);
    else if (step.kind === "topic") body = topicCard(lesson, next);
    else if (step.kind === "review") body = reviewCard(lesson, next);
    else if (step.kind === "rule") body = ruleCard(lesson, step.rule, next);
    else if (step.kind === "exercise") body = exerciseCard(lesson, step.exercise, next);
    else if (step.kind === "card") body = flashcard(step.item, next);
    else body = finishCard(lesson, ctx);
    const label = sectionLabel(lesson, steps, step.section);
    mount(view, top, label ? h("div", { class: "section-label" }, label) : null, body);
  };
  show();
}

function introCard(lesson, ctx, next) {
  const words = lesson.cards.length;
  return h(
    "section",
    { class: "card hero-card stack" },
    h("div", {}, h("h2", {}, lesson.title), lesson.intro_ru ? h("p", {}, lesson.intro_ru) : null),
    lessonPlan({ topic: lesson.topic_ru, mistakes: lesson.mistakes_review.length, level: ctx.me.level, words }),
    h(
      "p",
      { class: "tiny muted center" },
      countLabel(lesson.exercises.length, ["упражнение", "упражнения", "упражнений"]),
    ),
    button("Начать", next),
  );
}

function topicCard(lesson, next) {
  const block = lesson.topic_block;
  return card(
    h("div", { class: "question-label" }, "О чём вы говорили"),
    h("h2", {}, capitalize(lesson.topic_ru) || "Ваш разговор"),
    block.summary_ru ? h("p", { class: "lead" }, block.summary_ru) : null,
    block.phrases.length
      ? [
          h("div", { class: "question-label", style: { marginTop: "16px" } }, "Пригодится в следующий раз"),
          h(
            "ul",
            { class: "phrases" },
            block.phrases.map((p) =>
              h(
                "li",
                {},
                h("div", { class: "row spread wrap" }, h("strong", {}, p.en), p.ru ? h("span", { class: "small muted" }, p.ru) : null),
                p.example ? h("div", { class: "example" }, p.example) : null,
              ),
            ),
          ),
        ]
      : null,
    h("div", { style: { marginTop: "16px" } }, button("Дальше", next)),
  );
}

function reviewCard(lesson, next) {
  return card(
    h("div", { class: "question-label" }, "Разбор твоих ошибок"),
    h("p", { class: "small muted" }, "Как ты сказал в чате → как сказал бы носитель"),
    h(
      "ul",
      { class: "list review-list" },
      lesson.mistakes_review.map((m) =>
        h(
          "li",
          { class: "mistake-pair" },
          h("div", { class: "row spread" }, h("span", { class: "wrong-text" }, m.original), h("span", { class: "badge" }, m.type_ru)),
          h("div", { class: "right-text" }, m.corrected),
          m.explanation_ru ? h("div", { class: "small muted" }, m.explanation_ru) : null,
        ),
      ),
    ),
    h("div", { style: { marginTop: "16px" } }, button("Дальше", next)),
  );
}

function ruleCard(lesson, rule, next) {
  const own = lesson.mistakes_review.filter((m) => rule.mistake_ids.includes(m.id)).slice(0, 2);
  return card(
    h("div", { class: "question-label" }, "Правило"),
    h("h2", {}, rule.title_ru),
    h("p", { class: "lead" }, rule.rule_ru),
    rule.examples.length
      ? h(
          "ul",
          { class: "phrases" },
          rule.examples.map((e) => h("li", {}, h("strong", {}, e.en), e.ru ? h("div", { class: "small muted" }, e.ru) : null)),
        )
      : null,
    own.length
      ? h(
          "div",
          { class: "callout" },
          h("div", { class: "small muted" }, own.length > 1 ? "Твои ошибки на это правило" : "Твоя ошибка на это правило"),
          own.map((m) => h("div", { class: "mistake-pair" }, h("span", { class: "wrong-text" }, m.original), " → ", h("span", { class: "right-text" }, m.corrected))),
        )
      : null,
    h("div", { style: { marginTop: "16px" } }, button("Дальше", next)),
  );
}

function gapPrompt(sentence) {
  const [before, ...rest] = sentence.split("___");
  const blank = h("span", { class: "blank", "aria-label": "пропуск" }, " ");
  return { element: h("p", { class: "prompt" }, before, blank, rest.join("___")), blank };
}

function audioPlayer(lessonId, index) {
  const audio = new Audio();
  let ready = null;
  const playBtn = h("button", { class: "play", type: "button", "aria-label": "Прослушать" }, "▶");
  const load = () => {
    ready ??= api
      .audio(lessonId, index)
      .then((blob) => {
        audio.src = URL.createObjectURL(blob);
      })
      .catch((error) => {
        ready = null;
        throw error;
      });
    return ready;
  };
  // Загружаем заранее: на iOS play() должен идти сразу из нажатия, без ожидания сети.
  load().catch(() => {});
  playBtn.addEventListener("click", async () => {
    try {
      await load();
      audio.currentTime = 0;
      playBtn.textContent = "🔊";
      await audio.play();
    } catch (error) {
      playBtn.textContent = "▶";
      toast(error?.message || "Не получилось воспроизвести");
    }
  });
  audio.addEventListener("ended", () => (playBtn.textContent = "▶"));
  return h(
    "div",
    { class: "center", style: { margin: "8px 0 4px" } },
    playBtn,
    h("p", { class: "tiny muted", style: { marginTop: "8px" } }, "Можно слушать сколько угодно раз"),
  );
}

function exerciseCard(lesson, exercise, next) {
  const content = [h("div", { class: "question-label" }, exercise.question_ru)];
  let blank = null;
  if (exercise.type === "gap") {
    const gap = gapPrompt(exercise.sentence);
    blank = gap.blank;
    content.push(gap.element);
  } else if (exercise.type === "translate") {
    content.push(h("p", { class: "prompt" }, exercise.ru));
  } else if (exercise.type === "listen") {
    content.push(audioPlayer(lesson.id, exercise.index));
  }

  const marks = [];
  const options = exercise.options.map((text, i) => {
    const mark = h("span", { class: "mark", "aria-hidden": "true" });
    marks.push(mark);
    return h("button", { class: "option", type: "button", onclick: () => choose(i) }, h("span", {}, text), mark);
  });
  const feedback = h("div", { "aria-live": "polite" });
  const nextBtn = button("Дальше", next);
  nextBtn.hidden = true;

  function apply(result) {
    options.forEach((option, i) => {
      option.disabled = true;
      if (i === result.answer) {
        option.classList.add("correct");
        marks[i].textContent = "✓";
      } else if (i === result.choice) {
        option.classList.add("wrong");
        marks[i].textContent = "✗";
      } else {
        option.classList.add("dim");
      }
    });
    if (blank) blank.textContent = exercise.options[result.answer];
    feedback.replaceChildren(
      h(
        "div",
        { class: `feedback ${result.correct ? "good" : "bad"}` },
        h("strong", {}, result.correct ? "✓ Верно!" : "✗ Не совсем"),
        result.text ? h("div", {}, `Звучало: «${result.text}»`) : null,
        result.explanation_ru ? h("div", { class: "small" }, result.explanation_ru) : null,
      ),
    );
    nextBtn.hidden = false;
  }

  async function choose(i) {
    options.forEach((option) => (option.disabled = true));
    try {
      const result = await api.answer(lesson.id, exercise.index, i);
      exercise.result = result;
      haptic(result.correct ? "success" : "error");
      apply(result);
      nextBtn.focus({ preventScroll: true });
    } catch (error) {
      options.forEach((option) => (option.disabled = false));
      toast(error.message);
    }
  }

  if (exercise.result) apply(exercise.result);
  return card(...content, h("div", { class: "options" }, options), feedback, h("div", { style: { marginTop: "14px" } }, nextBtn));
}

export function flashcard(item, next, label = "Слово из твоих разговоров") {
  const reveal = h(
    "div",
    { class: "stack center", hidden: true },
    h("div", { class: "translation" }, item.translation_ru || "—"),
    item.example ? h("div", { class: "example" }, item.example) : null,
  );
  const grades = h("div", { class: "btn-row", hidden: true });
  const showBtn = button("Показать перевод", () => {
    reveal.hidden = false;
    showBtn.hidden = true;
    grades.hidden = false;
  });
  const grade = async (value, buttons) => {
    buttons.forEach((b) => (b.disabled = true));
    try {
      await api.review(item.vocab_id ?? item.id, value);
      haptic(value === "good" ? "success" : "warning");
      next();
    } catch (error) {
      buttons.forEach((b) => (b.disabled = false));
      toast(error.message);
    }
  };
  const again = button("Не помню", () => grade("again", [again, good]), "btn secondary");
  const good = button("Помню", () => grade("good", [again, good]));
  grades.append(again, good);
  return card(
    h("div", { class: "question-label center" }, label),
    h("div", { class: "flashcard" }, h("div", { class: "term" }, item.term), reveal),
    showBtn,
    grades,
  );
}

function finishCard(lesson, ctx) {
  const box = h("section", { class: "card empty" });
  const load = () => {
    box.replaceChildren(spinner());
    api
      .complete(lesson.id)
      .then((done) => {
        Object.assign(lesson, done);
        const ratio = done.total ? done.score / done.total : 0;
        haptic("success");
        box.replaceChildren(
          h("div", { class: "emoji" }, ratio >= 0.8 ? "🎉" : ratio >= 0.5 ? "💪" : "🌱"),
          h("div", { class: "score", style: { marginTop: "8px" } }, `${done.score}/${done.total}`),
          h("p", {}, "правильных ответов"),
          done.topic
            ? h(
                "p",
                { style: { marginTop: "14px", color: "var(--text)" } },
                `${ctx.me.persona.name} уже пишет тебе в чат — поболтаем про «${done.topic}».`,
              )
            : null,
          done.insights?.tip_ru ? h("p", { class: "small" }, `💡 ${done.insights.tip_ru}`) : null,
          h(
            "div",
            { class: "stack", style: { marginTop: "18px" } },
            button("Вернуться в чат", closeApp),
            button(
              "Мой прогресс",
              () => {
                ctx.setBack(null);
                ctx.navigate("#/progress");
              },
              "btn secondary",
            ),
          ),
        );
      })
      .catch((error) =>
        box.replaceChildren(
          h("p", {}, error.message),
          h("div", { style: { marginTop: "14px" } }, button("Повторить", load, "btn secondary")),
        ),
      );
  };
  load();
  return box;
}
