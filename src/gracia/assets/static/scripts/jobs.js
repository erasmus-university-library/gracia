// The job panel and the buttons that start jobs.
//
// Every [data-job-panel] on the page (templates/job_panel.pt) is a panel: it lists the jobs it is
// given (its list url, with the filter in data-filter), refreshed every second, and follows one job
// in a dialog, stage by stage. Starting a job is a separate act and any page can do it: a form in a
// modal with [data-job] (the jobs page), or a button with data-start-job="<url>" that posts the
// fields of the form it belongs to. Both announce the new job with the event `gracia:job-started`
// ({detail: {id}}) and the panels open it.
document.addEventListener("DOMContentLoaded", () => {
  const BADGES = {
    queued: "text-bg-secondary", running: "text-bg-primary", stopping: "text-bg-warning",
    stopped: "text-bg-warning", failed: "text-bg-danger", done: "text-bg-success",
    pending: "text-bg-secondary",
  };
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const number = (n) => Number(n).toLocaleString();
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
  const request = (url, options = {}) =>
    fetch(url, { credentials: "same-origin", cache: "no-store", ...options });
  const badge = (node, state) => {
    node.className = `badge ${BADGES[state] || "text-bg-secondary"}`;
    node.textContent = state;
    return node;
  };

  // A bar that knows how far it is fills from the left; one that does not (a total that is not
  // known yet) slides a short piece from left to right: a full bar would look like a job that is
  // done. The elements are kept between refreshes, so a slide is never restarted by a refresh.
  function bar(node, { fraction, state, active, determinate }) {
    const busy = active && !determinate;
    node.classList.toggle("indeterminate", busy);
    node.style.width = busy ? "" : `${Math.round((fraction || 0) * 100)}%`;
    node.textContent = !busy && determinate && fraction > 0 ? `${Math.round(fraction * 100)}%` : "";
    node.classList.toggle("progress-bar-striped", active && !busy);
    node.classList.toggle("progress-bar-animated", active && !busy);
    node.classList.toggle("bg-success", state === "done");
    node.classList.toggle("bg-danger", state === "failed");
    node.classList.toggle("bg-warning", state === "stopped" || state === "stopping");
  }

  const reasons = (rejected) =>
    Object.entries(rejected || {}).map(([why, n]) => `${number(n)} ${why.replace(/_/g, " ")}`).join(", ");

  class Panel {
    constructor(root) {
      this.root = root;
      const filter = root.dataset.filter;
      this.listUrl = root.dataset.listUrl + (filter ? `?${filter}` : "");
      this.body = root.querySelector("[data-jobs-body]");
      this.problem = root.querySelector("[data-problem]");
      this.dialogEl = root.querySelector("[data-progress-dialog]");
      this.dialog = new bootstrap.Modal(this.dialogEl);
      this.jobs = [];
      this.rows = new Map();
      this.compact = root.dataset.compact === "true"; // no table: asks only while a job is watched
      this.watching = null; // the id of the job the dialog follows
      this.shown = null; // the id the dialog's stage rows were made for
      this.stageRows = [];
      this.timer = null;
      this.generation = 0; // a call that is overtaken by a newer one does not start another clock
      this.fold = this.dialogEl.querySelector("[data-log-fold]");
      this.frame = this.dialogEl.querySelector("[data-log-frame]");
      this.fold.addEventListener("toggle", () => {
        if (this.fold.open) this.renderDialog(); // loads the log
        else this.frame.removeAttribute("src"); // and a closed log is not followed
      });
      this.dialogEl.addEventListener("hidden.bs.modal", () => {
        this.watching = null;
        this.fold.open = false;
        if (this.compact) { // nothing is on show: stop asking until the next job
          this.generation++;
          clearTimeout(this.timer);
        }
      });
      root.querySelector("[data-stop]").addEventListener("click", () => {
        const job = this.find(this.watching);
        if (job) this.stop(job);
      });
      document.addEventListener("gracia:job-started", (event) => this.started(event.detail.id));
      if (!this.compact || root.dataset.watch) {
        this.refresh().then(() => { if (root.dataset.watch) this.watch(root.dataset.watch); });
      }
    }

    find(id) { return this.jobs.find((job) => job.id === id); }

    // whether the panel is on show: one in a tab that is not the open one has no dialog to show (a
    // dialog inside a hidden tab cannot be seen), so the panel of the tab that started the job does it
    get visible() { return this.root.getClientRects().length > 0; }

    async started(id) {
      await this.refresh();
      if (this.visible && this.find(id)) this.watch(id); // a job this panel lists (its filter lets it through)
    }

    watch(id) {
      if (!this.find(id)) return;
      this.watching = id;
      this.renderDialog();
      this.dialog.show();
    }

    // asks for the jobs now and again every second, whatever happens: `refresh()` at any moment
    // restarts the clock, it never stops it
    async refresh() {
      const mine = ++this.generation;
      clearTimeout(this.timer);
      try {
        if (!document.hidden) {
          const response = await request(this.listUrl, { headers: { Accept: "application/json" } });
          if (response.ok) {
            this.jobs = (await response.json()).jobs;
            this.problem.classList.add("d-none");
            this.renderTable();
            this.renderDialog();
          } else {
            this.fail(`The jobs could not be read (${response.status}).`);
          }
        }
      } catch (err) {
        console.error(err);
        this.fail(`The jobs could not be shown: ${err}`);
      }
      if (mine === this.generation) this.timer = setTimeout(() => this.refresh(), 1000);
    }

    fail(text) {
      this.problem.textContent = text;
      this.problem.classList.remove("d-none");
    }

    async stop(job) {
      if (!confirm("Stop this job?")) return;
      const response = await request(`${job.url.replace(/\/$/, "")}/stop`, { method: "POST" });
      if (!response.ok && response.status !== 202) alert(`Could not stop the job (${response.status}).`);
      this.refresh(); // look at once, and keep looking: the job is `stopping` until it is `stopped`
    }

    // ---- the table: one row per job, made once and updated in place ----
    makeRow(job) {
      const row = el("tr");
      const label = el("div", "fw-semibold");
      const detail = el("div", "small text-muted");
      const name = el("td");
      name.append(label, detail);
      const state = el("td");
      const stateBadge = el("span");
      state.append(stateBadge);
      const cell = el("td");
      const outer = el("div", "progress");
      const inner = el("div", "progress-bar");
      inner.setAttribute("role", "progressbar");
      outer.append(inner);
      cell.append(outer);
      const started = el("td", "small");
      const actions = el("td", "text-end");
      const watch = el("button", "btn btn-sm btn-outline-secondary me-1", "Watch");
      watch.type = "button";
      watch.addEventListener("click", () => this.watch(job.id));
      const stop = el("button", "btn btn-sm btn-outline-danger", "Stop");
      stop.type = "button";
      stop.addEventListener("click", () => { const now = this.find(job.id); if (now) this.stop(now); });
      actions.append(watch, stop);
      row.append(name, state, cell, started, actions);
      return { row, label, detail, stateBadge, inner, started, stop };
    }

    renderTable() {
      this.body.querySelector(".none")?.remove();
      const seen = new Set();
      let previous = null;
      for (const job of this.jobs) {
        seen.add(job.id);
        if (!this.rows.has(job.id)) this.rows.set(job.id, this.makeRow(job));
        const parts = this.rows.get(job.id);
        parts.label.textContent = job.label;
        parts.detail.textContent = job.rejected ? `${number(job.rejected)} rejected` : job.id.slice(0, 8);
        badge(parts.stateBadge, job.state);
        bar(parts.inner, {
          fraction: job.percent / 100, state: job.state, active: job.active, determinate: job.determinate,
        });
        parts.started.textContent = when(job.started_at || job.created_at);
        parts.stop.classList.toggle("d-none", !(job.can_stop && job.active));
        const wanted = previous ? previous.nextSibling : this.body.firstChild;
        if (parts.row !== wanted) this.body.insertBefore(parts.row, wanted); // only when it moved
        previous = parts.row;
      }
      for (const [id, parts] of this.rows) {
        if (!seen.has(id)) { parts.row.remove(); this.rows.delete(id); }
      }
      if (!this.jobs.length && !this.body.querySelector(".none")) {
        this.body.replaceChildren();
        const row = el("tr", "none");
        const cell = el("td", "text-muted", "No jobs.");
        cell.colSpan = 5;
        row.append(cell);
        this.body.append(row);
      }
    }

    // ---- the dialog: one row per stage ----
    makeStageRow() {
      const box = el("div", "mb-3");
      const head = el("div", "d-flex align-items-baseline gap-2");
      const name = el("span", "fw-semibold");
      const state = el("span");
      const counts = el("span", "ms-auto small text-muted");
      head.append(name, state, counts);
      const outer = el("div", "progress my-1");
      const inner = el("div", "progress-bar");
      inner.setAttribute("role", "progressbar");
      outer.append(inner);
      const detail = el("div", "small text-muted");
      const samples = el("ul", "small text-muted mb-0 ps-3");
      box.append(head, outer, detail, samples);
      return { box, name, state, counts, inner, detail, samples };
    }

    renderDialog() {
      const job = this.find(this.watching);
      if (!job) return;
      const q = (name) => this.dialogEl.querySelector(`[data-${name}]`);
      q("title").textContent = job.label;
      badge(q("state"), job.state);
      if (this.shown !== job.id || this.stageRows.length !== job.stages.length) {
        this.shown = job.id;
        this.stageRows = job.stages.map(() => this.makeStageRow());
        q("stages").replaceChildren(...this.stageRows.map((r) => r.box));
      }
      job.stages.forEach((stage, i) => {
        const r = this.stageRows[i];
        const { done, running, total } = stage.batches;
        r.name.textContent = stage.name;
        badge(r.state, stage.state);
        r.state.classList.add("fw-normal");
        const known = total !== null && total !== undefined;
        bar(r.inner, {
          fraction: stage.state === "done" ? 1 : known && total ? done / total : 0,
          state: stage.state,
          active: stage.state === "running",
          determinate: known || stage.state === "done",
        });
        r.counts.textContent = known
          ? `${number(done)} of ${number(total)} batches${running ? `, ${running} running` : ""}`
          : "waiting for the plan";
        const parts = [`${number(stage.processed)} processed`];
        if (stage.total !== null && stage.total !== undefined) {
          parts[0] += ` of ${stage.estimated && stage.state !== "done" ? "about " : ""}${number(stage.total)}`;
        }
        parts.push(`${number(stage.passed)} passed`);
        const rejected = reasons(stage.rejected);
        if (rejected) parts.push(`rejected: ${rejected}`);
        r.detail.textContent = parts.join(" · ");
        r.samples.replaceChildren(
          ...stage.samples.map((s) => el("li", null, `${s.id} – ${s.message || s.reason}`)),
        );
      });
      q("message").textContent = job.message || "";
      const error = q("error");
      error.textContent = job.error || "";
      error.classList.toggle("d-none", !job.error);
      const download = q("download");
      download.classList.toggle("d-none", !job.download_url);
      if (job.download_url) download.href = job.download_url;
      if (this.fold.open) { // the last lines of the log, followed by the page in the frame
        const wanted = `${job.logs_url}?name=luigi&embed=1`;
        if (this.frame.getAttribute("src") !== wanted) this.frame.src = wanted;
      }
      q("stop").classList.toggle("d-none", !(job.can_stop && job.active));
    }
  }

  document.querySelectorAll("[data-job-panel]").forEach((root) => new Panel(root));

  const announce = (id) =>
    document.dispatchEvent(new CustomEvent("gracia:job-started", { detail: { id } }));

  // starting a job from a form in a modal (the jobs page): the form goes to the server; a form with
  // errors comes back to be shown again
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest(".modal[data-job] form");
    if (!form) return;
    event.preventDefault();
    const modal = form.closest(".modal");
    const holder = modal.querySelector(".job-form");
    const note = modal.querySelector(".job-problem");
    const button = modal.querySelector('button[type="submit"]');
    const startUrl = document.querySelector("[data-start-url]").dataset.startUrl;
    button.disabled = true;
    note.classList.add("d-none");
    try {
      const response = await request(`${startUrl}?job=${encodeURIComponent(modal.dataset.job)}`, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "application/json" },
      });
      const answer = await response.json().catch(() => ({}));
      if (response.ok) {
        bootstrap.Modal.getInstance(modal).hide();
        announce(answer.id);
      } else if (response.status === 422 && answer.form) {
        holder.innerHTML = answer.form;
        if (window.htmx) window.htmx.process(holder); // its hx- attributes, in the new form
        if (window.deform) window.deform.load(); // the widgets of the form that came back
      } else {
        note.textContent = answer.errors
          ? Object.entries(answer.errors).map(([k, v]) => `${k}: ${v}`).join("; ")
          : `The job could not be started (${response.status}).`;
        note.classList.remove("d-none");
      }
    } finally {
      button.disabled = false;
    }
  });

  // what a start button posts: the fields of its form, or only those it names with
  // data-start-fields="a,b" (a form of something else, such as the settings of a type, has many
  // fields the job knows nothing of, and nested ones the job cannot read)
  function startBody(button, form) {
    const data = new FormData(form);
    const names = (button.dataset.startFields || "").split(",").filter(Boolean);
    if (!names.length) return new URLSearchParams(data);
    const body = new URLSearchParams();
    names.forEach((name) => data.has(name) && body.set(name, data.get(name)));
    return body;
  }

  // starting a job from a button anywhere: it posts the fields of the form it belongs to
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-start-job]");
    if (!button) return;
    event.preventDefault();
    const form = document.getElementById(button.getAttribute("form")) || button.closest("form");
    const note = document.querySelector(button.dataset.startError || "[data-start-error]");
    button.disabled = true;
    if (note) note.classList.add("d-none");
    try {
      const response = await request(button.dataset.startJob, {
        method: "POST",
        body: startBody(button, form),
        headers: { Accept: "application/json" },
      });
      const answer = await response.json().catch(() => ({}));
      if (response.ok) {
        announce(answer.id);
      } else {
        const text = answer.errors
          ? Object.entries(answer.errors).map(([k, v]) => `${k}: ${v}`).join("; ")
          : answer.form
            ? "Choose valid values for the fields."
            : `The job could not be started (${response.status}).`;
        if (note) { note.textContent = text; note.classList.remove("d-none"); } else alert(text);
      }
    } finally {
      button.disabled = false;
    }
  });
});
