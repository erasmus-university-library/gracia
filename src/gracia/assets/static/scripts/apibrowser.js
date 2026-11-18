// The API browser: a "Try it" for every operation that reads. `{name}` in a path is an input.
document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("api");
  if (!root) return;
  const base = root.dataset.base;

  for (const box of root.querySelectorAll(".try")) {
    const path = box.dataset.path;
    const holder = box.querySelector("[data-inputs]");
    const names = [...path.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
    for (const name of names) {
      const input = document.createElement("input");
      input.className = "form-control form-control-sm d-inline-block w-auto me-1";
      input.placeholder = name;
      input.name = name;
      input.required = true;
      input.setAttribute("aria-label", name);
      holder.append(input);
    }
    if (!names.length) holder.remove();

    box.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.target));
      const url = base + path.replace(/\{([^}]+)\}/g, (_, n) => encodeURIComponent(values[n]));
      const status = box.querySelector("[data-status]");
      const body = box.querySelector("[data-body]");
      status.classList.remove("d-none");
      status.textContent = "…";
      try {
        const response = await fetch(url, {
          credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
        });
        const type = response.headers.get("Content-Type") || "";
        status.textContent = `${response.status} ${response.statusText} · ${type}`;
        body.classList.remove("d-none");
        if (type.includes("json")) {
          body.textContent = JSON.stringify(await response.json(), null, 2);
        } else {
          body.replaceChildren();
          const link = document.createElement("a");
          link.href = url;
          link.textContent = `This is a page, not data: open ${url}`;
          body.append(link);
        }
      } catch (err) {
        status.textContent = "The server cannot be reached.";
      }
    });
  }
});
