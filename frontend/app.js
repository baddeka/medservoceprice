// MedServicePrice.kz — фронтенд (vanilla JS, без сборки).
// По умолчанию API на том же origin (сайт открыт через сервер). Но если файл
// открыли напрямую с диска (file://) — берём задеплоенный backend, чтобы данные
// всё равно подгружались и страница не была пустой.
const API = (location.protocol === "file:" || !location.host)
  ? "https://medservoceprice.onrender.com"
  : "";
const $ = (id) => document.getElementById(id);
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("ru-RU"));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const CATS = {
  "лаборатория": { ic: "🧪", cls: "ic-lab" },
  "приём врача": { ic: "🩺", cls: "ic-doc" },
  "диагностика": { ic: "🩻", cls: "ic-diag" },
  "процедура":   { ic: "💉", cls: "ic-proc" },
};
const catIcon = (c) => (CATS[c] || { ic: "🩺", cls: "ic-doc" });
let activeCategory = "";

// ---------------- init ----------------
async function init() {
  await Promise.all([loadStats(), loadCities()]);
  buildChips();
  bindEvents();
  runSearch();
}

async function loadStats() {
  try {
    const s = await fetch(`${API}/stats`).then((r) => r.json());
    $("topstats").innerHTML =
      ts(s.total_offers, "цен") + ts(s.clinics, "клиник") +
      ts(s.cities, "городов") + ts(s.services_in_dictionary, "услуг");
  } catch (e) {}
}
const ts = (n, l) => `<div class="ts"><b>${fmt(n)}</b><span>${l}</span></div>`;

async function loadCities() {
  const cities = await fetch(`${API}/cities`).then((r) => r.json()).catch(() => []);
  cities.forEach((c) => $("f-city").insertAdjacentHTML("beforeend", `<option>${esc(c)}</option>`));
}

function buildChips() {
  const chips = [["", "Все услуги"], ["лаборатория", "🧪 Анализы"], ["приём врача", "🩺 Врачи"],
                 ["диагностика", "🩻 Диагностика"], ["процедура", "💉 Процедуры"]];
  $("chips").innerHTML = chips.map(([v, t]) =>
    `<div class="chip ${v === activeCategory ? "active" : ""}" data-cat="${v}">${t}</div>`).join("");
  $("chips").querySelectorAll(".chip").forEach((el) => {
    el.onclick = () => {
      activeCategory = el.dataset.cat;
      buildChips();
      runSearch();
    };
  });
}

