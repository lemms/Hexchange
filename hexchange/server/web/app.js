import { HexMap, api, fmt, priceColor, sparkline } from "/static/map.js?v=12";

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
let editMode = false, paintPolity = undefined;   // undefined = not painting; null = paint "unaligned"

function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.style.borderColor = bad ? "var(--bad)" : "";
  t.classList.add("show"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); console.error(e); } }

// ------------------------------------------------------------------ boot
async function boot() {
  map = new HexMap($("#map"), { onSelect: onSelect, onLane: showLane, onHex: onHex });
  wireHeader(); wireTabs(); wireOverlay(); await loadSettings();
  try { setCampaign(await api("/api/campaign")); }
  catch { $("#genDlg").showModal(); }
}

function setCampaign(data, keepView = false) {
  const keepSel = keepView ? selected : null;
  window.__hxlog?.("boot", { systems: data.systems.length, tick: data.tick });
  camp = data; goods = data.setting.goods; currency = data.setting.currency;
  goodName = Object.fromEntries(goods.map(g => [g.id, g.name]));
  $("#campName").textContent = data.name;
  $("#tick").textContent = `${data.setting.time_unit} ${data.tick}`;
  const gs = $("#goodSel"); const prev = gs.value;
  gs.replaceChildren(h("option", { value: "" }, "All goods (value)"), ...goods.map(g => h("option", { value: g.id }, g.name)));
  gs.value = goods.some(g => g.id === prev) ? prev : (goods[0]?.id || "");
  const multi = keepView ? [...map.multi] : [];
  map.load(data, { keepView });
  map.setParty(data.player_view.location);
  selected = null;
  refreshOverlay(); renderEvents(); renderRoutesForm(); renderPolitics(); renderPlayers(); renderMapTab();
  for (const id of multi) if (map.byId[id] && !map.byId[id].unknown) map.select(id, true, { silent: true });
  if (keepSel && map.byId[keepSel]) {
    // restore the selection quietly; refresh the system pane only if it is the one showing
    map.select(keepSel, false, { silent: true });
    selected = keepSel;
    renderRoutesForm(); renderPolitics();
    if ($("#pane-system").classList.contains("on")) editMode ? showEditor(keepSel) : showSystem(keepSel);
    return;
  }
  $("#pane-system").replaceChildren(h("p", { class: "muted" }, "Click a system on the map. Shift-click to select several (for events)."),
    data.setting.disclaimer ? h("p", { class: "disclaimer" }, data.setting.disclaimer) : null);
}

// ------------------------------------------------------------------ GM editing
async function applyEdit(path, method, body, message) {
  const data = await api(path, { method, body });
  setCampaign(data, true);
  if (message) toast(message);
  return data.result;
}

function setEditMode(on) {
  editMode = on;
  map.setEditMode(on);
  $("#editBtn").classList.toggle("on", on);
  $("#editBtn").textContent = on ? "Editing map — done" : "Edit map";
  if (on) {
    showTab("map");
    toast("Edit mode: click an empty hex to add a system, click a system to edit it");
  }
  if (selected) map.select(selected);
}

function attrInputs(values = {}) {
  // one input per setting attribute: numbers within range, categories as a select
  const inputs = {};
  const rows = camp.setting.attributes.map(a => {
    const v = values[a.key];
    const inp = a.kind === "category"
      ? h("select", {}, h("option", { value: "" }, "roll"), ...a.values.map(x => h("option", { value: x }, x + (a.descriptions[x] ? ` — ${a.descriptions[x]}` : ""))))
      : h("input", { type: "number", min: a.min, max: a.max, placeholder: "roll", style: "width:70px" });
    if (v !== undefined) inp.value = v;
    inputs[a.key] = inp;
    return [h("span", { class: "muted" }, a.name), h("span", {}, inp, a.kind === "category" ? "" : h("span", { class: "muted" }, ` ${a.min}–${a.max}`))];
  });
  const read = () => Object.fromEntries(Object.entries(inputs).filter(([, i]) => i.value !== "")
    .map(([k, i]) => [k, i.tagName === "SELECT" ? i.value : Number(i.value)]));
  return { grid: h("div", { class: "grid2" }, ...rows.flat()), read };
}

