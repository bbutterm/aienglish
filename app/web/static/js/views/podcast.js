// «Подкаст про тебя»: Jake и Emma двумя голосами обсуждают неделю ученика.
import { api } from "../api.js";
import { icon } from "../icons.js";
import { button, closeApp, errorCard, formatDate, h, haptic, loadingCard, mount, pageHead, spinner, toast } from "../ui.js";

const HOSTS = { jake: { name: "Jake", cls: "" }, emma: { name: "Emma", cls: " emma" } };
const MAKING = ["Jake и Emma читают вашу переписку…", "Пишут сценарий выпуска…", "Записывают голоса…", "Сводят звук…"];

export async function renderPodcast(view, ctx) {
  ctx.setTabsVisible(true);
  ctx.setBack(() => {
    ctx.setBack(null);
    ctx.navigate("#/");
  });
  mount(view, pageHead("Подкаст про тебя"), loadingCard());
  let data;
  try {
    data = await api.podcasts();
  } catch (error) {
    mount(view, errorCard(error, () => renderPodcast(view, ctx)));
    return;
  }
  draw(view, ctx, data, data.items[0] || null);
}

function draw(view, ctx, data, episode) {
  const parts = [pageHead("Подкаст про тебя", h("span", { class: "badge outline" }, "Jake & Emma"))];
  if (!episode) {
    parts.push(
      h(
        "section",
        { class: "card inverse stack" },
        icon("headphones", "art"),
        h("h2", {}, "Выпуск про твою неделю"),
        h(
          "p",
          {},
          "Две минуты на английском: о чём вы говорили, твои новые слова и пара ошибок — Jake и Emma разберут их как друзья, а в конце зададут вопрос.",
        ),
        makeButton(view, ctx, data),
      ),
    );
    mount(view, ...parts);
    return;
  }
  parts.push(episodeCard(episode), makeRow(view, ctx, data));
  if (episode.words.length) {
    parts.push(
      h("h3", {}, "Слова из выпуска"),
      h(
        "section",
        { class: "card flush" },
        h("ul", { class: "list" }, episode.words.map((w) => h("li", { class: "row spread" }, h("strong", {}, w.en), h("span", { class: "small muted" }, w.ru)))),
      ),
    );
  }
  parts.push(
    h("h3", {}, "Текст выпуска"),
    h(
      "div",
      { class: "transcript" },
      episode.lines.map((line) => {
        const host = HOSTS[line.speaker] || HOSTS.jake;
        return h(
          "div",
          { class: "bubble-row" },
          h("span", { class: `avatar small${host.cls}`, "aria-hidden": "true" }, host.name[0]),
          h("div", { class: `bubble${host.cls}` }, h("div", { class: "who" }, host.name), line.text),
        );
      }),
    ),
  );
  if (episode.question) {
    parts.push(
      h(
        "section",
        { class: "card stack", style: { marginTop: "8px" } },
        h("div", { class: "eyebrow" }, "Вопрос тебе"),
        h("p", { class: "lead", style: { marginTop: "0" } }, episode.question),
        button("Ответить в чате", closeApp),
      ),
    );
  }
  const older = data.items.filter((p) => p.id !== episode.id);
  if (older.length) {
    parts.push(
      h("h3", {}, "Прошлые выпуски"),
      h(
        "section",
        { class: "card flush" },
        h(
          "ul",
          { class: "list" },
          older.map((p) =>
            h(
              "li",
              { class: "clickable row spread", tabindex: 0, role: "button", onclick: () => draw(view, ctx, data, p) },
              h("span", {}, p.title),
              h("span", { class: "tiny muted" }, formatDate(p.created_at)),
            ),
          ),
        ),
      ),
    );
  }
  mount(view, ...parts);
}

