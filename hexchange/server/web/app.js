import { HexMap, api, fmt, priceColor, sparkline } from "/static/map.js?v=5";

const $ = sel => document.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k instanceof Node ? k : String(k));
  return e;
};

const DEFAULT_PARAMS = {
  war: { capacity: 0.1, risk: 0.15, demand: 2.0, production: 0.85 },
  embargo: { against: [] },
  tariff: { against: [], rate: 0.25 },
  lane_disruption: { factor: 0 },
  piracy: { risk: 0.1 },
  disaster: { production: 0.5, demand: 1.0 },
  boom: { production: 1.3, demand: 1.3 },
  relations: { value: -0.5 },
  legality: { status: "illegal" },
  modifier: { field: "production", op: "mul", value: 1.5 },
  player_action: { field: "demand", op: "mul", value: 1.5 },
};

let camp = null, map = null, goods = [], goodName = {}, currency = "";
let selected = null, lastRoutes = [];

function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.style.borderColor = bad ? "var(--bad)" : "";
  t.classList.add("show"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); console.error(e); } }

// ------------------------------------------------------------------ boot
async function boot() {
  map = new HexMap($("#map"), { onSelect: onSelect, onLane: showLane });
  wireHeader(); wireTabs(); wireOverlay(); await loadSettings();
  try { setCampaign(await api("/api/campaign")); }
  catch { $("#genDlg").showModal(); }
}

function setCampaign(data) {
  window.__hxlog?.("boot", { systems: data.systems.length, tick: data.tick });
  camp = data; goods = data.setting.goods; currency = data.setting.currency;
  goodName = Object.fromEntries(goods.map(g => [g.id, g.name]));
  $("#campName").textContent = data.name;
  $("#tick").textContent = `${data.setting.time_unit} ${data.tick}`;
  const gs = $("#goodSel"); const prev = gs.value;
  gs.replaceChildren(h("option", { value: "" }, "All goods (value)"), ...goods.map(g => h("option", { value: g.id }, g.name)));
  gs.value = goods.some(g => g.id === prev) ? prev : (goods[0]?.id || "");
  map.load(data);
  selected = null;
  refreshOverlay(); renderEvents(); renderRoutesForm(); renderPolitics(); renderPlayers();
  $("#pane-system").replaceChildren(h("p", { class: "muted" }, "Click a system on the map. Shift-click to select several (for events)."),
    data.setting.disclaimer ? h("p", { class: "disclaimer" }, data.setting.disclaimer) : null);
}

async function refreshCampaign() {
  const data = await api("/api/campaign");
  camp = data;
  $("#tick").textContent = `${data.setting.time_unit} ${data.tick}`;
}

