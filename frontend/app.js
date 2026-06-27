// MedServicePrice.kz — фронтенд (vanilla JS, без сборки). Тот же origin, что и API.
const API = ""; // пусто = тот же хост (FastAPI отдаёт и статику, и API)

const $ = (id) => document.getElementById(id);
const fmt = (n) => (n == null ? "—" : n.toLocaleString("ru-RU"));
const compare = new Map(); // id -> offer

// ---------- инициализация ----------
async function init() {
  await Promise.all([loadStats(), loadFilters()]);
  bindEvents();
  runSearch();
}

async function loadStats() {
  try {
    const s = await fetch(`${API}/stats`).then((r) => r.json());
    $("stats").innerHTML = `
      ${statBox(s.total_offers, "предложений")}
      ${statBox(s.clinics, "клиник")}
      ${statBox(s.cities, "городов")}
      ${statBox(s.services_in_dictionary, "услуг в справочнике")}
      ${statBox(s.sources, "источников")}
    `;
  } catch (e) { /* база ещё пустая — не страшно */ }
}
const statBox = (num, lbl) =>
  `<div class="stat"><div class="num">${fmt(num)}</div><div class="lbl">${lbl}</div></div>`;

async function loadFilters() {
  const [cities, cats] = await Promise.all([
    fetch(`${API}/cities`).then((r) => r.json()).catch(() => []),
    fetch(`${API}/categories`).then((r) => r.json()).catch(() => []),
  ]);
  cities.forEach((c) => $("f-city").insertAdjacentHTML("beforeend", `<option>${c}</option>`));
  cats.forEach((c) => $("f-category").insertAdjacentHTML("beforeend", `<option>${c}</option>`));
}

// ---------- события ----------
function bindEvents() {
  $("searchBtn").onclick = runSearch;
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { hideAc(); runSearch(); }
  });
  $("q").addEventListener("input", debounce(autocomplete, 180));
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".searchbar")) hideAc();
  });
  ["f-city", "f-category", "f-sort", "f-stale"].forEach((id) => ($(id).onchange = runSearch));
  ["f-pmin", "f-pmax"].forEach((id) =>
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); }));
  $("resetBtn").onclick = resetFilters;
  $("parseBtn").onclick = triggerParse;
  $("cmpGo").onclick = showCompare;
  $("cmpClr").onclick = clearCompare;
  $("modalBack").onclick = (e) => { if (e.target.id === "modalBack") closeModal(); };
}

// ---------- автодополнение ----------
let acIndex = -1, acItems = [];
async function autocomplete() {
  const q = $("q").value.trim();
  if (q.length < 2) return hideAc();
  acItems = await fetch(`${API}/services/autocomplete?q=${encodeURIComponent(q)}`)
    .then((r) => r.json()).catch(() => []);
  if (!acItems.length) return hideAc();
  acIndex = -1;
  $("ac").innerHTML = acItems
    .map((s, i) => `<div class="ac-item" data-i="${i}">${s}</div>`).join("");
  $("ac").classList.add("show");
  $("ac").querySelectorAll(".ac-item").forEach((el) => {
    el.onclick = () => { $("q").value = acItems[el.dataset.i]; hideAc(); runSearch(); };
  });
}
$("q") && $("q").addEventListener("keydown", (e) => {
  if (!$("ac").classList.contains("show")) return;
  const items = $("ac").querySelectorAll(".ac-item");
  if (e.key === "ArrowDown") { acIndex = Math.min(acIndex + 1, items.length - 1); e.preventDefault(); }
  else if (e.key === "ArrowUp") { acIndex = Math.max(acIndex - 1, 0); e.preventDefault(); }
  else if (e.key === "Enter" && acIndex >= 0) { $("q").value = acItems[acIndex]; hideAc(); runSearch(); e.preventDefault(); return; }
  else return;
  items.forEach((el, i) => el.classList.toggle("active", i === acIndex));
});
function hideAc() { $("ac").classList.remove("show"); acIndex = -1; }

// ---------- поиск ----------
async function runSearch() {
  const params = new URLSearchParams();
  const q = $("q").value.trim();
  if (q) params.set("q", q);
  if ($("f-city").value) params.set("city", $("f-city").value);
  if ($("f-category").value) params.set("category", $("f-category").value);
  if ($("f-pmin").value) params.set("price_min", $("f-pmin").value);
  if ($("f-pmax").value) params.set("price_max", $("f-pmax").value);
  params.set("sort", $("f-sort").value);
  if ($("f-stale").checked) params.set("include_stale", "true");

  $("results").innerHTML = `<div class="loading">Загрузка…</div>`;
  try {
    const data = await fetch(`${API}/search?${params}`).then((r) => r.json());
    render(data.results);
    $("count").textContent = data.count ? `найдено: ${data.count}` : "";
  } catch (e) {
    $("results").innerHTML = `<div class="empty"><div class="big">⚠️</div>Не удалось загрузить данные.<br>Запущен ли backend?</div>`;
  }
}

function render(rows) {
  if (!rows || !rows.length) {
    $("results").innerHTML = `<div class="empty"><div class="big">🔍</div>Ничего не найдено.<br>Попробуйте изменить запрос или сбросить фильтры.</div>`;
    return;
  }
  $("results").innerHTML = rows.map(card).join("");
  $("results").querySelectorAll("[data-clinic]").forEach((el) => {
    el.onclick = () => openClinic(el.dataset.clinic);
  });
  $("results").querySelectorAll(".cmp input").forEach((el) => {
    el.onchange = () => toggleCompare(el, rows);
  });
}

