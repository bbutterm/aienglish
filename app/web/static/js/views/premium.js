// «Премиум»: тарифы за звёзды Telegram, оплата прямо в приложении через openInvoice.
import { api } from "../api.js";
import { button, errorCard, formatDate, h, haptic, loadingCard, mount, pageHead, tg, toast } from "../ui.js";

export async function renderPremium(view, ctx) {
  ctx.setTabsVisible(true);
  ctx.setBack(() => {
    ctx.setBack(null);
    ctx.navigate("#/");
  });
  mount(view, pageHead("Премиум"), loadingCard());
  let data;
  try {
    data = await api.premium();
  } catch (error) {
    mount(view, errorCard(error, () => renderPremium(view, ctx)));
    return;
  }
  const persona = ctx.me.persona.name;
  const parts = [
    pageHead("Премиум", data.premium ? h("span", { class: "badge accent" }, `до ${formatDate(data.premium_until, true)}`) : null),
    h(
      "section",
      { class: "card inverse stack" },
      h("div", { class: "eyebrow", style: { color: "var(--marker)" } }, "Что даёт"),
      h(
        "ul",
        { class: "points good", style: { margin: "0" } },
        [
          `Живой голос ${persona} без дневного лимита`,
          "3 урока в день, миссии и подкаст про тебя",
          "Больше реплик и тем каждый день",
        ].map((t) => h("li", {}, h("span", { class: "icon" }, "✓"), h("span", {}, t))),
      ),
    ),
    h("h3", {}, "Оплата звёздами Telegram"),
  ];
  for (const plan of data.plans) {
    const best = plan.key === "year";
    const perMonth = Math.round(plan.stars / (plan.days / 30));
    const pay = button(`Оплатить ${plan.stars} ⭐`, () => buy(plan, pay), best ? "btn" : "btn secondary");
    parts.push(
      h(
        "section",
        { class: `card stack${best ? " idea mission" : ""}`, style: { cursor: "default", marginTop: "10px" } },
        h("div", { class: "row spread" }, h("h2", {}, plan.title), best ? h("span", { class: "badge accent" }, "выгоднее") : null),
        h("p", { class: "muted small", style: { margin: "0" } }, plan.description),
        h("div", { class: "small" }, plan.subscription ? `${plan.stars} ⭐ каждые 30 дней` : `≈ ${perMonth} ⭐ в месяц`),
        pay,
      ),
    );
  }
  parts.push(h("p", { class: "tiny muted center", style: { marginTop: "14px" } }, "Подписку можно отменить в Telegram → Настройки → Звёзды. Вопросы — /paysupport в чате."));
  mount(view, ...parts);

  async function buy(plan, btn) {
    btn.disabled = true;
    try {
      const { link } = await api.premiumInvoice(plan.key);
      if (!tg?.openInvoice) {
        window.open(link, "_blank");
        return;
      }
      tg.openInvoice(link, async (status) => {
        btn.disabled = false;
        if (status === "paid") {
          haptic("success");
          toast("Премиум включён — спасибо!");
          ctx.me = await api.me();
          renderPremium(view, ctx);
        } else if (status === "failed") {
          toast("Оплата не прошла. Попробуй ещё раз.");
        }
      });
    } catch (error) {
      btn.disabled = false;
      toast(error.message);
    }
  }
}