// ------------------------------------------------------------------ header
function wireHeader() {
  const stepButtons = ["#step1", "#step4", "#stepGo", "#genBtn", "#loadBtn"].map(s => $(s));
  const setBusy = (busy, text = "") => {
    for (const b of stepButtons) b.disabled = busy;
    $("#busy").textContent = text;
  };
  const step = async n => guard(async () => {
    if ($("#step1").disabled) return;
    setBusy(true, `Simulating 0/${n}…`);
    try {
      await api("/api/step", { method: "POST", body: { weeks: n } });
      let s;
      do {                                   // poll the background job
        await new Promise(r => setTimeout(r, 300));
        s = await api("/api/status");
        setBusy(true, `Simulating ${s.done}/${s.total}…`);
        $("#tick").textContent = `${camp.setting.time_unit} ${s.tick}`;
      } while (s.running);
      if (s.error) throw new Error(s.error);
      await refreshCampaign(); await refreshOverlay(); renderEvents();
      if (selected) await showSystem(selected);
      toast(`Advanced to ${camp.setting.time_unit} ${s.tick}`);
    } finally { setBusy(false); }
  });
  $("#step1").onclick = () => step(1);
  $("#step4").onclick = () => step(4);
  $("#stepGo").onclick = () => step(Number($("#stepN").value) || 1);
  $("#saveBtn").onclick = () => {
    $("#savePath").value = camp?.path || `~/campaigns/${(camp?.name || "sector").replace(/[^\w.-]+/g, "_")}.hexchange.json`;
    $("#saveInfo").textContent = camp?.path
      ? `Changes are saved automatically to ${camp.path}. Save here to write it now, or enter a new path to save a copy elsewhere.`
      : "This campaign has no file yet; choose where to save it. It will autosave there afterwards.";
    $("#saveDlg").showModal();
  };
  $("#downloadBtn").onclick = () => { window.location.href = "/api/campaign/download"; };
  $("#saveDlg").addEventListener("close", () => {
    if ($("#saveDlg").returnValue !== "ok") return;
    guard(async () => {
      const r = await api("/api/campaign/save", { method: "POST", body: { path: $("#savePath").value.trim() } });
      camp.path = r.path; toast(`Saved to ${r.path}`);
    });
  });
  $("#loadBtn").onclick = () => { $("#loadPath").value = camp?.path || ""; $("#loadDlg").showModal(); };
  $("#loadDlg").addEventListener("close", () => {
    if ($("#loadDlg").returnValue !== "ok") return;
    guard(async () => setCampaign(await api("/api/campaign/load", { method: "POST", body: { path: $("#loadPath").value } })));
  });
  $("#genBtn").onclick = () => $("#genDlg").showModal();
  $("#genDlg").addEventListener("close", () => {
    if ($("#genDlg").returnValue !== "ok") return;
    const f = new FormData($("#genForm"));
    const body = Object.fromEntries(f.entries());
    for (const k of ["width", "height", "polities", "seed", "warmup"]) body[k] = Number(body[k]);
    body.density = Number(body.density);
    if (!body.path) delete body.path;
    $("#busy").textContent = "Generating sector…";
    $("#genBtn").disabled = true;
    guard(async () => { setCampaign(await api("/api/generate", { method: "POST", body })); toast("Sector generated"); })
      .finally(() => { $("#busy").textContent = ""; $("#genBtn").disabled = false; });
  });
}

async function loadSettings() {
  const list = await api("/api/settings");
  const sel = $("#settingSel");
  sel.replaceChildren(...list.map(s => h("option", { value: s.name }, s.title)));
  const desc = () => { $("#settingDesc").textContent = list.find(s => s.name === sel.value)?.description || ""; };
  sel.onchange = desc; desc();
}

// ------------------------------------------------------------------ tabs
function wireTabs() {
  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => showTab(b.dataset.tab);
}
function showTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === name);
  for (const p of document.querySelectorAll(".pane")) p.classList.toggle("on", p.id === `pane-${name}`);
}

// ------------------------------------------------------------------ overlay
function wireOverlay() {
  $("#mode").onchange = refreshOverlay;
  $("#goodSel").onchange = refreshOverlay;
}

async function refreshOverlay() {
  if (!camp) return;
  const mode = $("#mode").value, good = $("#goodSel").value;
  const legend = $("#legend");
  $("#goodSel").style.display = mode === "polity" ? "none" : "";
  if (mode === "polity") {
    map.colorByPolity(); map.clearFlows();
    legend.replaceChildren(h("strong", {}, "Polities"),
      ...camp.polities.map(p => h("div", {}, h("span", { style: `color:${p.color}` }, "● "), p.name)),
      h("div", { class: "muted" }, "● unaligned"));
  } else if (mode === "price") {
    if (!good) { $("#goodSel").value = goods[0].id; }
    const g = $("#goodSel").value;
    const prices = await api(`/api/prices?good=${encodeURIComponent(g)}`);
    map.colorByPrice(prices); map.clearFlows();
    const stops = [0.35, 0.6, 1, 1.6, 2.8].map(r => priceColor(r)).join(",");
    legend.replaceChildren(h("strong", {}, `${goodName[g]} — price vs base ${fmt(prices.base_price)} ${currency}`),
      h("div", { class: "bar", style: `background:linear-gradient(90deg,${stops})` }),
      h("div", { class: "row" }, h("span", {}, "×0.35 cheap"), h("span", {}, "×1"), h("span", {}, "×2.8 dear")),
      h("div", { class: "muted" }, h("span", { style: "color:#9b59b6" }, "● "), "contraband here"));
  } else {
    const flows = await api(`/api/flows${good ? `?good=${encodeURIComponent(good)}` : ""}`);
    map.colorByPolity(); map.showFlows(flows);
    legend.replaceChildren(h("strong", {}, `Trade flows — ${good ? goodName[good] + " (units/wk)" : "all goods (value/wk)"}`),
      h("div", {}, h("span", { style: "color:#6cb2ff" }, "━ "), `charted lanes (${Object.keys(flows.lanes).length})`),
      h("div", {}, h("span", { style: "color:#c77dff" }, "┅ "), `smuggling, off-lane (${Object.keys(flows.smuggling).length})`));
  }
}