// ---------------- events ----------------
function bindEvents() {
  $("searchBtn").onclick = () => { hideAc(); runSearch(); };
  $("q").addEventListener("input", debounce(autocomplete, 170));
  $("q").addEventListener("keydown", acKeyNav);
  document.addEventListener("click", (e) => { if (!e.target.closest(".searchbar")) hideAc(); });
  ["f-city", "f-sort", "f-stale"].forEach((id) => ($(id).onchange = runSearch));
  ["f-pmin", "f-pmax"].forEach((id) => $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); }));
  $("resetBtn").onclick = resetFilters;
  $("parseBtn").onclick = refreshData;
  $("modalBack").onclick = (e) => { if (e.target.id === "modalBack") closeModal(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
}

// ---------------- autocomplete ----------------
let acItems = [], acIndex = -1;
async function autocomplete() {
  const q = $("q").value.trim();
  if (q.length < 2) return hideAc();
  acItems = await fetch(`${API}/services/autocomplete?q=${encodeURIComponent(q)}`).then((r) => r.json()).catch(() => []);
  if (!acItems.length) return hideAc();
  acIndex = -1;
  $("ac").innerHTML = acItems.map((s, i) =>
    `<div class="ac-item" data-i="${i}"><span class="ac-ic">🔍</span>${esc(s)}</div>`).join("");
  $("ac").classList.add("show");
  $("ac").querySelectorAll(".ac-item").forEach((el) =>
    (el.onclick = () => { $("q").value = acItems[el.dataset.i]; hideAc(); runSearch(); }));
}
function acKeyNav(e) {
  if (e.key === "Enter") { hideAc(); runSearch(); return; }
  if (!$("ac").classList.contains("show")) return;
  const items = $("ac").querySelectorAll(".ac-item");
  if (e.key === "ArrowDown") acIndex = Math.min(acIndex + 1, items.length - 1);
  else if (e.key === "ArrowUp") acIndex = Math.max(acIndex - 1, 0);
  else return;
  e.preventDefault();
  items.forEach((el, i) => el.classList.toggle("active", i === acIndex));
  if (items[acIndex]) $("q").value = acItems[acIndex];
}
function hideAc() { $("ac").classList.remove("show"); acIndex = -1; }

// ---------------- search (grouped by service) ----------------
async function runSearch() {
  const p = new URLSearchParams();
  const q = $("q").value.trim();
  if (q) p.set("q", q);
  if ($("f-city").value) p.set("city", $("f-city").value);
  if (activeCategory) p.set("category", activeCategory);
  if ($("f-pmin").value) p.set("price_min", $("f-pmin").value);
  if ($("f-pmax").value) p.set("price_max", $("f-pmax").value);
  p.set("sort", $("f-sort").value);
  if ($("f-stale").checked) p.set("include_stale", "true");

  $("resultsTitle").textContent = q ? `Результаты: «${q}»` : (activeCategory ? cap(activeCategory) : "Популярные услуги");
  $("results").innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
  try {
    const data = await fetch(`${API}/services?${p}`).then((r) => r.json());
    renderServices(data.results);
    $("count").textContent = data.count ? `${data.count} услуг` : "";
  } catch (e) {
    $("results").innerHTML = `<div class="empty"><div class="big">⚠️</div>
      Не удалось загрузить данные.<br>
      Откройте сайт по ссылке <a href="https://medservoceprice.onrender.com">medservoceprice.onrender.com</a>,
      а не файлом с диска.</div>`;
  }
}

function renderServices(rows) {
  if (!rows || !rows.length) {
    $("results").innerHTML = `<div class="empty"><div class="big">🔍</div>Ничего не найдено.<br>Измените запрос или сбросьте фильтры.</div>`;
    $("count").textContent = "";
    return;
  }
  $("results").innerHTML = rows.map(svcCard).join("");
  $("results").querySelectorAll(".svc-card").forEach((el) =>
    (el.onclick = () => openService(el.dataset.id)));
}

function svcCard(s) {
  const ic = catIcon(s.category);
  const place = s.clinic_count + " " + plural(s.clinic_count, "клиника", "клиники", "клиник")
    + (s.city_count > 1 ? ` · ${s.city_count} ${plural(s.city_count, "город", "города", "городов")}` : "");
  return `
    <div class="svc-card" data-id="${s.service_id}">
      <div class="svc-ic ${ic.cls}">${ic.ic}</div>
      <div class="svc-body">
        <div class="svc-name">${esc(s.canonical_name)}</div>
        <div class="svc-meta">
          <span class="svc-cat-badge">${esc(s.category || "услуга")}</span>
          <span>🏥 ${place}</span>
          ${s.turnaround ? `<span>⏱ ${esc(s.turnaround)}</span>` : ""}
        </div>
      </div>
      <div class="svc-price">
        <div class="from">от</div>
        <div class="val">${fmt(s.min_price)} <span>₸</span></div>
        <div class="chev">Подробнее →</div>
      </div>
    </div>`;
}

// ---------------- service detail (всё на нашем сайте) ----------------
async function openService(id) {
  openModal(`<div class="loading">Загрузка…</div>`);
  const p = new URLSearchParams();
  if ($("f-city").value) p.set("city", $("f-city").value);
  if ($("f-stale").checked) p.set("include_stale", "true");
  try {
    const d = await fetch(`${API}/service/${id}?${p}`).then((r) => r.json());
    const ic = catIcon(d.category);
    const tile = (k, v) => v && v !== "—" ? `<div class="info-tile"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>` : "";
    const clinics = d.offers.map((o) => clinicRow(o, d.turnaround)).join("") ||
      `<div class="empty" style="padding:30px">Пока нет актуальных предложений по этой услуге.</div>`;
    openModal(`
      <button class="modal-close" onclick="closeModal()">✕</button>
      <div class="svc-detail-head">
        <div class="cat">${ic.ic} ${esc(d.category || "услуга")}</div>
        <h2>${esc(d.canonical_name)}</h2>
        <div class="price-line">${d.clinic_count
          ? `${d.clinic_count} ${plural(d.clinic_count, "клиника", "клиники", "клиник")} · цена <b>от ${fmt(d.min_price)} ₸</b> до ${fmt(d.max_price)} ₸`
          : "нет актуальных предложений"}</div>
      </div>
      <div class="info-grid">
        ${tile("Биоматериал", d.biomaterial)}
        ${tile("Подготовка", d.preparation)}
        ${tile("Срок выполнения", d.turnaround)}
      </div>
      ${d.description ? `<div class="svc-desc">${esc(d.description)}</div>` : ""}
      <div class="clinics-head"><span>Где сдать дешевле</span><span class="hint">отсортировано по цене · зелёным — минимальная</span></div>
      <div class="clinic-list">${clinics}</div>
    `);
    $("modal").querySelectorAll("[data-clinic]").forEach((el) =>
      (el.onclick = () => openClinic(el.dataset.clinic)));
  } catch (e) {
    openModal(`<button class="modal-close" onclick="closeModal()">✕</button><div class="empty">Не удалось загрузить услугу.</div>`);
  }
}

function clinicRow(o, turnaround) {
  const upd = o.updated_days_ago === 0 ? "сегодня" : o.updated_days_ago === 1 ? "вчера"
    : o.updated_days_ago != null ? `${o.updated_days_ago} дн. назад` : "—";
  const tags = (o.is_cheapest ? `<span class="tag best">лучшая цена</span> ` : "")
    + (o.is_stale ? `<span class="tag stale">устарело</span>` : "");
  const stars = o.rating != null
    ? `<span class="cl-rating">★ ${o.rating} <span class="rc">· ${fmt(o.reviews_count)} ${plural(o.reviews_count, "отзыв", "отзыва", "отзывов")}</span></span>`
    : "";
  const meta = [
    o.city ? `📍 ${esc(o.city)}${o.address ? ", " + esc(o.address) : ""}` : "",
    o.working_hours ? `🕑 ${esc(o.working_hours)}` : "",
    turnaround ? `📋 результат через ${esc(turnaround)}` : "",
    `🔄 обновлено ${upd}`,
  ].filter(Boolean).join(" · ");
  const enc = encodeURIComponent(o.clinic_name);
  return `
    <div class="clinic-row ${o.is_cheapest ? "best" : ""}">
      <div>
        <div class="cl-name"><button data-clinic="${enc}">${esc(o.clinic_name)}</button> ${stars} ${tags}</div>
        <div class="cl-meta">${meta}</div>
        <button class="cl-more" data-clinic="${enc}">Другие услуги этой клиники →</button>
      </div>
      <div class="cl-price"><div class="p">${fmt(o.price_kzt)} <span>₸</span></div></div>
    </div>`;
}

// ---------------- clinic card ----------------
async function openClinic(encoded) {
  const name = decodeURIComponent(encoded);
  openModal(`<div class="loading">Загрузка…</div>`);
  try {
    const c = await fetch(`${API}/clinic/${encodeURIComponent(name)}`).then((r) => r.json());
    const rows = c.services.map((s) => `
      <tr><td>${esc(s.service_name || s.service_name_raw)}</td><td>${esc(s.category || "—")}</td>
      <td class="num">${fmt(s.price_kzt)} ₸</td></tr>`).join("");
    const ratingLine = c.rating != null
      ? `<span class="cl-rating big">★ ${c.rating}</span> <span class="rc">${fmt(c.reviews_count)} ${plural(c.reviews_count, "отзыв", "отзыва", "отзывов")}</span>`
      : "";
    const reviews = (c.reviews || []).map((r) => `
      <div class="review">
        <div class="review-top"><b>${esc(r.author)}</b><span class="review-stars">${"★".repeat(r.stars)}${"☆".repeat(5 - r.stars)}</span></div>
        <div class="review-text">${esc(r.text)}</div>
      </div>`).join("");
    openModal(`
      <button class="modal-close" onclick="closeModal()">✕</button>
      <div class="clinic-modal-head">
        <h2>${esc(c.clinic_name)}</h2>
        <div class="rating-row">${ratingLine}</div>
        <div class="sub">${[c.city ? "📍 " + esc(c.city) + (c.address ? ", " + esc(c.address) : "") : "",
          c.phone ? "☎ " + esc(c.phone) : "", c.working_hours ? "🕑 " + esc(c.working_hours) : ""].filter(Boolean).join(" · ")}</div>
      </div>
      ${reviews ? `<div class="reviews-block"><div class="block-title">Отзывы пациентов</div>${reviews}</div>` : ""}
      <div class="block-title pad">Все услуги клиники (${c.services.length})</div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Услуга</th><th>Категория</th><th style="text-align:right">Цена</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`);
  } catch (e) {
    openModal(`<button class="modal-close" onclick="closeModal()">✕</button><div class="empty">Не удалось загрузить клинику.</div>`);
  }
}

// ---------------- обновление данных ----------------
async function refreshData() {
  const btn = $("parseBtn"), prev = btn.textContent;
  btn.disabled = true; btn.textContent = "↻ Обновляем…";
  $("adminMsg").textContent = "Проверяем источники и подтягиваем свежие цены… (до минуты)";
  try {
    const res = await fetch(`${API}/admin/trigger-parse`, { method: "POST" }).then((r) => r.json());
    let msg = res.new_records > 0
      ? `Готово. Добавлено свежих цен: ${res.new_records}.`
      : "Готово. Новых цен нет — данные уже актуальны.";
    if (res.errors && res.errors.length) msg += `\nНедоступные источники: ${res.errors.length} (пропущены).`;
    $("adminMsg").textContent = msg;
    await loadStats();
    runSearch();
  } catch (e) {
    $("adminMsg").textContent = "Не удалось обновить данные.";
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}

// ---------------- utils ----------------
function resetFilters() {
  $("q").value = ""; $("f-city").value = ""; $("f-pmin").value = ""; $("f-pmax").value = "";
  $("f-sort").value = "price_asc"; $("f-stale").checked = false; activeCategory = ""; buildChips(); runSearch();
}
function openModal(html) { $("modal").innerHTML = html; $("modalBack").classList.add("show"); document.body.style.overflow = "hidden"; }
function closeModal() { $("modalBack").classList.remove("show"); document.body.style.overflow = ""; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}
window.closeModal = closeModal;
init();
