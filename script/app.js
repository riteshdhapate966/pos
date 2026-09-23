const API = "/api/v1";
const $ = (id) => document.getElementById(id);
const money = (n) => Number(n || 0).toFixed(2);

const state = { outlet: "", menus: [], menu: 0, sub: null, session: null, cart: [], bill: null };

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: { "Content-Type": "application/json" },
    body: opts.body && JSON.stringify(opts.body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || res.statusText));
  return data;
}

function el(tag, props = {}, ...children) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...children.filter((c) => c != null));
  return e;
}

let toastTimer;
function toast(msg, error = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), error ? 6000 : 2500);
}

// Runs an async action with its button disabled; errors go to the toast.
// renderTicket() afterwards rebuilds every button with its correct enabled state.
async function busy(button, fn) {
  if (button) button.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { renderTicket(); }
}

// ---------- outlet ----------

async function loadOutlet() {
  state.outlet = $("outlet").value.trim();
  state.session = null; state.cart = []; state.bill = null;
  renderTicket();
  await Promise.all([loadMenu(), loadTables(), loadSettlementOptions()]);
}

async function loadMenu() {
  try {
    state.menus = (await api(`/outlets/${encodeURIComponent(state.outlet)}/menu`)).menus;
    state.menu = 0; state.sub = null;
  } catch (e) { state.menus = []; toast(e.message, true); }
  renderMenu();
}

async function loadTables() {
  const box = $("tables");
  box.replaceChildren(el("small", { className: "muted", textContent: "Loading…" }));
  try {
    const tables = await api(`/outlets/${encodeURIComponent(state.outlet)}/tables`);
    box.replaceChildren(...tables.map((t) => el("button", {
      className: "table" + (t.checkNumber ? " busy" : "") + (state.session?.tableNumber === t.tableNumber ? " selected" : ""),
      onclick: () => openTable(t.tableNumber, t.covers || 1, t.checkNumber),
    }, el("b", { textContent: t.tableNumber }),
       el("small", { textContent: t.checkNumber ? `${t.checkNumber} · ${money(t.total)}` : "vacant" }))));
  } catch (e) {
    box.replaceChildren(el("small", { className: "muted", textContent: "Could not load tables. Open one by number below." }));
    toast(e.message, true);
  }
}

async function loadSettlementOptions() {
  try {
    const opts = await api(`/outlets/${encodeURIComponent(state.outlet)}/settlement-options`);
    $("settle-option").replaceChildren(...opts.map((o) => el("option", { value: o.code, textContent: `${o.name} (${o.code})` })));
  } catch (e) { toast(e.message, true); }
}

// ---------- menu ----------

function renderMenu() {
  const menu = state.menus[state.menu];
  $("menus").replaceChildren(...state.menus.map((m, i) => el("button", {
    className: i === state.menu ? "on" : "", textContent: m.name,
    onclick: () => { state.menu = i; state.sub = null; renderMenu(); },
  })));
  const subs = menu ? menu.subMenus : [];
  $("submenus").replaceChildren(
    el("button", { className: state.sub === null ? "on" : "", textContent: "All", onclick: () => { state.sub = null; renderMenu(); } }),
    ...subs.map((s) => el("button", {
      className: state.sub === s.code ? "on" : "", textContent: s.name,
      onclick: () => { state.sub = s.code; renderMenu(); },
    })));

  const q = $("search").value.trim().toLowerCase();
  // Search looks across every menu; otherwise show the selected menu / sub-menu.
  const pool = q ? state.menus.flatMap((m) => m.subMenus) : subs.filter((s) => state.sub === null || s.code === state.sub);
  const items = pool.flatMap((s) => s.items).filter((i) => !q || i.name.toLowerCase().includes(q));
  $("items").replaceChildren(...items.map((i) => el("button", { className: "item", onclick: () => addToCart(i) },
    i.imageUrl ? el("img", { src: i.imageUrl, alt: "", loading: "lazy" }) : el("div", { className: "noimg" }),
    el("span", { textContent: i.name }),
    el("strong", { textContent: money(i.price) }))));
  if (!items.length) $("items").append(el("p", { className: "muted", textContent: state.menus.length ? "No items" : "No menu loaded" }));
}

// ---------- session / ticket ----------

async function openTable(tableNumber, covers, checkNumber) {
  if (state.cart.length && !confirm("Discard unsent items?")) return;
  await busy(null, async () => {
    state.session = await api("/sessions", {
      method: "POST",
      body: { outletCode: state.outlet, tableNumber: String(tableNumber), covers: Number(covers), checkNumber },
    });
    state.cart = [];
    await refreshBill();
    loadTables();
  });
}

async function refreshBill() {
  state.bill = state.session ? await api(`/sessions/${state.session.sessionId}/bill`) : null;
  renderTicket();
}

