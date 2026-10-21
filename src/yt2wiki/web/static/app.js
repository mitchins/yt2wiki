// yt2wiki — small progressive enhancements. Every page works without this file.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

  // Forms: show a busy state so a slow URL probe or upload doesn't feel stuck.
  $$("form[data-busy]").forEach((form) => {
    form.addEventListener("submit", () => {
      const btn = $("button[type=submit]", form);
      if (!btn) return;
      btn.setAttribute("aria-busy", "true");
      btn.dataset.label = btn.textContent;
      const file = $("input[type=file]", form);
      btn.lastChild.textContent = file && file.files.length ? " Uploading…" : " Checking the link…";
    });
  });

  // Upload drop zone: show the chosen file name; accept drag and drop.
  $$("[data-drop]").forEach((zone) => {
    const input = $("input[type=file]", zone);
    const name = $("[data-fname]", zone);
    const show = () => {
      if (!input.files.length) return;
      const f = input.files[0];
      name.textContent = `${f.name} · ${(f.size / 1048576).toFixed(1)} MB`;
      name.classList.add("fname");
    };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((e) => zone.addEventListener(e, (ev) => {
      ev.preventDefault(); zone.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((e) => zone.addEventListener(e, () => zone.classList.remove("over")));
    zone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      if (ev.dataTransfer.files.length) { input.files = ev.dataTransfer.files; show(); }
    });
  });

  // Menus: close on outside click / Escape.
  document.addEventListener("click", (ev) => {
    $$("details.menu[open]").forEach((d) => { if (!d.contains(ev.target)) d.open = false; });
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") $$("details.menu[open]").forEach((d) => { d.open = false; });
  });
  $$("[data-print]").forEach((b) => b.addEventListener("click", () => {
    $$("details.menu[open]").forEach((d) => { d.open = false; });
    window.print();
  }));

  // ------------------------------------------------------------ job progress
  const fmt = (s) => {
    if (s == null) return "";
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  };
  const CHECK = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';
  const CROSS = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';

  function renderSteps(list, steps) {
    steps.forEach((s) => {
      const li = $(`[data-step="${s.key}"]`, list);
      if (!li) return;
      li.className = s.state;
      $(".dot", li).innerHTML = (s.state === "done" || s.state === "skipped") ? CHECK : s.state === "failed" ? CROSS : "";
      let d = $(".step-detail", li);
      const text = s.state === "failed" && s.error ? s.error : s.detail;
      if (text) {
        if (!d) { d = document.createElement("span"); d.className = "step-detail"; d.style.display = "block"; li.children[1].appendChild(d); }
        d.textContent = text;
      }
      $(".step-time", li).textContent = fmt(s.duration_s);
    });
  }

  function pollJob(el) {
    const id = el.dataset.jobLive;
    let stopped = false;
    const tick = async () => {
      if (stopped || document.hidden) return;
      let r;
      try { r = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { cache: "no-store" }); } catch { return; }
      if (!r.ok) return;
      const j = (await r.json()).view;
      const chip = $("[data-chip]", el);
      if (chip) { chip.textContent = j.state_label; chip.className = `chip ${j.tone}`; }
      const bar = $("[data-bar]", el);
      if (bar) bar.style.width = `${j.progress}%`;
      const steps = $("[data-steps]", el);
      if (steps) renderSteps(steps, j.steps);
      if (!["queued", "running"].includes(j.state)) {
        stopped = true;
        // Reload once so the page shows the finished state, article link and retry.
        setTimeout(() => location.reload(), 600);
      }
    };
    setInterval(tick, 2500);
    document.addEventListener("visibilitychange", tick);
  }
  $$("[data-job-live]").forEach(pollJob);

  // Library / add page strip of active jobs.
  $$("[data-live-jobs]").forEach((box) => {
    const tick = async () => {
      if (document.hidden) return;
      let r;
      try { r = await fetch("/api/jobs/active", { cache: "no-store" }); } catch { return; }
      if (!r.ok) return;
      const { jobs } = await r.json();
      const ids = new Set(jobs.map((j) => j.job_id));
      const shown = $$("[data-job]", box).map((a) => a.dataset.job);
      if (shown.some((id) => !ids.has(id)) || jobs.some((j) => !shown.includes(j.job_id))) {
        location.reload(); // a job finished or started: show the new library state
        return;
      }
      jobs.forEach((j) => {
        const a = $(`[data-job="${j.job_id}"]`, box);
        if (!a) return;
        $("[data-bar]", a).style.width = `${j.progress}%`;
        if (j.state === "running") $("[data-label]", a).textContent = `${j.current_label || "Starting"}…`;
      });
    };
    setInterval(tick, 4000);
  });

  // ------------------------------------------------------------------ reader
  const article = $("[data-article]");
  if (!article) return;

  // Copy buttons on code blocks.
  $$("pre", article).forEach((pre) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "copy"; b.textContent = "Copy";
    b.setAttribute("aria-label", "Copy code");
    b.addEventListener("click", async () => {
      const text = $("code", pre)?.innerText ?? pre.innerText;
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); ta.remove();
      }
      b.textContent = "Copied"; b.classList.add("done");
      setTimeout(() => { b.textContent = "Copy"; b.classList.remove("done"); }, 1600);
    });
    pre.appendChild(b);
  });

  // Contents: highlight the section being read.
  const links = $$(".toc a[href^='#']");
  if (links.length && "IntersectionObserver" in window) {
    const byId = new Map(links.map((a) => [decodeURIComponent(a.hash.slice(1)), a]));
    const visible = new Map();
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => visible.set(e.target.id, e.isIntersecting ? e.boundingClientRect.top : null));
      let best = null;
      for (const [id, top] of visible) if (top !== null && (best === null || top < best.top)) best = { id, top };
      if (!best) return;
      links.forEach((a) => a.classList.remove("active"));
      byId.get(best.id)?.classList.add("active");
    }, { rootMargin: "-70px 0px -55% 0px" });
    $$(".sec h2[id]", article).forEach((h) => io.observe(h));
  }
  $$(".toc-mobile a").forEach((a) => a.addEventListener("click", () => { a.closest("details").open = false; }));

  // Lightbox with keyboard navigation.
  const dlg = $("[data-lightbox]");
  const zooms = $$("a[data-zoom]", article);
  if (dlg && zooms.length && typeof dlg.showModal === "function") {
    const img = $("img", dlg);
    const cap = $("[data-lb-cap]", dlg);
    let at = 0;
    const show = (i) => {
      at = (i + zooms.length) % zooms.length;
      const a = zooms[at];
      const src = $("img", a);
      img.src = a.href; img.alt = src.alt || "";
      cap.replaceChildren();
      const fc = a.closest("figure")?.querySelector("figcaption");
      if (fc) cap.append(...Array.from(fc.cloneNode(true).childNodes));
      const n = document.createElement("span");
      n.className = "muted"; n.textContent = zooms.length > 1 ? `  ${at + 1} / ${zooms.length}` : "";
      cap.append(n);
    };
    zooms.forEach((a, i) => a.addEventListener("click", (ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
      ev.preventDefault(); show(i); dlg.showModal();
    }));
    $("[data-lb-close]", dlg).addEventListener("click", () => dlg.close());
    $("[data-lb-prev]", dlg).addEventListener("click", () => show(at - 1));
    $("[data-lb-next]", dlg).addEventListener("click", () => show(at + 1));
    if (zooms.length < 2) { $("[data-lb-prev]", dlg).hidden = true; $("[data-lb-next]", dlg).hidden = true; }
    dlg.addEventListener("click", (ev) => { if (ev.target === dlg || ev.target.classList.contains("lb-bar")) dlg.close(); });
    dlg.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowRight") show(at + 1);
      if (ev.key === "ArrowLeft") show(at - 1);
    });
  }
})();
