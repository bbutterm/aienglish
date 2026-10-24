// Линейные иконки 24×24 (1.75px, скруглённые концы) — свои, вместо эмодзи в интерфейсе.
const PATHS = {
  chat: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 13.5z"/><path d="M8 8h8M8 11.5h5"/>',
  compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
  chart: '<path d="M4 19h16"/><path d="M6.5 15.5 10 11l3 2.5 4.5-6"/><circle cx="17.5" cy="7.5" r="1"/>',
  cards: '<rect x="3.5" y="7" width="12" height="13" rx="2"/><path d="M8 4h10.5A2 2 0 0 1 20.5 6v11.5"/>',
  sliders: '<path d="M5 6h9M18 6h1M5 12h3M12 12h7M5 18h11M20 18h-1"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="7" y="5.5" width="3.5" height="13" rx="1" fill="currentColor" stroke="none"/><rect x="13.5" y="5.5" width="3.5" height="13" rx="1" fill="currentColor" stroke="none"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  send: '<path d="M4 12 20 4l-5 16-3.5-6.5z"/><path d="m11.5 13.5 3.5-3.5"/>',
  gift: '<rect x="3.5" y="8.5" width="17" height="4" rx="1"/><path d="M5 12.5v7.5h14v-7.5M12 8.5V20"/><path d="M12 8.5c-1.5-3.5-5-3.5-5-1.5S10 8.5 12 8.5c2 0 5-0.5 5-1.5s-3.5-2-5 1.5z"/>',
  headphones: '<path d="M4 16v-3.5a8 8 0 0 1 16 0V16"/><rect x="3.5" y="14" width="4" height="6" rx="1.5"/><rect x="16.5" y="14" width="4" height="6" rx="1.5"/>',
  mask: '<path d="M4 6c5-2 11-2 16 0v5c0 5-3.5 8.5-8 8.5S4 16 4 11z"/><path d="M8 10.5h2M14 10.5h2M9.5 15c1.5 1 3.5 1 5 0"/>',
  pen: '<path d="M4 20l4-1 11-11-3-3L5 16z"/><path d="M14 7l3 3"/>',
  camera: '<path d="M4 8.5A1.5 1.5 0 0 1 5.5 7h2l1.5-2h6l1.5 2h2A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/><circle cx="12" cy="13" r="3.5"/>',
  book: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5zM20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z"/>',
};

export function icon(name, cls = "") {
  const span = document.createElement("span");
  span.className = `ico ${cls}`.trim();
  span.setAttribute("aria-hidden", "true");
  span.style.display = "inline-grid";
  // Статичные строки из этого модуля — не пользовательские данные.
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${PATHS[name] || ""}</svg>`;
  return span;
}
