// The log page of a job: follows a log by asking for what is new, every second. The server never
// holds a request open: it answers with the bytes after `offset` and says where to go on from in
// the header X-Log-Offset (see views/jobs.py). It stops when the job is over and nothing is new.
document.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-job-log]");
  if (!root) return;
  const text = root.querySelector("[data-log-text]");
  const which = root.querySelector("[data-log-name]");
  const state = root.querySelector("[data-log-state]");
  const follow = root.querySelector("[data-log-follow]");
  const KEEP = 400000; // characters kept in the page: the start is dropped, the file has it all
  let offset = null; // null: the first request, which answers with the end of the log
  let generation = 0; // a call for a log that was switched away from is dropped
  let timer = null;
  let ended = false;
  let quiet = 0; // polls in a row that found nothing new after the job was over
  const AFTER = 3; // the process writes its last words after it has said it is done

  const get = (url) => fetch(url, { credentials: "same-origin", cache: "no-store" });

  async function tick() {
    clearTimeout(timer);
    const mine = ++generation;
    let delay = 1000;
    try {
      const url = new URL(root.dataset.logUrl, location.href);
      url.searchParams.set("name", which.value);
      if (offset !== null) url.searchParams.set("offset", offset);
      const response = await get(url);
      if (mine !== generation) return;
      if (response.ok) {
        const added = await response.text();
        offset = Number(response.headers.get("X-Log-Offset"));
        if (added) {
          const atEnd = text.scrollHeight - text.scrollTop - text.clientHeight < 40;
          text.append(added);
          if (text.textContent.length > KEEP) text.textContent = text.textContent.slice(-KEEP);
          if (follow.checked && atEnd) text.scrollTop = text.scrollHeight;
          delay = 0; // there may be more: a request gives at most 64 KB
        }
        const job = await (await get(root.dataset.statusUrl)).json();
        state.textContent = job.state;
        state.className = `badge ${job.active ? "text-bg-primary" : "text-bg-secondary"}`;
        quiet = job.active || added ? 0 : quiet + 1;
        if (quiet >= AFTER) {
          ended = true; // over, and everything has been read
          return;
        }
      } else {
        state.textContent = `the log could not be read (${response.status})`;
      }
    } catch (err) {
      console.error(err);
      state.textContent = "the server cannot be reached";
    }
    if (mine === generation) timer = setTimeout(tick, delay);
  }

  which.addEventListener("change", () => { // the other log: from its end again
    text.textContent = "";
    offset = null;
    ended = false;
    quiet = 0;
    tick();
  });
  follow.addEventListener("change", () => {
    if (follow.checked) text.scrollTop = text.scrollHeight;
  });
  tick();
  window.addEventListener("beforeunload", () => { ended = true; clearTimeout(timer); });
});
