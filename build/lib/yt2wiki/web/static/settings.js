// yt2wiki settings: inference endpoints and role assignment.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const root = $("[data-inference]");
  if (!root) return;
  const list = $("[data-providers]", root);
  const tpl = $("[data-provider-tpl]", root);
  const msg = $("[data-msg]", root);

  const read = (p) => Object.fromEntries($$("[data-k]", p).map((i) => [i.dataset.k, i.value.trim()]));
  const names = () => $$("[data-provider]", list).map((p) => read(p).name.toLowerCase()).filter(Boolean);

  const syncRoles = () => {
    const opts = names();
    $$("[data-role]", root).forEach((sel) => {
      const keep = sel.value;
      sel.replaceChildren(new Option("Not assigned", ""));
      opts.forEach((n) => sel.appendChild(new Option(n, n)));
      sel.value = opts.includes(keep) ? keep : "";
    });
  };

  const wire = (p) => {
    $("[data-remove]", p).addEventListener("click", () => { p.remove(); syncRoles(); msg.textContent = "Unsaved changes"; });
    $$("[data-k]", p).forEach((i) => i.addEventListener("input", () => { msg.textContent = "Unsaved changes"; }));
    $("[data-k=name]", p).addEventListener("change", syncRoles);
    $("[data-test]", p).addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      const out = $("[data-result]", p);
      const v = read(p);
      btn.setAttribute("aria-busy", "true");
      out.className = "test-result"; out.textContent = "Testing…";
      try {
        const r = await fetch("/api/settings/inference/test", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify(v) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Test failed");
        out.className = `test-result ${d.ok ? "ok" : "bad"}`;
        out.textContent = d.ok
          ? `Reachable · ${d.latency_ms} ms${d.models && d.models.length ? " · " + d.models.join(", ") : ""}`
          : d.error;
        const model = $("[data-k=model]", p);
        if (d.ok && !model.value && d.models && d.models.length === 1) {
          model.value = d.models[0]; msg.textContent = "Unsaved changes";
        }
      } catch (e) {
        out.className = "test-result bad"; out.textContent = e.message;
      } finally { btn.removeAttribute("aria-busy"); }
    });
  };
  $$("[data-provider]", list).forEach(wire);

  $("[data-add]", root).addEventListener("click", () => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    list.appendChild(node);
    wire(node);
    $("[data-none]", root)?.remove();
    $("[data-k=name]", node).focus();
  });

  $("[data-save]", root).addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const providers = $$("[data-provider]", list).map(read).filter((v) => v.name || v.url);
    const roles = Object.fromEntries($$("[data-role]", root).map((s) => [s.dataset.role, s.value]));
    btn.setAttribute("aria-busy", "true");
    try {
      const r = await fetch("/api/settings/inference", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ providers, roles }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not save");
      msg.style.color = "var(--green-strong)"; msg.textContent = "Saved";
    } catch (e) {
      msg.style.color = "var(--red)"; msg.textContent = e.message;
    } finally { btn.removeAttribute("aria-busy"); }
  });
})();