function episodeCard(episode) {
  const audio = new Audio();
  let loaded = null;
  const bars = Array.from({ length: 36 }, (_, i) => 8 + Math.round(Math.abs(Math.sin(i * 1.7 + episode.id)) * 24));
  const spans = bars.map((v) => h("span", { style: { height: `${v}px` } }));
  const wave = h("div", { class: "wave", role: "slider", "aria-label": "Позиция в выпуске", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": 0, tabindex: 0 }, spans);
  const time = h("span", { class: "tiny muted" }, "0:00");
  const play = h("button", { class: "play", type: "button", "aria-label": "Слушать" }, icon("play"));
  const load = () => {
    loaded ??= api.podcastAudio(episode.id).then((blob) => {
      audio.src = URL.createObjectURL(blob);
    });
    return loaded;
  };
  load().catch(() => {
    loaded = null;
  });
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  audio.addEventListener("timeupdate", () => {
    const ratio = audio.duration ? audio.currentTime / audio.duration : 0;
    const on = Math.round(ratio * spans.length);
    spans.forEach((s, i) => s.classList.toggle("on", i < on));
    wave.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    time.textContent = `${fmt(audio.currentTime)} / ${fmt(audio.duration || 0)}`;
  });
  audio.addEventListener("ended", () => play.replaceChildren(icon("play")));
  play.addEventListener("click", async () => {
    try {
      await load();
      if (audio.paused) {
        await audio.play();
        play.replaceChildren(icon("pause"));
      } else {
        audio.pause();
        play.replaceChildren(icon("play"));
      }
    } catch (error) {
      toast(error?.message || "Не получилось воспроизвести");
    }
  });
  wave.addEventListener("click", (event) => {
    if (!audio.duration) return;
    const rect = wave.getBoundingClientRect();
    audio.currentTime = ((event.clientX - rect.left) / rect.width) * audio.duration;
  });
  const send = button([icon("send"), "Прислать в чат"], async () => {
    send.disabled = true;
    try {
      await api.sendPodcast(episode.id);
      haptic("success");
      toast("Выпуск в чате — слушай в плеере Telegram");
    } catch (error) {
      toast(error.message);
    } finally {
      send.disabled = false;
    }
  }, "btn secondary small");
  return h(
    "section",
    { class: "card inverse stack" },
    h("div", { class: "row spread" }, h("div", { class: "eyebrow", style: { color: "var(--marker)" } }, `Выпуск от ${formatDate(episode.created_at)}`), time),
    h("h2", {}, episode.title),
    h("div", { class: "player" }, play, wave),
    episode.has_audio ? h("div", {}, send) : h("p", { class: "small" }, "Аудио не записалось — текст ниже."),
  );
}

function makeRow(view, ctx, data) {
  if (data.remaining <= 0) {
    return h("p", { class: "tiny muted center", style: { marginTop: "10px" } }, "Новый выпуск — через неделю (с премиумом — каждый день).");
  }
  return h("div", { style: { marginTop: "12px" } }, makeButton(view, ctx, data, "btn secondary"));
}

function makeButton(view, ctx, data, cls = "btn big") {
  const label = data.items.length ? "Записать новый выпуск" : "Записать выпуск";
  const make = button(label, async () => {
    make.disabled = true;
    const hint = h("p", {}, MAKING[0]);
    mount(view, h("section", { class: "card empty", style: { marginTop: "16vh" } }, spinner(), h("h2", { style: { marginTop: "16px" } }, "Записываем выпуск"), hint));
    let i = 0;
    const timer = setInterval(() => {
      i = Math.min(i + 1, MAKING.length - 1);
      hint.textContent = MAKING[i];
    }, 4000);
    try {
      const episode = await api.createPodcast();
      haptic("success");
      const fresh = await api.podcasts();
      draw(view, ctx, fresh, fresh.items.find((p) => p.id === episode.id) || episode);
    } catch (error) {
      haptic("error");
      mount(view, errorCard(error, () => renderPodcast(view, ctx)));
    } finally {
      clearInterval(timer);
    }
  }, cls);
  if (data.remaining <= 0) make.disabled = true;
  return make;
}
