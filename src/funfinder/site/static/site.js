// Фильтр «Когда» и скрытие прошедших дней.
// Страницы пересобираются, только когда меняется каталог, поэтому «сегодня»
// считается здесь, в браузере, по часовому поясу города.
(() => {
  const main = document.querySelector("main[data-tz]");
  const bar = document.querySelector(".when");
  if (!main || !bar) return;

  const ymd = new Intl.DateTimeFormat("en-CA", {
    timeZone: main.dataset.tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const shift = (iso, days) => {
    const d = new Date(`${iso}T12:00:00Z`);
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  };

  const today = ymd.format(new Date());
  const tomorrow = shift(today, 1);
  const weekday = new Date(`${today}T12:00:00Z`).getUTCDay(); // 0 — воскресенье
  const saturday = weekday === 0 ? shift(today, -1) : shift(today, 6 - weekday);
  const sunday = shift(saturday, 1);

  const days = [...document.querySelectorAll(".day")].filter((day) => {
    const date = day.dataset.date;
    if (date < today) {
      day.remove();
      return false;
    }
    const rel = day.querySelector(".day-rel");
    if (date === today) {
      day.classList.add("is-today");
      rel.textContent = "сегодня, ";
    } else if (date === tomorrow) {
      day.classList.add("is-tomorrow");
      rel.textContent = "завтра, ";
    }
    return true;
  });

  const filters = {
    all: () => true,
    today: (date) => date === today,
    tomorrow: (date) => date === tomorrow,
    weekend: (date) => date >= saturday && date <= sunday,
  };
  const emptyText = {
    all: "Будущих событий в этом разделе пока нет. Загляните позже: афиша обновляется по мере новых анонсов.",
    today: "На сегодня в этом разделе событий нет.",
    tomorrow: "На завтра в этом разделе событий пока нет.",
    weekend: "На эти выходные в этом разделе событий пока нет.",
  };
  const empty = document.querySelector(".empty");

  function show(key) {
    let visible = 0;
    for (const day of days) {
      day.hidden = !filters[key](day.dataset.date);
      if (!day.hidden) visible += 1;
    }
    for (const button of bar.querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.when === key));
    }
    empty.hidden = visible > 0;
    empty.querySelector(".empty-text").textContent = emptyText[key];
    empty.querySelector("button").hidden = key === "all";
    history.replaceState(null, "", key === "all" ? location.pathname : `#${key}`);
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-when]");
    if (button) show(button.dataset.when);
  });

  bar.hidden = false;
  const fromHash = location.hash.slice(1);
  show(fromHash in filters ? fromHash : "all");
})();
