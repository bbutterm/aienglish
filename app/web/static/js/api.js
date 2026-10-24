// Запросы к API: каждый подписан initData из Telegram.
import { tg } from "./ui.js";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body, { raw = false } = {}) {
  const headers = { Authorization: `tma ${tg?.initData || ""}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "Нет связи с сервером. Проверь интернет и попробуй ещё раз.");
  }
  if (!response.ok) {
    let detail = {};
    try {
      detail = (await response.json()).detail || {};
    } catch {
      /* тело без JSON */
    }
    const message = typeof detail === "object" && detail.message ? detail.message : "Что-то пошло не так.";
    throw new ApiError(response.status, detail.code || "error", message);
  }
  return raw ? response.blob() : response.json();
}

export const api = {
  me: () => request("GET", "/api/me"),
  today: () => request("GET", "/api/lessons/today"),
  createLesson: () => request("POST", "/api/lessons"),
  answer: (id, index, choice) => request("POST", `/api/lessons/${id}/answers`, { index, choice }),
  audio: (id, index) => request("GET", `/api/lessons/${id}/audio/${index}`, undefined, { raw: true }),
  complete: (id) => request("POST", `/api/lessons/${id}/complete`),
  progress: () => request("GET", "/api/progress"),
  vocab: () => request("GET", "/api/vocab"),
  review: (id, grade) => request("POST", `/api/vocab/${id}/review`, { grade }),
  admin: {
    overview: (days = 14) => request("GET", `/api/admin/overview?days=${days}`),
    users: (q = "", offset = 0) =>
      request("GET", `/api/admin/users?q=${encodeURIComponent(q)}&offset=${offset}`),
    user: (id) => request("GET", `/api/admin/users/${id}`),
    premium: (id, days) => request("POST", `/api/admin/users/${id}/premium`, { days, notify: true }),
    block: (id, blocked) => request("POST", `/api/admin/users/${id}/block`, { blocked }),
  },
};
