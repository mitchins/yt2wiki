// yt2wiki editor: text revisions, preview, image operations, history.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const bar = $("[data-editor]");
  if (!bar) return;
  const id = bar.dataset.editor;
  const sourceId = bar.dataset.source;
  const md = $("[data-md]");
  const preview = $("[data-preview]");
  const status = $("[data-status]");
  const toastEl = $("[data-toast]");
  const original = md.value;
  let dirty = false;

  const toast = (msg) => {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toast.t);
    toast.t = setTimeout(() => toastEl.classList.remove("show"), 2600);
  };
  const post = async (url, body) => {
    const headers = { "Content-Type": "application/json" };
    const csrf = document.querySelector('meta[name=csrf-token]')?.content;
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const r = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
    let data = {};
    try { data = await r.json(); } catch { /* empty body */ }
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  };
  const fmtT = (t) => {
    const s = Math.max(0, Math.round(t));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  md.addEventListener("input", () => {
    dirty = md.value !== original;
    status.textContent = dirty ? "Unsaved changes" : `Version ${bar.dataset.version}`;
  });
  window.addEventListener("beforeunload", (ev) => { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });

  // Image operations save immediately; make sure they can't clobber typed text.
  const guardDirty = () => {
    if (!dirty) return true;
    toast("Save or discard your text changes first.");
    md.focus();
    return false;
  };
  const replaceFrame = async (oldId, newId) => {
    if (!guardDirty()) return;
    try {
      await post(`/api/articles/${id}/images/replace`, { old_frame_id: oldId, new_frame_id: newId });
      toast("Image replaced"); setTimeout(() => location.reload(), 400);
    } catch (e) { toast(e.message); }
  };

  // ---- save
  $("[data-save]").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    if (md.value.trim().length < 20) { toast("The article is too short to save."); return; }
    btn.setAttribute("aria-busy", "true");
    try {
      const d = await post(`/api/articles/${id}`, { markdown: md.value, note: $("#note").value });
      dirty = false;
      toast(`Saved as version ${d.version}`);
      setTimeout(() => { location.href = `/articles/${id}`; }, 500);
    } catch (e) { toast(e.message); btn.removeAttribute("aria-busy"); }
  });

  // ---- write / preview
  $$("[data-mode]").forEach((tab) => tab.addEventListener("click", async (ev) => {
    ev.preventDefault();
    const mode = tab.dataset.mode;
    $$("[data-mode]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    if (mode === "write") { preview.hidden = true; md.hidden = false; md.focus(); return; }
    try {
      const d = await post("/api/preview", { article_id: id, markdown: md.value });
      // Server-rendered from escaped Markdown by the same renderer as the reader.
      preview.innerHTML = `<header class="a-head"><h1 class="a-title"></h1>${d.lead_html ? `<div class="a-lead">${d.lead_html}</div>` : ""}</header>${d.body_html}`;
      $(".a-title", preview).textContent = d.title;
      md.hidden = true; preview.hidden = false;
    } catch (e) { toast(e.message); }
  }));

  // ---- images
  let sections = null;
  const loadSections = async () => {
    if (sections) return sections;
    try { sections = await (await fetch(`/api/v9/articles/${id}/sections`)).json(); } catch { sections = []; }
    return sections;
  };

  $$(".img-item").forEach((item) => {
    const fid = item.dataset.fid;
    const alts = $("[data-alts]", item);
    const grid = $("[data-alt-grid]", item);

    $("[data-act=caption]", item).addEventListener("click", async () => {
      if (!guardDirty()) return;
      try {
        await post(`/api/articles/${id}/images/caption`, { frame_id: fid, caption: $("[data-cap]", item).value });
        toast("Caption saved"); setTimeout(() => location.reload(), 400);
      } catch (e) { toast(e.message); }
    });

    $("[data-act=remove]", item).addEventListener("click", async () => {
      if (!guardDirty()) return;
      if (!confirm("Remove this image from the guide? You can restore it from History.")) return;
      try {
        await post(`/api/articles/${id}/images/remove`, { frame_id: fid });
        toast("Image removed"); setTimeout(() => location.reload(), 400);
      } catch (e) { toast(e.message); }
    });

    const move = $("[data-act=move]", item);
    if (move) move.addEventListener("change", async () => {
      if (!move.value) return;
      if (!guardDirty()) { move.value = ""; return; }
      try {
        await post(`/api/articles/${id}/images/move`, { frame_id: fid, section: move.value });
        toast(`Moved to “${move.value}”`); setTimeout(() => location.reload(), 400);
      } catch (e) { toast(e.message); move.value = ""; }
    });

    const altBtn = $("[data-act=alts]", item);
    altBtn.addEventListener("click", async () => {
      const open = alts.hidden;
      alts.hidden = !open;
      altBtn.setAttribute("aria-expanded", String(open));
      if (!open || grid.dataset.loaded) return;
      const secs = await loadSections();
      const sec = secs.find((s) => s.heading === item.dataset.section);
      const q = sec ? `section_id=${encodeURIComponent(sec.section_id)}&` : "";
      let data;
      try { data = await (await fetch(`/api/v9/articles/${id}/alternatives?${q}limit=9`)).json(); }
      catch { grid.textContent = "Could not load frames."; return; }
      grid.replaceChildren();
      (data.frames || []).filter((f) => f.frame_id !== fid).forEach((f) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "alt"; b.title = `Use the frame at ${fmtT(f.t)}`;
        const im = document.createElement("img");
        im.src = `/frames/${encodeURIComponent(sourceId)}/${encodeURIComponent(f.frame_id)}.jpg`;
        im.alt = ""; im.loading = "lazy";
        const s = document.createElement("span");
        s.textContent = fmtT(f.t);
        b.append(im, s);
        b.addEventListener("click", () => replaceFrame(fid, f.frame_id));
        grid.appendChild(b);
      });
      if (!grid.children.length) grid.textContent = "No other frames in this part of the video.";
      grid.dataset.loaded = "1";
    });
  });

  // ---- history
  const view = $("[data-revview]");
  let current = null;
  $$("[data-rev]").forEach((btn) => btn.addEventListener("click", async () => {
    if (current === btn.dataset.rev && !view.hidden) { view.hidden = true; return; }
    try {
      const r = await (await fetch(`/api/articles/${id}/revisions/${encodeURIComponent(btn.dataset.rev)}`)).json();
      current = btn.dataset.rev;
      $("[data-revtext]", view).textContent = r.markdown;
      btn.closest(".rev").after(view);
      view.hidden = false;
    } catch { toast("Could not load that version."); }
  }));
  $("[data-restore]").addEventListener("click", async () => {
    if (!current || !guardDirty()) return;
    if (!confirm("Restore this version? The current text is kept in History.")) return;
    try {
      const d = await post(`/api/articles/${id}/revert`, { revision_id: current });
      toast(`Restored as version ${d.version}`); setTimeout(() => location.reload(), 500);
    } catch (e) { toast(e.message); }
  });
})();
