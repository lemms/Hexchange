import { HexMap, api, fmt, priceColor, sparkline } from "/static/map.js?v=4";

const $ = s => document.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? (e.className = v) : e.setAttribute(k, v);
  for (const k of kids.flat()) if (k != null) e.append(k instanceof Node ? k : String(k));
  return e;
};

let camp, map, goodName = {};

async function load() {
  camp = await api("/api/player/campaign");
  goodName = Object.fromEntries(camp.setting.goods.map(g => [g.id, g.name]));
  $("#campName").textContent = camp.name;
  $("#tick").textContent = `${camp.setting.time_unit} ${camp.tick}`;
  const gs = $("#goodSel"), prev = gs.value;
  gs.replaceChildren(...camp.setting.goods.map(g => h("option", { value: g.id }, g.name)));
  if (prev) gs.value = prev;
  map.load(camp);
  overlay();
}

async function overlay() {
  const mode = $("#mode").value;
  $("#goodSel").style.display = mode === "price" ? "" : "none";
  if (mode === "polity") {
    map.colorByPolity();
    $("#legend").replaceChildren(h("strong", {}, "Polities"),
      ...camp.polities.map(p => h("div", {}, h("span", { style: `color:${p.color}` }, "● "), p.name)));
  } else {
    const g = $("#goodSel").value;
    const prices = await api(`/api/player/prices?good=${encodeURIComponent(g)}`);
    map.colorByPrice(prices);
    const stops = [0.35, 0.6, 1, 1.6, 2.8].map(r => priceColor(r)).join(",");
    $("#legend").replaceChildren(h("strong", {}, `${goodName[g]} — known prices`),
      h("div", { class: "bar", style: `background:linear-gradient(90deg,${stops})` }),
      h("div", { class: "row" }, h("span", {}, "cheap"), h("span", {}, "typical"), h("span", {}, "dear")),
      h("div", { class: "muted" }, "Dark systems: no market reports."));
  }
}

async function showSystem(id) {
  const s = map.byId[id];
  const pane = $("#pane");
  const head = h("h2", {}, `${s.name} `, h("span", { class: "muted mono" }, `${s.id} · ${s.profile}`));
  if (!s.visible) { pane.replaceChildren(head, h("p", { class: "muted" }, "No market reports from this system.")); return; }
  const d = await api(`/api/player/system/${id}`);
  const cur = camp.setting.currency;
  pane.replaceChildren(head,
    h("table", {}, h("tr", {}, h("th", {}, "Good"), h("th", {}, `Buy ${cur}`), h("th", {}, `Sell ${cur}`), h("th", {}, "Trend")),
      ...d.market.map(m => h("tr", {}, h("td", {}, goodName[m.good], m.legal ? "" : h("span", { class: "pill warn" }, "illegal")),
        h("td", {}, fmt(m.buy)), h("td", {}, fmt(m.sell)), h("td", {}, sparkline(m.history))))),
    camp.setting.disclaimer ? h("p", { class: "disclaimer" }, camp.setting.disclaimer) : null);
}

map = new HexMap($("#map"), { onSelect: (id, add) => { if (!add) showSystem(id); } });
$("#mode").onchange = overlay;
$("#goodSel").onchange = overlay;
$("#refresh").onclick = load;
load().catch(e => { $("#pane").textContent = "The DM has not opened a campaign yet."; console.error(e); });
