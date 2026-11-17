// Agentic OS - a small UI library, shared by all pages.
//
// Rule: text goes into the page with textContent, never with innerHTML.
// Agent answers are untrusted text. With innerHTML, an answer that contains
// <script> or <img onerror=...> would run as code in our page.

const ui = {
  // Make an element. attrs are properties (className, onclick, value...).
  // children are elements or text; text is always added as text, not HTML.
  // Lists inside lists are flattened at every depth, like ui.show (one level was
  // not enough: a list inside a list became the text "[object HTMLDivElement]").
  el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value !== undefined && value !== null) node[key] = value;
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  },

  money(value) {
    return "$" + Number(value || 0).toFixed(4);
  },

  // "2026-11-02T09:14:25.305" -> "11-02 09:14"
  time(text) {
    return text ? text.slice(5, 16).replace("T", " ") : "";
  },

  badge(status) {
    return ui.el("span", { className: `badge ${status}` }, status);
  },

  // Make a table. columns: [{title, value: row => text or element, num: true}]
  // onclick (optional) is called with the row when you click it.
  // event: "click" (default) or "dblclick" (then a single click does nothing).
  table(columns, rows, onclick, event = "click") {
    const head = ui.el("tr", {}, columns.map(c => ui.el("th", { className: c.num ? "num" : "" }, c.title)));
    const double = event === "dblclick";
    const body = rows.map(row => ui.el("tr", {
        className: onclick ? "click" : "",
        title: onclick && double ? "Double-click to open" : null,
        [double ? "ondblclick" : "onclick"]: onclick ? () => onclick(row) : null,
        // A double click selects a word of the text; the second press must not do that.
        onmousedown: onclick && double ? e => { if (e.detail > 1) e.preventDefault(); } : null,
      },
      columns.map(c => ui.el("td", { className: c.num ? "num" : "" }, c.value(row))),
    ));
    if (rows.length === 0) {
      body.push(ui.el("tr", {}, ui.el("td", { className: "muted", colSpan: columns.length }, "Nothing yet.")));
    }
    return ui.el("div", { className: "table-wrap" }, ui.el("table", {}, ui.el("thead", {}, head), ui.el("tbody", {}, body)));
  },

  // Put new content into a container (remove the old content first).
  // Like ui.el: lists inside lists are flattened, and null, undefined and false are skipped.
  // (replaceChildren alone would show them as the text "null" or "[object ...]".)
  show(container, ...children) {
    const parts = children.flat(Infinity).filter(child => child !== null && child !== undefined && child !== false);
    container.replaceChildren(...parts);
  },

  // Get JSON from our API. With a body, send it as JSON with POST.
  async api(path, body) {
    const options = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
    const response = await fetch(path, options);
    // Read the text first: an error page (for example a 500) may not be JSON.
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!response.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail
        : detail ? JSON.stringify(detail) : `${response.status} ${response.statusText}`);
    }
    if (data === null && text) throw new Error("The server did not send JSON.");
    return data;
  },

  // Run an async action with the button disabled, so a double click does not
  // send it twice (a second paid job, a message sent twice).
  async busy(button, action) {
    if (button.disabled) return undefined;
    button.disabled = true;
    try {
      return await action();
    } finally {
      button.disabled = false;
    }
  },

  // The bar at the top of every page, with the clock, the weather and the usage limits on the right.
  header(active) {
    const pages = [["/", "Jobs"], ["/costs", "Costs"], ["/artifacts", "Artifacts"], ["/brain", "Brain"]];
    const clock = ui.el("span", { className: "clock" });
    const weather = ui.el("span", { className: "weather" });
    const limits = ui.el("span", { className: "limits" });
    const bar = ui.el("header", {},
      ui.el("span", { className: "name" }, "Agentic OS"),
      ui.el("nav", {}, pages.map(([href, name]) =>
        ui.el("a", { href, className: name === active ? "active" : "" }, name))),
      clock,
      weather,
      limits,
    );
    document.body.prepend(bar);
    ui.api("/api/limits").then(data => {
      const parts = Object.entries(data.windows).map(([name, w]) =>
        `${name.replace("_", " ")} ${Math.round(w.utilization * 100)}%`);
      limits.textContent = parts.join(" | ") + (data.pause_reason ? " | jobs wait" : "");
    }).catch(() => {});
    // The clock: this computer's time, hours and minutes. We look every second, but
    // the page changes only when the minute does.
    const showClock = () => {
      const now = new Date();
      const text = String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0");
      if (clock.textContent !== text) clock.textContent = text;
    };
    showClock();
    setInterval(showClock, 1000);
    // The weather is extra: with no network the place stays empty.
    const showWeather = () => ui.api("/api/weather").then(facts => {
      weather.textContent = facts.line + (facts.stale ? " (old)" : "");
      weather.title = facts.rest;  // the rest of the day, when the mouse is over it
    }).catch(() => {});
    showWeather();
    setInterval(showWeather, 15 * 60 * 1000);  // the server keeps an answer for 15 minutes
  },
};
