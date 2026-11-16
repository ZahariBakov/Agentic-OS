// The Recos panel.

// Recos: tasks you repeat by hand (-> a routine) and corrections you repeat
// (-> a lesson for the lead). Python finds them; you pick cards into the cart.
const recosState = { cards: [], at: null, cart: new Set(), message: "", error: false, busy: false };

function setRecos(changes) {
  Object.assign(recosState, changes);
  const ids = new Set(recosState.cards.map(found => found.id));
  recosState.cart = new Set([...recosState.cart].filter(id => ids.has(id)));  // handled cards leave the cart
  renderRecos();
}

async function recosCall(path, body, done) {
  setRecos({ busy: true });
  try {
    const result = await ui.api(path, body);
    setRecos({ busy: false, cards: result.cards, at: result.at ?? recosState.at, ...done(result) });
  } catch (error) {
    setRecos({ busy: false, message: error.message, error: true });
  }
  loadToday();
}

function recoCard(found) {
  const inCart = recosState.cart.has(found.id);
  const routine = found.routine;
  return ui.el("div", { className: "verdict" + (inCart ? " in-cart" : "") },
    ui.el("div", { className: "buttons", style: "justify-content: space-between" },
      ui.el("label", { className: "inline" },
        ui.el("input", { type: "checkbox", checked: inCart, disabled: recosState.busy, onchange: event => {
          const cart = new Set(recosState.cart);
          event.target.checked ? cart.add(found.id) : cart.delete(found.id);
          setRecos({ cart, message: "" });
        } }),
        " ", ui.el("span", { className: "tag" }, found.kind), " ", ui.el("strong", {}, found.title)),
      ui.el("button", { disabled: recosState.busy, onclick: () => {
        if (confirm(`Dismiss "${found.title}"? It will not come back.`)) {
          recosCall(`/api/recos/${found.id}/dismiss`, {}, () => ({ message: "Dismissed.", error: false }));
        }
      } }, "Dismiss")),
    ui.el("p", { className: "muted" }, found.what),
    routine ? ui.el("p", {}, `Routine "${routine.name}": ${routine.schedule} | ${routine.mode} | haiku | `
      + "at most $0.50 a week (you can change the file in routines/ later)") : null,
    ui.el("ul", { className: "muted" }, found.examples.map(example => ui.el("li", {}, example))));
}

function renderRecos() {
  const { cards, cart, busy } = recosState;
  ui.show(document.getElementById("recos"),
    ui.el("div", { className: "buttons", style: "justify-content: space-between" },
      ui.el("h2", {}, cards.length ? `Recos (${cards.length})` : "Recos"),
      ui.el("div", { className: "buttons" },
        ui.el("span", { className: "muted" }, recosState.at ? "found " + ui.time(recosState.at) : ""),
        ui.el("button", { disabled: busy, onclick: () => recosCall("/api/recos/refresh", {},
          result => ({ message: `${result.new.length} new card(s).`, error: false })) }, "Look again"))),
    ui.el("p", { className: "muted" },
      "Tasks you asked for 3+ times in 7 days, and corrections you gave a lead twice. "
      + "Python finds them (free); a weekly routine looks again on Fridays."),
    cards.length ? cards.map(recoCard) : ui.el("p", { className: "muted" }, "No suggestions now."),
    cards.length ? ui.el("div", { className: "buttons" },
      ui.el("button", { className: "primary", disabled: busy || cart.size === 0, onclick: () =>
        recosCall("/api/recos/setup", { ids: [...cart] }, result => {
          loadRoutines();
          return { cart: new Set(), message: "Set up: " + result.done.join("; "), error: false };
        }) }, `Set up ${cart.size} card(s)`),
      ui.el("span", { className: "muted" }, "A routine card writes routines/<name>.md; a memory card starts a lesson job.")) : null,
    recosState.message ? ui.el("p", { className: recosState.error ? "error" : "muted" }, recosState.message) : null);
}

function loadRecos() {
  ui.api("/api/recos").then(result => setRecos({ cards: result.cards, at: result.at }))
    .catch(error => setRecos({ message: "Recos: " + error.message, error: true }));
}