// ------------------------------------------------------------------ system panel
function onSelect(id, additive) {
  if (additive) { renderEventsTargets(); toast(`${map.multi.size} systems in multi-selection`); return; }
  selected = id; showTab("system"); showSystem(id); renderRoutesForm();
}

async function showSystem(id) {
  const d = await api(`/api/system/${id}`);
  const attrDefs = Object.fromEntries(camp.setting.attributes.map(a => [a.key, a]));
  const codeName = Object.fromEntries(camp.setting.codes.map(c => [c.code, c.name]));
  const pol = camp.polities.find(p => p.id === d.polity);
  const visible = camp.player_view.visible_systems.includes(id);
  const pane = $("#pane-system");
  const marketRows = d.market.map(m => {
    const ratio = m.price / m.base_price;
    const qty = h("input", { type: "number", value: 10, min: 0, step: 1 });
    const tradeBtn = (sign, label) => h("button", { onclick: () => guard(async () => {
      const t = await api("/api/trade", { method: "POST", body: { system: id, good: m.good, quantity: sign * Number(qty.value), note: "" } });
      toast(`${sign > 0 ? "Bought" : "Sold"} ${Math.abs(t.quantity)} ${goodName[m.good]} @ ${fmt(t.price)} ${currency}`);
      showSystem(id); renderPlayers();
    }) }, label);
    return h("tr", {},
      h("td", {}, goodName[m.good], m.legal ? "" : h("span", { class: "pill warn" }, "illegal")),
      h("td", { class: ratio > 1.05 ? "up" : ratio < 0.95 ? "down" : "" }, fmt(m.price), h("div", { class: "muted" }, `×${ratio.toFixed(2)}`)),
      h("td", {}, fmt(m.buy)), h("td", {}, fmt(m.sell)), h("td", {}, fmt(m.stock, 1)),
      h("td", {}, sparkline(m.history, { width: 60, height: 20 })),
      h("td", {}, qty, " ", tradeBtn(1, "Buy"), " ", tradeBtn(-1, "Sell")));
  });
  const notes = h("textarea", {}, d.notes || "");
  pane.replaceChildren(
    h("h2", {}, `${d.name} `, h("span", { class: "muted mono" }, `${d.id} · ${d.profile}`)),
    h("div", {}, pol ? h("span", { class: "pill", style: `border-color:${pol.color};color:${pol.color}` }, pol.name) : h("span", { class: "pill" }, "Unaligned"),
      ...d.codes.map(c => h("span", { class: "pill", title: codeName[c] || c }, codeName[c] || c)),
      ...d.events.map(e => h("span", { class: "pill warn" }, `event: ${e}`))),
    h("div", { class: "row" },
      h("button", { onclick: () => map.centerOn(id) }, "Centre map"),
      h("button", { onclick: () => togglePlayerVisible(id) }, visible ? "Hide from players" : "Show to players")),
    h("h3", {}, "Market"),
    h("table", {}, h("tr", {}, h("th", {}, "Good"), h("th", {}, `Price ${currency}`), h("th", {}, "Buy"), h("th", {}, "Sell"),
      h("th", {}, "Stock"), h("th", {}, "Trend"), h("th", {}, "Trade (t)")), ...marketRows),
    h("h3", {}, "World"),
    h("div", { class: "grid2" }, ...Object.entries(d.attrs).flatMap(([k, v]) => {
      const a = attrDefs[k]; const desc = a?.descriptions?.[String(v)];
      return [h("span", { class: "muted" }, a?.name || k), h("span", {}, String(v), desc ? h("span", { class: "muted" }, ` — ${desc}`) : null)];
    })),
    h("h3", {}, "Lanes"),
    h("div", {}, ...d.lanes.map(l => h("span", { class: "pill", style: "cursor:pointer", onclick: () => map.selectLane(l.id) },
      `${l.a === id ? l.b : l.a} (${l.length} ${camp.setting.distance_unit})`))),
    h("h3", {}, "DM notes"), notes,
    h("div", { class: "row" }, h("button", { onclick: () => guard(async () => {
      await api(`/api/system/${id}/notes`, { method: "PUT", body: { notes: notes.value } }); toast("Notes saved");
    }) }, "Save notes")),
  );
}

