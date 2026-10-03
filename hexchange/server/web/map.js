// Shared SVG hex map: rendering, pan/zoom, selection, overlays.
const NS = "http://www.w3.org/2000/svg";
const R = 10;                              // hex circumradius in map units
const SQ3 = Math.sqrt(3);

export function hexCenter(col, row) {
  return [R * 1.5 * col + R, R * SQ3 * (row + 0.5 * (col & 1)) + R * SQ3 / 2];
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

function hexPoints(cx, cy) {
  const pts = [];
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 3 * i;
    pts.push(`${(cx + R * Math.cos(a)).toFixed(2)},${(cy + R * Math.sin(a)).toFixed(2)}`);
  }
  return pts.join(" ");
}

// diverging palette for price ratio: cheap (green) - normal (grey) - expensive (red)
export function priceColor(ratio) {
  const t = Math.max(-1, Math.min(1, Math.log2(ratio) / 1.5));
  const mix = (a, b, f) => Math.round(a + (b - a) * f);
  const mid = [150, 160, 178];
  const lo = [52, 211, 153], hi = [248, 92, 92];
  const c = t < 0 ? mid.map((v, i) => mix(v, lo[i], -t)) : mid.map((v, i) => mix(v, hi[i], t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

export class HexMap {
  constructor(svg, { onSelect, onLane } = {}) {
    this.svg = svg; this.onSelect = onSelect; this.onLane = onLane;
    this.view = { x: 0, y: 0, w: 100, h: 100 };
    this.selected = null; this.multi = new Set(); this.selLane = null;
    this.layers = {};
    for (const name of ["hex", "smug", "lanes", "path", "unk", "sys", "labels"]) this.layers[name] = el("g", {}, svg);
    this._interact();
  }

  load(data) {
    this.data = data;
    this.byId = Object.fromEntries([...(data.unknown || []).map(u => [u.id, { ...u, unknown: true }]),
                                    ...data.systems.map(s => [s.id, s])]);
    this.polity = Object.fromEntries(data.polities.map(p => [p.id, p]));
    const W = R * 1.5 * data.width + R * 1.5, H = R * SQ3 * (data.height + 0.5) + R;
    this.bounds = { w: W, h: H };
    this.view = { x: -R, y: -R, w: W + 2 * R, h: H + 2 * R };
    for (const g of Object.values(this.layers)) g.replaceChildren();
    // grid
    const frag = document.createDocumentFragment();
    for (let c = 0; c < data.width; c++) for (let r = 0; r < data.height; r++) {
      const [x, y] = hexCenter(c, r);
      el("polygon", { points: hexPoints(x, y), class: "hex" }, frag);
    }
    this.layers.hex.appendChild(frag);
    // lanes
    this.laneEls = {};
    for (const ln of data.lanes) {
      const a = this.byId[ln.a], b = this.byId[ln.b];
      const [x1, y1] = hexCenter(a.col, a.row), [x2, y2] = hexCenter(b.col, b.row);
      const out = a.unknown || b.unknown;        // player view: lane leading into uncharted space
      const line = el("line", { x1, y1, x2, y2, class: out ? "lane out" : "lane", "stroke-width": 1.2 }, this.layers.lanes);
      line.addEventListener("click", e => { e.stopPropagation(); this.selectLane(ln.id); });
      line.style.cursor = "pointer";
      this.laneEls[ln.id] = line;
    }
    // uncharted endpoints (player view): position only
    for (const u of data.unknown || []) {
      const [x, y] = hexCenter(u.col, u.row);
      const c = el("circle", { cx: x, cy: y, r: 2.6, class: "unk" }, this.layers.unk);
      el("title", {}, c).textContent = `Uncharted system (${u.id})`;
    }
    // systems
    this.sysEls = {};
    const popKey = data.setting.roles.population;
    for (const s of data.systems) {
      const [x, y] = hexCenter(s.col, s.row);
      const pop = Number(s.attrs[popKey] ?? 5);
      const c = el("circle", { cx: x, cy: y, r: (2 + pop * 0.35).toFixed(2), class: "sys" }, this.layers.sys);
      c.addEventListener("click", e => { e.stopPropagation(); this.select(s.id, e.shiftKey); });
      const t = el("title", {}, c); t.textContent = `${s.name} ${s.id} ${s.profile}`;
      this.sysEls[s.id] = c;
      const lab = el("text", { x, y: y + R * 0.78, class: "label" }, this.layers.labels);
      lab.textContent = s.name;
      if (s.visible === false) c.classList.add("hidden");
    }
    this.colorByPolity();
    this._apply();
  }

  colorByPolity() {
    for (const s of this.data.systems) {
      const p = this.polity[s.polity];
      this.sysEls[s.id].setAttribute("fill", p ? p.color : "#7a8399");
    }
  }

  colorByPrice(prices) {
    for (const s of this.data.systems) {
      const q = prices.systems[s.id];
      const e = this.sysEls[s.id];
      if (!q) { e.setAttribute("fill", "#2a3245"); continue; }
      e.setAttribute("fill", q.legal ? priceColor(q.price / prices.base_price) : "#9b59b6");
    }
  }

  showFlows(flows) {
    const vals = Object.values(flows.lanes).map(Math.abs);
    const max = Math.max(1e-9, ...vals);
    for (const [id, line] of Object.entries(this.laneEls)) {
      const v = Math.abs(flows.lanes[id] || 0);
      line.setAttribute("stroke-width", (0.6 + 4.5 * Math.sqrt(v / max)).toFixed(2));
      line.style.stroke = v > 0 ? "#6cb2ff" : "";
      line.style.opacity = v > 0 ? 0.35 + 0.65 * Math.sqrt(v / max) : 0.5;
    }
    this.layers.smug.replaceChildren();
    const sv = Object.values(flows.smuggling || {}).map(Math.abs);
    const smax = Math.max(1e-9, ...sv, max * 0.25);
    // draw only smuggling that matters next to charted trade, or the map drowns in dashes
    const cutoff = 0.03 * Math.max(max, ...sv);
    for (const [key, v] of Object.entries(flows.smuggling || {})) {
      if (Math.abs(v) < cutoff) continue;
      const [ida, idb] = key.split("~");
      const a = this.byId[ida], b = this.byId[idb];
      if (!a || !b) continue;
      const [x1, y1] = hexCenter(a.col, a.row), [x2, y2] = hexCenter(b.col, b.row);
      el("line", { x1, y1, x2, y2, class: "smug", "stroke-width": (0.5 + 3 * Math.sqrt(Math.abs(v) / smax)).toFixed(2) },
         this.layers.smug);
    }
  }

  clearFlows() {
    for (const line of Object.values(this.laneEls)) { line.setAttribute("stroke-width", 1.2); line.style.stroke = ""; line.style.opacity = ""; }
    this.layers.smug.replaceChildren();
  }

  showPath(ids) {
    this.layers.path.replaceChildren();
    if (!ids || ids.length < 2) return;
    const pts = ids.map(id => hexCenter(this.byId[id].col, this.byId[id].row).join(",")).join(" ");
    el("polyline", { points: pts, class: "path" }, this.layers.path);
  }

  select(id, additive = false) {
    if (additive) {
      this.multi.has(id) ? this.multi.delete(id) : this.multi.add(id);
      this.sysEls[id].classList.toggle("multi", this.multi.has(id));
    } else {
      if (this.selected) this.sysEls[this.selected]?.classList.remove("sel");
      this.selected = id;
      this.sysEls[id]?.classList.add("sel");
    }
    this.onSelect?.(id, additive);
  }

  clearMulti() {
    for (const id of this.multi) this.sysEls[id]?.classList.remove("multi");
    this.multi.clear();
  }

  selectLane(id) {
    if (this.selLane) this.laneEls[this.selLane]?.classList.remove("sel");
    this.selLane = id;
    this.laneEls[id]?.classList.add("sel");
    this.onLane?.(id);
  }

  fitTo(ids, margin = 3) {
    const pts = ids.map(id => this.byId[id]).filter(Boolean).map(s => hexCenter(s.col, s.row));
    if (!pts.length) return;
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    const pad = R * margin;
    let x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
    const r = this.svg.getBoundingClientRect(), aspect = (r.width || 4) / (r.height || 3);
    let w = Math.max(x1 - x0, 140), h = Math.max(y1 - y0, 140 / aspect);
    if (w / h < aspect) w = h * aspect; else h = w / aspect;      // keep the screen's aspect ratio
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    this.view = { x: cx - w / 2, y: cy - h / 2, w, h };
    this._apply();
  }

  centerOn(id) {
    const s = this.byId[id]; if (!s) return;
    const [x, y] = hexCenter(s.col, s.row);
    const w = Math.min(this.view.w, 160), h = w * this.view.h / this.view.w;
    this.view = { x: x - w / 2, y: y - h / 2, w, h };
    this._apply();
  }

  _apply() {
    const v = this.view;
    this.svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
    const zoomed = v.w < 260;
    this.layers.labels.style.display = zoomed ? "" : "none";
  }

  _interact() {
    const svg = this.svg;
    let drag = null;
    svg.addEventListener("wheel", e => {
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
      const k = Math.exp(e.deltaY * 0.0015);
      const v = this.view;
      const w = Math.max(40, Math.min(this.bounds ? this.bounds.w * 2 : 2000, v.w * k));
      const h = w * v.h / v.w;
      this.view = { x: v.x + (v.w - w) * fx, y: v.y + (v.h - h) * fy, w, h };
      this._apply();
    }, { passive: false });
    svg.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, v: { ...this.view }, moved: false, id: e.pointerId };
    });
    svg.addEventListener("pointermove", e => {
      if (!drag) return;
      if (!drag.moved) {
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) <= 4) return;
        // capture only once it is really a drag: capturing on press would retarget
        // the click away from the system/lane under the cursor
        drag.moved = true;
        svg.classList.add("dragging");
        svg.setPointerCapture(drag.id);
      }
      const r = svg.getBoundingClientRect();
      const dx = (e.clientX - drag.x) * drag.v.w / r.width, dy = (e.clientY - drag.y) * drag.v.h / r.height;
      this.view = { ...drag.v, x: drag.v.x - dx, y: drag.v.y - dy };
      this._apply();
    });
    const end = e => {
      if (drag && drag.moved) {               // swallow the click that ends a drag
        const stop = ev => { ev.stopPropagation(); };
        svg.addEventListener("click", stop, { capture: true, once: true });
      }
      drag = null; svg.classList.remove("dragging");
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
  }
}

// Small SVG line chart
export function sparkline(values, { width = 90, height = 22, color = "#6cb2ff" } = {}) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", width); svg.setAttribute("height", height);
  if (!values || values.length < 2) return svg;
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1) * (width - 2) + 1).toFixed(1)},${(height - 2 - (v - lo) / span * (height - 4)).toFixed(1)}`);
  el("polyline", { points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 1.4 }, svg);
  return svg;
}

export function fmt(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return "–";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(1) + "G";
  if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return (n / 1e3).toFixed(1) + "k";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json();
}