function onHex(col, row) {
  if (!editMode) return;
  if (map.data.systems.some(s => s.col === col && s.row === row)) return;
  showTab("system");
  const sec = (camp.sectors || []).find(s => col >= s.col0 && col < s.col0 + s.width && row >= s.row0 && row < s.row0 + s.height);
  const label = sec ? `${String(col - sec.col0 + 1).padStart(2, "0")}${String(row - sec.row0 + 1).padStart(2, "0")}` : "";
  const name = h("input", { placeholder: "name (blank = random)", style: "width:100%" });
  const attrs = attrInputs();
  $("#pane-system").replaceChildren(
    h("h2", {}, "New system ", h("span", { class: "muted mono" }, `${sec?.name || ""} ${label}`)),
    h("p", { class: "muted" }, "Leave attributes blank to roll them with the setting's dice; trade codes are worked out automatically."),
    name, h("h3", {}, "Attributes"), attrs.grid,
    h("div", { class: "row" }, h("button", { class: "primary", onclick: () => guard(async () => {
      const res = await applyEdit("/api/systems", "POST", { col, row, name: name.value || null, attrs: attrs.read() }, "System added");
      map.select(res.id);
    }) }, "Add system"), h("button", { onclick: () => $("#pane-system").replaceChildren() }, "Cancel")));
}

async function showEditor(id) {
  const s = map.byId[id];
  const name = h("input", { value: s.name, style: "width:100%" });
  const attrs = attrInputs(s.attrs);
  const lanes = camp.lanes.filter(l => l.a === id || l.b === id);
  const others = [...map.multi].filter(x => x !== id);
  const pol = camp.polities.find(p => p.id === s.polity);
  $("#pane-system").replaceChildren(
    h("h2", {}, "Edit ", h("span", { class: "mono" }, `${map.place(id)} · ${s.profile}`)),
    h("div", {}, pol ? h("span", { class: "pill", style: `border-color:${pol.color};color:${pol.color}` }, pol.name) : h("span", { class: "pill" }, "Unaligned"),
      ...s.codes.map(c => h("span", { class: "pill" }, c))),
    h("h3", {}, "Name"), name,
    h("h3", {}, "Attributes"), attrs.grid,
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: () => guard(() => applyEdit(`/api/systems/${id}`, "PUT", { name: name.value, attrs: attrs.read() }, "System updated")) }, "Save"),
      h("button", { onclick: () => guard(() => applyEdit(`/api/systems/${id}/reroll`, "POST", {}, "Attributes rerolled")) }, "Reroll"),
      h("button", { class: "danger", onclick: ev => guard(async () => {
        if (!confirmButton(ev.target)) return;
        await applyEdit(`/api/systems/${id}`, "DELETE", undefined, `${s.name} removed`);
        $("#pane-system").replaceChildren();
      }) }, "Delete system")),
    h("h3", {}, `Lanes (${lanes.length})`),
    ...lanes.map(l => laneRow(l)),
    h("div", { class: "row" },
      ...others.map(o => h("button", { onclick: () => guard(() => applyEdit("/api/lanes", "POST", { a: id, b: o }, `Lane ${s.name} – ${map.byId[o].name} added`)) },
        `Connect to ${map.byId[o].name}`)),
      h("button", { onclick: () => guard(() => applyEdit("/api/lanes/auto", "POST", { systems: [id, ...others] }, "Lanes generated")) }, "Auto-lane")),
    h("p", { class: "muted" }, "Shift-click other systems, then Connect. Auto-lane links these systems to their neighbours the way the generator does; existing lanes are kept."),
  );
}

function confirmButton(btn) {
  // two-step confirm without dialogs: first click arms the button
  if (btn.dataset.armed) return true;
  btn.dataset.armed = "1"; const label = btn.textContent; btn.textContent = "Click again to confirm";
  setTimeout(() => { delete btn.dataset.armed; btn.textContent = label; }, 3000);
  return false;
}

