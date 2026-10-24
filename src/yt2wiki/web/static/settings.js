// yt2wiki settings: guided endpoint setup + advanced providers/roles.
// Requires operator auth (auth.js provides window.yt2wikiAuth with CSRF).
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const auth = window.yt2wikiAuth;

  // Guided card: save one endpoint, test the actual server, show what it
  // qualified for. Server assigns writer+vision / asr when they pass.
  $$("[data-guided-llm], [data-guided-asr]").forEach((card) => {
    const kind = "guidedAsr" in card.dataset ? "asr" : "llm";
    const read = () => Object.fromEntries(
      $$("[data-g]", card).map((i) => [i.dataset.g, i.value.trim()]));
    const msg = $("[data-guided-msg]", card);
    const out = $("[data-guided-result]", card);
    $("[data-guided-save]", card).addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      const v = read();
      if (!v.name || !v.url) { msg.textContent = "Name and URL are required."; return; }
      btn.setAttribute("aria-busy", "true");
      msg.textContent = "Testing the actual server…";
      out.className = "test-result"; out.textContent = "";
      try {
        const d = await auth.api("/api/settings/inference/save-test", {
          method: "POST", body: JSON.stringify(v),
        });
        const r = d.result || {};
        const caps = [];
        if (r.qualified_for_writer) caps.push("writes");
        if (r.qualified_for_vision) caps.push("judges images");
        if (r.qualified_for_asr) caps.push("transcribes");
        if (d.ok && caps.length) {
          out.className = "test-result ok";
          out.textContent = `Qualified: ${caps.join(" · ")}. Roles assigned — reload to see readiness.`;
          msg.textContent = "Saved.";
          setTimeout(() => location.reload(), 1200);
        } else if (d.ok) {
          out.className = "test-result ok";
          const savedNote = "Endpoint saved; qualification recorded as failed — fix the endpoint and test again.";
          out.textContent = kind === "asr"
            ? `Reachable, but it did not speak the Whisper /asr contract (multipart audio_file, segments + language JSON). ${savedNote}`
            : `Reachable, but it did not produce the required structured output. ${savedNote}`;
          msg.textContent = "Saved, not qualified.";
        } else {
          out.className = "test-result bad";
          out.textContent = r.error || "The endpoint did not answer.";
          msg.textContent = "Not saved — fix the endpoint first.";
        }
      } catch (e) {
        out.className = "test-result bad"; out.textContent = e.message;
        msg.textContent = "";
      } finally { btn.removeAttribute("aria-busy"); }
    });
  });

  // Advanced providers list (unchanged storage, CSRF via auth.api).
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
        const d = await auth.api("/api/settings/inference/test", {
          method: "POST", body: JSON.stringify(v),
        });
        if (!d.ok) throw new Error(d.error || "Test failed");
        const caps = [];
        if (d.qualified_for_writer) caps.push("writes");
        if (d.qualified_for_vision) caps.push("judges images");
        if (d.qualified_for_asr) caps.push("transcribes");
        out.className = "test-result ok";
        if (d.kind === "asr") {
          out.textContent = "Speaks /asr · checked just now";
        } else {
          const models = (d.models ?? []).slice(0, 3).join(", ");
          const quals = caps.length ? ` · ${caps.join(" · ")}` : " · no qualification";
          const modelPart = models ? ` · ${models}` : "";
          out.textContent = `Reachable · ${d.latency_ms} ms${modelPart}${quals}`;
        }
        const model = $("[data-k=model]", p);
        if (d.models && d.models.length === 1 && !model.value) {
          model.value = d.models[0]; msg.textContent = "Unsaved changes";
        }
        msg.textContent = "Test recorded — Save to keep it with this endpoint.";
      } catch (e) {
        out.className = "test-result bad"; out.textContent = e.message;
      } finally { btn.removeAttribute("aria-busy"); }
    });
  };
  $$("[data-provider]", list).forEach(wire);

  $("[data-add]", root)?.addEventListener("click", () => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    list.appendChild(node);
    wire(node);
    $("[data-none]", root)?.remove();
    $("[data-k=name]", node).focus();
  });

  $("[data-save]", root)?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const providers = $$("[data-provider]", list).map(read).filter((v) => v.name || v.url);
    const roles = Object.fromEntries($$("[data-role]", root).map((s) => [s.dataset.role, s.value]));
    btn.setAttribute("aria-busy", "true");
    try {
      await auth.api("/api/settings/inference", {
        method: "POST", body: JSON.stringify({ providers, roles }),
      });
      msg.style.color = "var(--green-strong)"; msg.textContent = "Saved";
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      msg.style.color = "var(--red)"; msg.textContent = e.message;
    } finally { btn.removeAttribute("aria-busy"); }
  });
})();