async function showLane(id) {
  const ln = camp.lanes.find(l => l.id === id);
  if (!ln) return;
  showTab("system");
  const flows = await api("/api/flows");
  const a = map.byId[ln.a], b = map.byId[ln.b];
  $("#pane-system").replaceChildren(
    h("h2", {}, `Lane ${a.name} — ${b.name}`),
    h("div", { class: "grid2" },
      h("span", { class: "muted" }, "Length"), h("span", {}, `${ln.length} ${camp.setting.distance_unit}`),
      h("span", { class: "muted" }, "Capacity"), h("span", {}, `${fmt(ln.capacity)} tons/${camp.setting.time_unit}`),
      h("span", { class: "muted" }, "Base risk"), h("span", {}, `${(ln.risk * 100).toFixed(1)}%`),
      h("span", { class: "muted" }, "Trade value"), h("span", {}, `${fmt(Math.abs(flows.lanes[id] || 0))} ${currency}/${camp.setting.time_unit}`)),
    h("div", { class: "row" },
      h("button", { onclick: () => { map.select(ln.a); } }, a.name), h("button", { onclick: () => map.select(ln.b) }, b.name),
      h("button", { onclick: () => { showTab("events"); renderEventsTargets(); } }, "Use in event…")));
}

// ------------------------------------------------------------------ events
async function renderEvents() {
  if (!camp) return;
  const list = await api("/api/events");
  const pane = $("#pane-events");
  const typeSel = h("select", {}, ...Object.keys(DEFAULT_PARAMS).map(t => h("option", { value: t }, t.replace("_", " "))));
  const help = h("p", { class: "muted" });
  const params = h("textarea", { class: "mono", rows: 4 });
  const setType = () => { help.textContent = camp.event_help[typeSel.value] || ""; params.value = JSON.stringify(DEFAULT_PARAMS[typeSel.value]); };
  typeSel.onchange = setType;
  const name = h("input", { placeholder: "Event name", style: "width:100%" });
  const start = h("input", { type: "number", value: camp.tick });
  const dur = h("input", { type: "number", placeholder: "∞", min: 1 });
  const source = h("select", {}, h("option", { value: "dm" }, "DM"), h("option", { value: "player" }, "Players"));
  const polBoxes = camp.polities.map(p => h("label", { class: "pill" }, h("input", { type: "checkbox", value: p.id }), " ", p.name));
  const goodsSel = h("select", { multiple: true, size: 4, style: "width:100%" }, ...goods.map(g => h("option", { value: g.id }, g.name)));
  const tags = h("input", { placeholder: "tags, comma separated (e.g. military)", style: "width:100%" });
  const targetsBox = h("div", { id: "evTargets", class: "muted" });
  setType();
  pane.replaceChildren(
    h("h2", {}, "New event"),
    h("div", { class: "row" }, typeSel, source), help, name,
    h("div", { class: "row" }, "Start", start, "Duration", dur, h("span", { class: "muted" }, camp.setting.time_unit + "s")),
    h("h3", {}, "Targets"), targetsBox,
    h("div", {}, ...polBoxes),
    h("h3", {}, "Goods (none = all)"), goodsSel, tags,
    h("h3", {}, "Parameters (JSON)"), params,
    h("div", { class: "row" }, h("button", { class: "primary", onclick: () => guard(async () => {
      const id = "E" + Date.now().toString(36);
      const body = {
        id, type: typeSel.value, name: name.value || typeSel.value, start: Number(start.value),
        duration: dur.value ? Number(dur.value) : null, source: source.value,
        params: JSON.parse(params.value || "{}"),
        targets: {
          systems: [...eventSystems()], lanes: map.selLane ? [map.selLane] : [],
          polities: polBoxes.map(l => l.querySelector("input")).filter(i => i.checked).map(i => i.value),
          goods: [...goodsSel.selectedOptions].map(o => o.value),
          tags: tags.value.split(",").map(s => s.trim()).filter(Boolean),
        },
      };
      await api("/api/events", { method: "POST", body });
      toast(`Event "${body.name}" added — takes effect from ${camp.setting.time_unit} ${body.start}`);
      renderEvents();
    }) }, "Add event"), h("button", { onclick: () => { map.clearMulti(); map.selLane = null; renderEventsTargets(); } }, "Clear map selection")),
    h("h3", {}, `Events (${list.length})`),
    ...list.slice().reverse().map(e => h("div", { class: "card" },
      h("div", { class: "row", style: "justify-content:space-between" },
        h("strong", {}, e.name), h("span", {}, h("span", { class: "pill" + (e.active ? " on" : "") }, e.active ? "active" : "inactive"),
          h("span", { class: "pill" }, e.type), h("span", { class: "pill" }, e.source))),
      h("div", { class: "muted" }, `from ${e.start}${e.duration ? ` for ${e.duration}` : " (open-ended)"} · `,
        [...e.targets.polities.map(p => camp.polities.find(x => x.id === p)?.name || p), ...e.targets.systems.map(s => map.byId[s]?.name || s),
         ...e.targets.lanes].join(", ") || "no targets",
        e.targets.goods.length || e.targets.tags.length ? ` · goods: ${[...e.targets.goods.map(g => goodName[g]), ...e.targets.tags].join(", ")}` : ""),
      h("div", { class: "mono muted" }, JSON.stringify(e.params)),
      h("div", { class: "row" }, h("button", { class: "danger", onclick: () => guard(async () => {
        await api(`/api/events/${e.id}`, { method: "DELETE" }); renderEvents(); toast("Event removed");
      }) }, "Remove")))),
  );
  renderEventsTargets();
}