function laneRow(l) {
  const other = map.byId[l.a === selected ? l.b : l.a] || map.byId[l.b];
  const cap = h("input", { type: "number", value: l.capacity, min: 0, style: "width:90px" });
  const risk = h("input", { type: "number", value: l.risk, min: 0, max: 1, step: 0.005, style: "width:70px" });
  const toll = h("input", { type: "number", value: l.toll, min: 0, style: "width:70px" });
  return h("div", { class: "card" },
    h("div", {}, h("strong", {}, `${map.byId[l.a]?.name} — ${map.byId[l.b]?.name}`), h("span", { class: "muted" }, ` · ${l.length} ${camp.setting.distance_unit}`)),
    h("div", { class: "row" }, "cap", cap, "risk", risk, "toll", toll,
      h("button", { onclick: () => guard(() => applyEdit(`/api/lanes/${l.id}`, "PUT",
        { capacity: Number(cap.value), risk: Number(risk.value), toll: Number(toll.value) }, "Lane updated")) }, "Save"),
      h("button", { class: "danger", onclick: ev => guard(async () => {
        if (!confirmButton(ev.target)) return;
        await applyEdit(`/api/lanes/${l.id}`, "DELETE", undefined, "Lane removed");
      }) }, "Delete")));
}

// ------------------------------------------------------------------ map tab: sectors
function renderMapTab() {
  if (!camp) return;
  const pane = $("#pane-map");
  const secs = camp.sectors || [];
  const count = {};
  for (const s of camp.systems) count[s.sector] = (count[s.sector] || 0) + 1;
  const name = h("input", { placeholder: "sector name", value: "" });
  const ref = h("select", {}, ...secs.map(s => h("option", { value: s.id }, s.name)));
  const dir = h("select", {}, ...[["E", "east of"], ["W", "west of"], ["N", "north of"], ["S", "south of"]].map(([v, t]) => h("option", { value: v }, t)));
  const w = h("input", { type: "number", value: 32, min: 4, max: 128 }), hh = h("input", { type: "number", value: 40, min: 4, max: 128 });
  const mode = h("select", {}, h("option", { value: "random" }, "random systems"), h("option", { value: "blank" }, "blank (build by hand)"));
  const dens = h("input", { type: "number", value: 0.4, step: 0.05, min: 0.05, max: 1 });
  const npol = h("input", { type: "number", value: 1, min: 0, max: 12 });
  pane.replaceChildren(
    h("h2", {}, "Map"),
    h("div", { class: "row" }, h("button", { class: editMode ? "toggle on" : "toggle", onclick: () => setEditMode(!editMode) },
      editMode ? "Editing map — done" : "Edit map"),
      h("span", { class: "muted" }, editMode ? "Click an empty hex to add a system; click a system to edit it, its lanes, or connect it to shift-selected systems." : "Turn on to add, change or remove systems and lanes.")),
    h("h3", {}, `Sectors (${secs.length})`),
    ...secs.map(s => {
      const nm = h("input", { value: s.name });
      return h("div", { class: "card" },
        h("div", { class: "row" }, nm, h("span", { class: "muted" }, `${count[s.id] || 0} systems · ${s.width}×${s.height}`)),
        h("div", { class: "row" },
          h("button", { onclick: () => map.centerOnSector(s.id) }, "Go to"),
          h("button", { onclick: () => guard(() => applyEdit(`/api/sectors/${s.id}`, "PUT", { name: nm.value }, "Sector renamed")) }, "Rename"),
          secs.length > 1 ? h("button", { class: "danger", onclick: ev => guard(async () => {
            if (!confirmButton(ev.target)) return;
            await applyEdit(`/api/sectors/${s.id}`, "DELETE", undefined, `${s.name} removed`);
          }) }, "Delete sector") : null));
    }),
    h("h3", {}, "Add a sector"),
    h("div", { class: "grid2" },
      h("span", {}, "Name"), name,
      h("span", {}, "Position"), h("span", {}, dir, " ", ref),
      h("span", {}, "Size"), h("span", {}, w, " × ", hh, h("span", { class: "muted" }, " hexes")),
      h("span", {}, "Contents"), mode,
      h("span", {}, "Density"), dens,
      h("span", {}, "New polities"), npol),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: () => guard(async () => {
      if (!name.value) { toast("Give the sector a name", true); return; }
      toast("Adding sector…");
      const res = await applyEdit("/api/sectors", "POST", { name: name.value, adjacent: ref.value, direction: dir.value,
        width: Number(w.value), height: Number(hh.value), mode: mode.value, density: Number(dens.value),
        polities: mode.value === "random" ? Number(npol.value) : 0 }, `Sector ${name.value} added`);
      map.centerOnSector(res.id);
    }) }, "Add sector")),
    h("p", { class: "muted" }, "Random sectors are linked to their neighbours by new lanes across the border; existing lanes never change. All sectors share one economy, so trade, smuggling and the party travel between them."),
  );
}