function card(o) {
  const badges = [`<span class="badge cat">${o.category || "—"}</span>`];
  if (o.is_cheapest) badges.push(`<span class="badge best">★ лучшая цена</span>`);
  if (o.is_stale) badges.push(`<span class="badge stale">устарело &gt;30 дн.</span>`);
  const updated = o.updated_days_ago === 0 ? "сегодня"
    : o.updated_days_ago === 1 ? "вчера"
    : o.updated_days_ago != null ? `${o.updated_days_ago} дн. назад` : "—";
  const meta = [
    o.city ? `📍 ${o.city}${o.address ? ", " + o.address : ""}` : "",
    o.working_hours ? `🕑 ${o.working_hours}` : "",
    `🔄 обновлено ${updated}`,
    o.source_url ? `<a href="${o.source_url}" target="_blank" rel="noopener">источник ↗</a>` : "",
  ].filter(Boolean).join(" · ");

  return `
    <div class="card ${o.is_cheapest ? "cheapest" : ""}">
      <div class="card-main">
        <div class="svc">${o.service_name || o.service_name_raw}</div>
        <div class="clinic"><button data-clinic="${encodeURIComponent(o.clinic_name)}">${o.clinic_name}</button></div>
        <div class="meta">${meta}</div>
        <div class="badges">${badges.join("")}</div>
      </div>
      <div class="card-side">
        <div class="price">${fmt(o.price_kzt)} <span>₸</span></div>
        <label class="cmp"><input type="checkbox" data-id="${o.id}" ${compare.has(o.id) ? "checked" : ""}/> сравнить</label>
      </div>
    </div>`;
}

// ---------- сравнение ----------
function toggleCompare(el, rows) {
  const id = +el.dataset.id;
  if (el.checked) {
    const o = rows.find((r) => r.id === id);
    if (o) compare.set(id, o);
  } else compare.delete(id);
  updateCmpBar();
}
function updateCmpBar() {
  $("cmpCount").textContent = `Выбрано: ${compare.size}`;
  $("cmpBar").classList.toggle("show", compare.size > 0);
}
function clearCompare() {
  compare.clear(); updateCmpBar();
  document.querySelectorAll(".cmp input").forEach((el) => (el.checked = false));
}
function showCompare() {
  const items = [...compare.values()];
  const minPrice = Math.min(...items.map((o) => o.price_kzt));
  const rows = items.map((o) => `
    <tr class="${o.price_kzt === minPrice ? "min" : ""}">
      <td>${o.clinic_name}</td>
      <td>${o.service_name || o.service_name_raw}</td>
      <td>${o.city || "—"}</td>
      <td class="num">${fmt(o.price_kzt)} ₸</td>
      <td>${o.updated_days_ago != null ? o.updated_days_ago + " дн." : "—"}</td>
    </tr>`).join("");
  openModal(`
    <button class="modal-close" onclick="closeModal()">✕</button>
    <h2>Сравнение предложений</h2>
    <div class="sub">${items.length} предложений · зелёным выделена минимальная цена</div>
    <table>
      <thead><tr><th>Клиника</th><th>Услуга</th><th>Город</th><th>Цена</th><th>Обновлено</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`);
}

// ---------- карточка клиники ----------
async function openClinic(encoded) {
  const name = decodeURIComponent(encoded);
  openModal(`<div class="loading">Загрузка…</div>`);
  try {
    const c = await fetch(`${API}/clinic/${encodeURIComponent(name)}`).then((r) => r.json());
    const rows = c.services.map((s) => `
      <tr>
        <td>${s.service_name || s.service_name_raw}</td>
        <td>${s.category || "—"}</td>
        <td class="num">${fmt(s.price_kzt)} ₸</td>
      </tr>`).join("");
    openModal(`
      <button class="modal-close" onclick="closeModal()">✕</button>
      <h2>${c.clinic_name}</h2>
      <div class="sub">
        ${c.city ? "📍 " + c.city : ""}${c.address ? ", " + c.address : ""}
        ${c.phone ? " · ☎ " + c.phone : ""}${c.working_hours ? " · 🕑 " + c.working_hours : ""}
      </div>
      <table>
        <thead><tr><th>Услуга</th><th>Категория</th><th>Цена</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`);
  } catch (e) {
    openModal(`<button class="modal-close" onclick="closeModal()">✕</button><div class="empty">Не удалось загрузить карточку клиники.</div>`);
  }
}

// ---------- админ: запуск парсинга ----------
async function triggerParse() {
  const btn = $("parseBtn");
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "⟳ Парсинг запущен…";
  $("adminMsg").textContent = "Выполняется parse → normalize, это может занять до минуты…";
  try {
    const res = await fetch(`${API}/admin/trigger-parse`, { method: "POST" }).then((r) => r.json());
    let msg = `Готово: ${res.status}\nНовых записей: ${res.new_records}`;
    if (res.errors && res.errors.length) msg += `\nОшибки/предупреждения:\n• ` + res.errors.slice(0, 5).join("\n• ");
    $("adminMsg").textContent = msg;
    await loadStats();
    runSearch();
  } catch (e) {
    $("adminMsg").textContent = "Ошибка вызова /admin/trigger-parse.";
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ---------- утилиты ----------
function resetFilters() {
  $("q").value = ""; $("f-city").value = ""; $("f-category").value = "";
  $("f-pmin").value = ""; $("f-pmax").value = ""; $("f-sort").value = "price_asc";
  $("f-stale").checked = false;
  runSearch();
}
function openModal(html) { $("modal").innerHTML = html; $("modalBack").classList.add("show"); }
function closeModal() { $("modalBack").classList.remove("show"); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

window.closeModal = closeModal;
init();