function eventSystems() {
  const s = new Set(map.multi);
  if (!s.size && selected) s.add(selected);
  return s;
}
function renderEventsTargets() {
  const box = document.getElementById("evTargets"); if (!box) return;
  const sys = [...eventSystems()].map(id => map.byId[id]?.name || id);
  box.textContent = `Systems: ${sys.join(", ") || "none (shift-click systems on the map)"}` +
    (map.selLane ? ` · Lane: ${map.selLane}` : "");
}

// ------------------------------------------------------------------ routes
function renderRoutesForm() {
  if (!camp) return;
  const pane = $("#pane-routes");
  const cargo = h("input", { type: "number", value: 200, min: 1 });
  const jump = h("input", { type: "number", value: 2, min: 1, max: camp.setting.max_jump });
  const maxj = h("input", { type: "number", value: 3, min: 1, max: 8 });
  const off = h("input", { type: "checkbox" });
  const ill = h("input", { type: "checkbox" });
  const results = h("div");
  pane.replaceChildren(
    h("h2", {}, "Trade routes"),
    h("p", { class: "muted" }, selected ? `From ${map.byId[selected].name} (${selected})` : "Select an origin system on the map."),
    h("div", { class: "grid2" },
      h("span", {}, "Cargo hold (tons)"), cargo, h("span", {}, "Jump range"), jump, h("span", {}, "Max jumps"), maxj,
      h("span", {}, "Uncharted jumps"), h("label", {}, off, " allow (risky)"),
      h("span", {}, "Contraband"), h("label", {}, ill, " include")),
    h("div", { class: "row" }, h("button", { class: "primary", disabled: !selected, onclick: () => guard(async () => {
      const q = new URLSearchParams({ origin: selected, cargo: cargo.value, jump: jump.value, max_jumps: maxj.value,
        lanes_only: !off.checked, illegal: ill.checked });
      lastRoutes = await api(`/api/routes?${q}`);
      results.replaceChildren(lastRoutes.length ? h("table", {},
        h("tr", {}, h("th", {}, "Good"), h("th", {}, "To"), h("th", {}, "Jumps"), h("th", {}, "Qty"), h("th", {}, "Buy"),
          h("th", {}, "Sell"), h("th", {}, "Profit"), h("th", {}, "Risk")),
        ...lastRoutes.map(r => h("tr", { class: "click", onclick: () => { map.showPath(r.path); map.centerOn(r.destination); } },
          h("td", {}, goodName[r.good], r.legal ? "" : h("span", { class: "pill warn" }, "smuggle")),
          h("td", {}, map.byId[r.destination].name), h("td", {}, r.jumps + (r.offlane_jumps ? "*" : "")),
          h("td", {}, fmt(r.quantity, 1)), h("td", {}, fmt(r.buy)), h("td", {}, fmt(r.sell)),
          h("td", { class: "down" }, fmt(r.profit)), h("td", {}, `${(r.risk * 100).toFixed(0)}%`)))) :
        h("p", { class: "muted" }, "No profitable routes from here."),
        h("p", { class: "muted" }, "* uses uncharted jumps. Profit accounts for price impact, freight and risk."));
    }) }, "Find routes"), h("button", { onclick: () => map.showPath(null) }, "Clear path")),
    results);
}