async function refreshCampaign() {
  const data = await api("/api/campaign");
  camp = data;
  $("#tick").textContent = `${data.setting.time_unit} ${data.tick}`;
  map.setParty(camp.player_view.location);
}

async function moveParty(id) {
  await guard(async () => {
    await api("/api/party", { method: "PUT", body: { location: id } });
    await refreshCampaign(); renderPlayers();
    if (selected) showSystem(selected);
    toast(id ? `Party moved to ${map.byId[id].name}` : "Party removed from the map");
  });
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
      await refreshCampaign(); await refreshOverlay(); renderEvents(); renderPlayers();
      if (selected) await showSystem(selected);
      toast(`Advanced to ${camp.setting.time_unit} ${s.tick}`);
    } finally { setBusy(false); }
  });
  $("#step1").onclick = () => step(1);
  $("#step4").onclick = () => step(4);
  $("#stepGo").onclick = () => step(Number($("#stepN").value) || 1);
  $("#editBtn").onclick = () => setEditMode(!editMode);
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
  if (name === "players") renderPlayers();         // always show current party/knowledge
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
  if (paintPolity !== undefined && !additive) {        // paint mode: clicking assigns the system
    const pname = paintPolity ? camp.polities.find(p => p.id === paintPolity)?.name : "unaligned";
    guard(() => applyEdit("/api/assign", "PUT", { systems: [id], polity: paintPolity }, `${map.byId[id].name} → ${pname}`));
    return;
  }
  if (additive) { renderEventsTargets(); toast(`${map.multi.size} systems in multi-selection`); if (editMode && selected) showEditor(selected); return; }
  selected = id; showTab("system"); renderRoutesForm();
  if (editMode) showEditor(id); else showSystem(id);
}

async function showSystem(id) {
  const d = await api(`/api/system/${id}`);
  const attrDefs = Object.fromEntries(camp.setting.attributes.map(a => [a.key, a]));
  const codeName = Object.fromEntries(camp.setting.codes.map(c => [c.code, c.name]));
  const pol = camp.polities.find(p => p.id === d.polity);
  const visible = camp.player_view.visible_systems.includes(id);
  const partyHere = camp.player_view.location === id;
  const known = camp.player_view.knowledge?.[id];
  const courierNote = h("input", { placeholder: "note, e.g. bought from a courier for Cr500", style: "flex:1;min-width:180px" });
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
    h("h2", {}, `${d.name} `, h("span", { class: "muted mono" }, `${map.place(d.id)} · ${d.profile}`)),
    h("div", {}, pol ? h("span", { class: "pill", style: `border-color:${pol.color};color:${pol.color}` }, pol.name) : h("span", { class: "pill" }, "Unaligned"),
      ...d.codes.map(c => h("span", { class: "pill", title: codeName[c] || c }, codeName[c] || c)),
      ...d.events.map(e => h("span", { class: "pill warn" }, `event: ${e}`))),
    h("div", { class: "row" },
      h("button", { onclick: () => map.centerOn(id) }, "Centre map"),
      h("button", { onclick: () => togglePlayerVisible(id) }, visible ? "Hide from players" : "Show to players"),
      partyHere ? h("span", { class: "pill on" }, "◆ party is here")
        : h("button", { class: "primary", onclick: () => moveParty(id) }, "Move party here")),
    h("div", { class: "muted" }, partyHere ? "Players see this market live."
      : known ? `Players know prices here as of ${camp.setting.time_unit} ${known.tick} (${known.source === "report" ? "courier report" : "their visit"}${known.note ? ": " + known.note : ""}).`
      : "Players have no market information here."),
    ...(partyHere ? [] : [h("div", { class: "row" }, courierNote, h("button", { onclick: () => guard(async () => {
      await api("/api/party/report", { method: "POST", body: { system: id, note: courierNote.value } });
      await refreshCampaign(); showSystem(id); renderPlayers();
      toast(`Players now hold a courier report for ${d.name} (week ${camp.tick})`);
    }) }, "Give courier report"))]),
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
    h("h3", {}, "GM notes"), notes,
    h("div", { class: "row" }, h("button", { onclick: () => guard(async () => {
      await api(`/api/system/${id}/notes`, { method: "PUT", body: { notes: notes.value } }); toast("Notes saved");
    }) }, "Save notes")),
  );
}

