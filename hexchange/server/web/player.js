import { HexMap, api, fmt, priceColor } from "/static/map.js?v=8";

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
  map.setParty(camp.party);
  map.fitTo([...camp.systems.map(s => s.id), ...(camp.unknown || []).map(u => u.id)]);   // zoom to what is known
  const here = camp.party && map.byId[camp.party];
  $("#where").textContent = here ? `◆ Party at ${here.name} (${here.id})` : "";
  overlay();
  if (!camp.systems.length)
    $("#pane").replaceChildren(h("p", { class: "muted" }, "Your GM hasn't revealed any systems yet."));
  else if (here) showSystem(here.id);
}

async function overlay() {
  const mode = $("#mode").value;
  $("#goodSel").style.display = mode === "price" ? "" : "none";
  if (mode === "polity") {
    map.colorByPolity();
    $("#legend").replaceChildren(h("strong", {}, "Polities"),
      ...(camp.polities.length ? camp.polities.map(p => h("div", {}, h("span", { style: `color:${p.color}` }, "● "), p.name))
        : [h("div", { class: "muted" }, "none known")]));
  } else {
    const g = $("#goodSel").value;
    const prices = await api(`/api/player/prices?good=${encodeURIComponent(g)}`);
    map.colorByPrice(prices);
    const stops = [0.35, 0.6, 1, 1.6, 2.8].map(r => priceColor(r)).join(",");
    $("#legend").replaceChildren(h("strong", {}, `${goodName[g]} — prices you know`),
      h("div", { class: "bar", style: `background:linear-gradient(90deg,${stops})` }),
      h("div", { class: "row" }, h("span", {}, "cheap"), h("span", {}, "typical"), h("span", {}, "dear")),
      h("div", { class: "muted" }, "Faded: older information. Dark: no information."));
  }
}

async function showSystem(id) {
  const s = map.byId[id];
  const pane = $("#pane");
  const head = h("h2", {}, `${s.name} `, h("span", { class: "muted mono" }, `${s.id} · ${s.profile}`));
  const d = await api(`/api/player/system/${id}`);
  const cur = camp.setting.currency, tu = camp.setting.time_unit;
  const k = d.known;
  if (!k) { pane.replaceChildren(head, h("p", { class: "muted" }, "You have no market information for this system.")); return; }
  const status = k.live ? h("p", { class: "pill on" }, "◆ You are here — live prices")
    : h("p", { class: "muted" }, `Prices as of ${tu} ${k.tick} (${k.age === 0 ? "this " + tu : k.age + " " + tu + (k.age === 1 ? "" : "s") + " ago"}) — `,
        k.source === "report" ? `courier report${k.note ? ": " + k.note : ""}` : "from your last visit",
        ". Real prices may have changed.");
  pane.replaceChildren(head, status,
    h("table", {}, h("tr", {}, h("th", {}, "Good"), h("th", {}, `Buy ${cur}`), h("th", {}, `Sell ${cur}`), h("th", {}, "vs typical")),
      ...k.market.map(m => h("tr", {}, h("td", {}, goodName[m.good], m.legal ? "" : h("span", { class: "pill warn" }, "illegal")),
        h("td", {}, fmt(m.buy)), h("td", {}, fmt(m.sell)), h("td", {}, `×${(m.price / m.base_price).toFixed(2)}`)))),
    camp.setting.disclaimer ? h("p", { class: "disclaimer" }, camp.setting.disclaimer) : null);
}

map = new HexMap($("#map"), { onSelect: (id, add) => { if (!add) showSystem(id); } });
$("#mode").onchange = overlay;
$("#goodSel").onchange = overlay;
$("#refresh").onclick = load;
load().catch(e => { $("#pane").textContent = "The GM has not opened a campaign yet."; console.error(e); });