// ------------------------------------------------------------------ politics
function renderPolitics() {
  if (!camp) return;
  const pane = $("#pane-politics");
  const count = {};
  for (const s of camp.systems) count[s.polity] = (count[s.polity] || 0) + 1;
  const rnd = h("input", { type: "checkbox", checked: camp.options.random_events });
  rnd.onchange = () => guard(async () => { await api("/api/options", { method: "PUT", body: { random_events: rnd.checked } }); toast(`Random events ${rnd.checked ? "on" : "off"}`); });
  const pol = camp.polities;
  const matrix = pol.length > 1 ? h("table", {}, h("tr", {}, h("th", {}, ""), ...pol.map(p => h("th", { style: `color:${p.color}` }, p.id))),
    ...pol.map(a => h("tr", {}, h("td", { style: `color:${a.color}` }, a.name), ...pol.map(b => {
      if (a.id === b.id) return h("td", {}, "—");
      const key = [a.id, b.id].sort().join("|");
      const inp = h("input", { type: "number", min: -1, max: 1, step: 0.1, value: camp.relations[key] ?? 0, style: "width:56px" });
      inp.onchange = () => guard(async () => {
        camp.relations = await api("/api/relations", { method: "PUT", body: { a: a.id, b: b.id, value: Number(inp.value) } });
        toast("Relation updated (affects tariffs from next week)");
      });
      return h("td", {}, inp);
    })))) : h("p", { class: "muted" }, "Fewer than two polities.");
  const tagsAll = [...new Set(goods.flatMap(g => g.tags))].sort();
  const lawLabel = v => v === "legal" ? "legal" : v === "illegal" ? "banned" : `banned above law ${v}`;
  const keyLabel = k => k.startsWith("tag:") ? `all ${k.slice(4)}` : (goodName[k] || k);
  const lawCard = p => {
    const keySel = h("select", {}, h("optgroup", { label: "Tags" }, ...tagsAll.map(t => h("option", { value: `tag:${t}` }, `all ${t}`))),
      h("optgroup", { label: "Goods" }, ...goods.map(g => h("option", { value: g.id }, g.name))));
    const valSel = h("select", {}, h("option", { value: "legal" }, "legal"), h("option", { value: "illegal" }, "banned"),
      h("option", { value: "law" }, "banned above law…"));
    const lawN = h("input", { type: "number", value: 5, min: 0, max: 15, style: "display:none" });
    valSel.onchange = () => { lawN.style.display = valSel.value === "law" ? "" : "none"; };
    const setLaw = (key, value) => guard(async () => {
      p.legality = await api(`/api/polities/${p.id}/legality`, { method: "PUT", body: { key, value } });
      renderPolitics(); if (selected) showSystem(selected);
      toast(`${p.name}: ${keyLabel(key)} ${value === null ? "rule removed" : lawLabel(value)}`);
    });
    return h("div", { class: "card" },
      h("span", { style: `color:${p.color}` }, "● "), h("strong", {}, p.name),
      h("span", { class: "muted" }, ` · ${count[p.id] || 0} systems · capital ${map.byId[p.capital]?.name || "–"}`),
      h("div", { class: "row" }, ...Object.entries(p.legality || {}).map(([k, v]) =>
        h("span", { class: "pill" + (v === "legal" ? " on" : " warn"), style: "cursor:pointer", title: "click to remove",
          onclick: () => setLaw(k, null) }, `${keyLabel(k)}: ${lawLabel(v)} ✕`))),
      h("div", { class: "row" }, keySel, valSel, lawN, h("button", { onclick: () =>
        setLaw(keySel.value, valSel.value === "law" ? Number(lawN.value) : valSel.value) }, "Set law")));
  };
  pane.replaceChildren(
    h("h2", {}, "Polities"),
    h("p", { class: "muted" }, "Laws decide what is contraband in each polity. Banned goods can only arrive by smuggling. Unaligned systems use each good's default (law level)."),
    ...pol.map(lawCard),
    h("p", { class: "muted" }, `${count[null] || count[undefined] || 0} unaligned systems.`),
    h("h3", {}, "Relations (−1 hostile … +1 allied)"),
    h("p", { class: "muted" }, "Cross-border lanes pay customs; hostile relations add tariffs. Wars and embargoes override these while active."),
    matrix,
    h("h3", {}, "Random events"),
    h("label", {}, rnd, " Roll the setting's random events each week"),
  );
}