async function showLane(id) {
  const ln = camp.lanes.find(l => l.id === id);
  if (!ln) return;
  showTab("system");
  if (editMode) { $("#pane-system").replaceChildren(h("h2", {}, "Edit lane"), laneRow(ln)); return; }
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
  const source = h("select", {}, h("option", { value: "gm" }, "GM"), h("option", { value: "player" }, "Players"));
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
    const nm = h("input", { value: p.name, style: "width:180px" });
    const col = h("input", { type: "color", value: p.color, style: "width:44px;padding:0" });
    const reach = h("input", { type: "number", value: 4, min: 1, max: 40, style: "width:56px" });
    const painting = paintPolity === p.id;
    return h("div", { class: "card", style: painting ? "border-color:var(--warn)" : "" },
      h("span", { style: `color:${p.color}` }, "● "), h("strong", {}, p.name),
      h("span", { class: "muted" }, ` · ${count[p.id] || 0} systems · capital ${map.byId[p.capital]?.name || "–"}`),
      h("div", { class: "row" }, nm, col,
        h("button", { onclick: () => guard(() => applyEdit(`/api/polities/${p.id}`, "PUT", { name: nm.value, color: col.value }, "Polity updated")) }, "Save"),
        h("button", { class: painting ? "toggle on" : "toggle", onclick: () => { paintPolity = painting ? undefined : p.id; renderPolitics();
          toast(paintPolity ? `Paint mode: click systems to add them to ${p.name}` : "Paint mode off"); } }, painting ? "Painting — stop" : "Paint systems"),
        h("button", { class: "danger", onclick: ev => guard(async () => {
          if (!confirmButton(ev.target)) return;
          if (paintPolity === p.id) paintPolity = undefined;
          await applyEdit(`/api/polities/${p.id}`, "DELETE", undefined, `${p.name} dissolved; its systems are unaligned`);
        }) }, "Delete")),
      h("div", { class: "row" },
        h("button", { disabled: !selected, onclick: () => guard(() => applyEdit(`/api/polities/${p.id}`, "PUT", { capital: selected }, `Capital set to ${map.byId[selected].name}`)) },
          selected ? `Make ${map.byId[selected].name} capital` : "Select a system to make it capital"),
        h("button", { disabled: !(map.multi.size || selected), onclick: () => guard(() => applyEdit("/api/assign", "PUT",
          { systems: [...eventSystems()], polity: p.id }, `${eventSystems().size} systems assigned to ${p.name}`)) }, "Assign selected"),
        "grow", reach, h("button", { onclick: () => guard(() => applyEdit(`/api/polities/${p.id}/grow`, "POST",
          { reach: Number(reach.value) }, `${p.name} expanded`)) }, "Grow from capital")),
      h("div", { class: "row" }, ...Object.entries(p.legality || {}).map(([k, v]) =>
        h("span", { class: "pill" + (v === "legal" ? " on" : " warn"), style: "cursor:pointer", title: "click to remove",
          onclick: () => setLaw(k, null) }, `${keyLabel(k)}: ${lawLabel(v)} ✕`))),
      h("div", { class: "row" }, keySel, valSel, lawN, h("button", { onclick: () =>
        setLaw(keySel.value, valSel.value === "law" ? Number(lawN.value) : valSel.value) }, "Set law")));
  };
  const newName = h("input", { placeholder: "new polity name" });
  const newColor = h("input", { type: "color", value: "#d98c3f", style: "width:44px;padding:0" });
  const unpaint = paintPolity === null;
  pane.replaceChildren(
    h("h2", {}, "Polities"),
    h("p", { class: "muted" }, "Create and reshape political regions at any time. Border and law changes affect customs and contraband from the next week (laws immediately). Laws decide what is contraband in each polity; banned goods can only arrive by smuggling."),
    h("div", { class: "card" },
      h("strong", {}, "New polity"),
      h("div", { class: "row" }, newName, newColor,
        h("button", { class: "primary", onclick: () => guard(async () => {
          if (!newName.value) { toast("Give the polity a name", true); return; }
          const res = await applyEdit("/api/polities", "POST", { name: newName.value, color: newColor.value, capital: selected || null },
            `${newName.value} founded${selected ? " at " + map.byId[selected].name : ""}`);
          paintPolity = res.id; renderPolitics();
          toast(`Paint mode: click systems to add them to ${res.name}`);
        }) }, selected ? `Found at ${map.byId[selected].name}` : "Create")),
      h("div", { class: "muted" }, "Select a system first to make it the capital. After creating, click systems on the map to paint them in.")),
    h("div", { class: "row" }, h("button", { class: unpaint ? "toggle on" : "toggle", onclick: () => { paintPolity = unpaint ? undefined : null; renderPolitics(); } },
      unpaint ? "Painting unaligned — stop" : "Paint systems unaligned")),
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
  const loc = camp.player_view.location;
  const knowledge = Object.entries(camp.player_view.knowledge || {}).sort((x, y) => y[1].tick - x[1].tick);
  pane.replaceChildren(
    h("h2", {}, "Player view"),
    h("p", { class: "muted" }, "Players open ", h("a", { href: "/player", target: "_blank", style: "color:var(--accent)" }, location.origin + "/player"),
      ". They see the party's location live, remembered prices where they have been or bought courier reports, and systems you reveal. Everything else stays blank."),
    h("h3", {}, "Party location"),
    h("div", { class: "row" },
      loc ? h("strong", {}, `◆ ${map.byId[loc]?.name} (${loc})`) : h("span", { class: "muted" }, "Not placed"),
      loc ? h("button", { onclick: () => map.centerOn(loc) }, "Centre") : null,
      selected && selected !== loc ? h("button", { class: "primary", onclick: () => moveParty(selected) }, `Move to ${map.byId[selected].name}`) : null,
      loc ? h("button", { onclick: () => moveParty(null) }, "Remove") : null),
    h("p", { class: "muted" }, "Select a system on the map, then move the party there. The system they leave keeps the prices they last saw."),
    h("h3", {}, `Players' market information (${knowledge.length})`),
    knowledge.length ? h("table", {}, h("tr", {}, h("th", {}, "System"), h("th", {}, "As of"), h("th", {}, "Source"), h("th", {}, "")),
      ...knowledge.map(([sid, k]) => h("tr", {},
        h("td", {}, map.byId[sid]?.name || sid, sid === loc ? h("span", { class: "pill on" }, "live") : ""),
        h("td", {}, `${camp.setting.time_unit} ${k.tick}`),
        h("td", { title: k.note || "" }, k.source === "report" ? "courier" : "visit"),
        h("td", {}, sid === loc ? "" : h("button", { class: "danger", onclick: () => guard(async () => {
          await api(`/api/party/knowledge/${sid}`, { method: "DELETE" }); await refreshCampaign(); renderPlayers();
        }) }, "Forget"))))) : h("p", { class: "muted" }, "None yet."),
    h("h3", {}, "Revealed on the map (no prices unless visited or reported)"),
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