function addToCart(item) {
  if (!state.session) return toast("Select a table first", true);
  const line = state.cart.find((l) => l.itemCode === item.itemCode && l.subMenuCode === item.subMenuCode);
  if (line) line.quantity++;
  else state.cart.push({ itemCode: item.itemCode, subMenuCode: item.subMenuCode, name: item.name, price: item.price, quantity: 1 });
  renderCart();
}

function renderCart() {
  const cart = $("cart");
  cart.replaceChildren(...state.cart.map((l, idx) => el("li", {},
    el("button", { textContent: "−", onclick: () => { if (--l.quantity <= 0) state.cart.splice(idx, 1); renderCart(); } }),
    el("span", { textContent: l.quantity }),
    el("button", { textContent: "+", onclick: () => { l.quantity++; renderCart(); } }),
    el("span", { className: "name", textContent: l.name }),
    el("span", { textContent: money(l.price * l.quantity) }))));
  if (!state.cart.length) cart.append(el("li", { className: "empty", textContent: "Tap menu items to add" }));
  $("send").disabled = !state.session || !state.cart.length;
}

function renderTicket() {
  const s = state.session, b = state.bill;
  $("session-info").textContent = s ? `Table ${s.tableNumber} · session ${s.sessionId} · ${s.status}` : "No table selected";
  $("check-no").textContent = b?.checkNumber ? `${b.checkNumber} (${b.checkStatus})` : "";

  const lines = b?.lines || [];
  $("bill-lines").replaceChildren(...lines.map((l) => el("li", { className: l.status === "PO" ? "" : "void" },
    el("span", { textContent: l.quantity }),
    el("span", { className: "name", textContent: l.name }),
    el("span", { textContent: money(l.price * l.quantity) }),
    l.status === "PO" && s?.status === "ACTIVE"
      ? el("button", { className: "del", title: "Void item", textContent: "✕", onclick: (e) => removeLine(l, e.currentTarget) })
      : el("small", { className: "muted", textContent: l.status }))));
  if (!lines.length) $("bill-lines").append(el("li", { className: "empty", textContent: s ? "Nothing sent yet" : "—" }));

  $("totals").replaceChildren(...(b?.checkNumber ? [
    ["Subtotal", b.subTotal], ["Discount", b.discount], ["Taxes", b.taxes],
  ].flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: money(v) })]).concat(
    el("dt", { className: "grand", textContent: "Balance" }), el("dd", { className: "grand", textContent: money(b.balance) }),
  ) : []));
  $("settle-form").querySelector("button").disabled = !(b?.checkNumber && b.checkStatus === "PO" && s?.status === "ACTIVE");
  renderCart();
}

async function sendOrder(button) {
  await busy(button, async () => {
    const r = await api(`/sessions/${state.session.sessionId}/items`, {
      method: "POST",
      body: { items: state.cart.map(({ itemCode, subMenuCode, quantity }) => ({ itemCode, subMenuCode, quantity })) },
    });
    state.cart = [];
    state.session.checkNumber = r.checkNumber;
    toast(`Sent to ${r.checkNumber} · balance ${money(r.balance)}`);
    await refreshBill();
    loadTables();
  });
}

async function removeLine(line, button) {
  if (!confirm(`Void ${line.quantity} × ${line.name}?`)) return;
  await busy(button, async () => {
    state.bill = await api(`/sessions/${state.session.sessionId}/items/${encodeURIComponent(line.sequenceNumber)}`, { method: "DELETE" });
    toast("Item voided");
    renderTicket();
    loadTables();
  });
}

async function settle(e) {
  e.preventDefault();
  const amount = parseFloat($("settle-amount").value);
  const code = $("settle-option").value;
  if (!confirm(`Settle ${state.bill.checkNumber} with ${code} for ${money(amount || state.bill.balance)}?`)) return;
  await busy(e.submitter, async () => {
    const r = await api(`/sessions/${state.session.sessionId}/settle`, {
      method: "POST", body: { settlementOptionCode: code, amount: amount || null },
    });
    state.session.status = r.sessionStatus;
    $("settle-amount").value = "";
    toast(r.sessionStatus === "CLOSED" ? `Settled. Change ${money(r.change)}` : `Part-paid ${money(r.settled)}`);
    await refreshBill();
    loadTables();
  });
}

// ---------- wiring ----------

$("outlet-form").onsubmit = (e) => { e.preventDefault(); loadOutlet(); };
$("refresh-tables").onclick = loadTables;
$("table-form").onsubmit = (e) => { e.preventDefault(); openTable($("table-no").value.trim(), $("covers").value, null); };
$("search").oninput = renderMenu;
$("send").onclick = (e) => sendOrder(e.currentTarget);
$("settle-form").onsubmit = settle;

loadOutlet();