// ------------------------------------------------------------------ players
async function renderPlayers() {
  if (!camp) return;
  const pane = $("#pane-players");
  const trades = await api("/api/trades");
  const vis = camp.player_view.visible_systems;
  pane.replaceChildren(
    h("h2", {}, "Player view"),
    h("p", { class: "muted" }, "Players open ", h("a", { href: "/player", target: "_blank", style: "color:var(--accent)" }, location.origin + "/player"),
      " — they see the map, and market prices only for the systems listed here."),
    h("div", { class: "row" },
      h("button", { onclick: () => guard(async () => {
        const ids = new Set([...vis, ...eventSystems()]); await setVisible([...ids]);
      }) }, "Add selected"),
      h("button", { onclick: () => guard(() => setVisible(camp.systems.map(s => s.id))) }, "Show all"),
      h("button", { onclick: () => guard(() => setVisible([])) }, "Hide all")),
    h("div", {}, ...vis.map(id => h("span", { class: "pill", style: "cursor:pointer", title: "remove",
      onclick: () => guard(() => setVisible(vis.filter(x => x !== id))) }, `${map.byId[id]?.name || id} ✕`))),
    h("h3", {}, `Player trades (${trades.length})`),
    trades.length ? h("table", {}, h("tr", {}, h("th", {}, "Week"), h("th", {}, "System"), h("th", {}, "Good"), h("th", {}, "Qty"), h("th", {}, "Price")),
      ...trades.slice().reverse().map(t => h("tr", {}, h("td", {}, t.tick), h("td", {}, map.byId[t.system]?.name || t.system),
        h("td", {}, goodName[t.good]), h("td", { class: t.quantity > 0 ? "up" : "down" }, fmt(t.quantity, 1)), h("td", {}, fmt(t.price))))) :
      h("p", { class: "muted" }, "Record trades from a system's market table. Purchases and sales move local prices."),
    h("h3", {}, "Political impact"),
    h("p", { class: "muted" }, "Record what the players did as an event with source “Players” (Events tab) — e.g. a ‘player action’ that boosts demand, or a relations change."),
  );
}

async function setVisible(ids) {
  const view = await api("/api/player_view", { method: "PUT", body: { visible_systems: ids, show_flows: camp.player_view.show_flows } });
  camp.player_view = view; renderPlayers(); if (selected) showSystem(selected);
}

async function togglePlayerVisible(id) {
  const vis = camp.player_view.visible_systems;
  await guard(() => setVisible(vis.includes(id) ? vis.filter(x => x !== id) : [...vis, id]));
}

boot();
