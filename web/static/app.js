"use strict";

const LABELS = {
  courses: {
    course_code: "Code", course_name: "Course", programme: "Prog", level: "Level", cohort: "Cohort",
    lecturer: "Lecturer", lecture_hours: "Lect hrs", practical_hours: "Prac hrs", credits: "Credits",
    online: "Online", field_work: "Field work", hours_per_session: "Hrs/session", sessions_per_week: "Sessions/wk", min_room_size: "Min room",
    sections: "Sections",
  },
  rooms: { name: "Name", capacity: "Capacity", kind: "Kind" },
  cohorts: { programme: "Prog", level: "Level", section: "Section", size: "Size" },
  lecturers: { name: "Name" },
};

const COL_KEYS = { courses: "course_columns", rooms: "room_columns", cohorts: "cohort_columns", lecturers: "lecturer_columns" };

const COURSE_BASIC_GROUPS = [
  { label: "Course", cols: ["course_code", "course_name"] },
  { label: "Students", cols: ["programme", "level"] },
  { label: "Lecturer", cols: ["lecturer"] },
  { label: "Delivery", cols: ["online", "field_work", "hours_per_session"] },
];
const COURSE_ADVANCED_GROUPS = [
  { label: "Teaching hours (TPC)", cols: ["lecture_hours", "practical_hours"] },
  { label: "Sections", cols: ["sections"] },
  { label: "Engine", cols: ["cohort", "credits", "sessions_per_week", "min_room_size", "split"] },
];
const COURSE_GROUPS = [...COURSE_BASIC_GROUPS, ...COURSE_ADVANCED_GROUPS];

let showAdvancedCourses = localStorage.getItem("courses-advanced") === "1";

const COLORS = {
  CE: "#0d9488", CV: "#7c3aed", DS: "#2563eb", EL: "#ea580c", ES: "#0891b2",
  GL: "#4d7c0f", GM: "#db2777", MC: "#b91c1c", MF: "#a16207", PM: "#4f46e5", TM: "#0f766e",
};
const FALLBACK = "#64748b";

const meta = { days: [], slot_times: [], course_columns: [], room_columns: [], cohort_columns: [], lecturer_columns: [] };
const state = { courses: [], rooms: [], cohorts: [], lecturers: [] };
const saved = { courses: [], rooms: [], cohorts: [], lecturers: [] };
let timetable = { summary: {}, rows: [] };
let hlToday = false;
let semester = "sem2";

let authToken = sessionStorage.getItem("auth") || "";

function authHeaders() {
  return authToken ? { Authorization: "Bearer " + authToken } : {};
}

async function apiAuthed(path, options) {
  options = options || {};
  options.headers = Object.assign({}, options.headers || {}, authHeaders());
  try {
    return await api(path, options);
  } catch (e) {
    if (authToken && /login/i.test(e.message || "")) {
      authToken = "";
      sessionStorage.removeItem("auth");
      updateLoginUI();
      openLogin();
    }
    throw e;
  }
}

function updateLoginUI() {
  const on = !!authToken;
  $("login-btn").textContent = on ? "Logout" : "Login";
  $("login-state").textContent = on ? "Admin" : "";
}

async function refreshAuth() {
  if (!authToken) { updateLoginUI(); return; }
  try {
    await apiAuthed("/api/auth/check");
  } catch (e) {
    authToken = "";
    sessionStorage.removeItem("auth");
    toast("Session expired - please log in again", true);
  }
  updateLoginUI();
}

function openLogin() {
  $("login-modal").hidden = false;
  $("login-pass").value = "";
  $("login-pass").focus();
}

function closeLogin() {
  $("login-modal").hidden = true;
}

async function doLogin() {
  const pass = $("login-pass").value;
  try {
    const r = await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pass }),
    });
    authToken = r.token;
    sessionStorage.setItem("auth", r.token);
    updateLoginUI();
    closeLogin();
    toast("Logged in");
  } catch (e) { toast(e.message, true); }
}

function doLogout() {
  authToken = "";
  sessionStorage.removeItem("auth");
  updateLoginUI();
  toast("Logged out");
}

async function requireAdmin() {
  if (authToken) return true;
  openLogin();
  return false;
}

const withSem = (path) => path + (path.includes("?") ? "&" : "?") + "semester=" + encodeURIComponent(semester);

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  options = options || {};
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((body && body.detail) || `Request failed (${res.status})`);
  return body;
}

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = ""), 3200);
}

function colourOf(prog) {
  return COLORS[prog] || FALLBACK;
}

function clone(x) {
  return JSON.parse(JSON.stringify(x));
}

/* ---------------- tabs ---------------- */

function activateTab(name) {
  const sec = $("tab-" + name);
  if (!sec) name = "timetable";
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
}

document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    activateTab(btn.dataset.tab);
    history.replaceState(null, "", "#" + btn.dataset.tab);
  });
});
if (location.hash) activateTab(location.hash.slice(1));

/* ---------------- editable tables ---------------- */

function groupCols(kind) {
  if (kind !== "courses") return null;
  return showAdvancedCourses ? COURSE_GROUPS : COURSE_BASIC_GROUPS;
}

function visibleCols(kind) {
  const groups = groupCols(kind);
  if (!groups) return meta[COL_KEYS[kind]];
  return groups.flatMap((g) => g.cols);
}

const COURSE_SELECTS = {
  programme: () => [...new Set(state.cohorts.map((x) => x.programme).filter(Boolean))].sort(),
  level: () => [...new Set(state.cohorts.map((x) => String(x.level)).filter(Boolean))].sort(),
  online: () => ["yes", "no"],
  field_work: () => ["no", "yes"],
  lecturer: () => [...new Set(state.lecturers.map((x) => x.name).filter(Boolean))].sort(),
};

function sectionsForCourse(r) {
  const p = String(r.programme || "").trim();
  const lv = String(r.level || "").trim();
  if (!p || !lv) return "";
  const target = `${p}${lv}-`;
  return state.cohorts
    .filter((c) => String(c.programme) === p && String(c.level) === lv)
    .map((c) => target + String(c.section).toUpperCase())
    .sort()
    .join(",");
}

function defaults(kind) {
  if (kind === "courses") {
    return {
      course_code: "", course_name: "", programme: "", level: "100", cohort: "",
      lecturer: "", lecture_hours: "", practical_hours: "", credits: "",
      online: "no", hours_per_session: "2", sessions_per_week: "1", min_room_size: "",
      field_work: "no", sections: "", split: "", size: "", special: false,
    };
  }
  if (kind === "rooms") return { name: "", capacity: "", kind: "lecture" };
  return { programme: "", level: "", section: "A", size: "" };
}

function rowChanged(kind, row, savedRow) {
  const cols = meta[COL_KEYS[kind]];
  return cols.some((c) => String(row[c] ?? "") !== String(savedRow[c] ?? ""));
}

function updateBadge(kind) {
  const n = state[kind].reduce((acc, r, i) => acc + (rowChanged(kind, r, saved[kind][i]) ? 1 : 0), 0);
  const badge = $("badge-" + kind);
  badge.textContent = n;
  badge.classList.toggle("show", n > 0);
}

function tableFor(kind) {
  const allCols = meta[COL_KEYS[kind]];
  const cols = visibleCols(kind);
  const labels = LABELS[kind];
  const groups = groupCols(kind);
  const wrap = $("wrap-" + kind);
  const rows = state[kind];
  const q = ($("search-" + kind).value || "").toLowerCase();

  const visible = rows
    .map((r, i) => ({ r, i }))
    .filter(({ r }) => {
      if (!q) return true;
      return allCols.some((c) => String(r[c] || "").toLowerCase().includes(q));
    });

  const table = document.createElement("table");
  table.className = "grid-table";
  const thead = document.createElement("thead");

  const gr = document.createElement("tr");
  if (groups) {
    groups.forEach((g) => {
      const th = document.createElement("th");
      th.textContent = g.label;
      th.colSpan = g.cols.length;
      gr.appendChild(th);
    });
  } else {
    const th = document.createElement("th");
    th.textContent = labels[cols[0]] || cols[0];
    th.colSpan = cols.length;
    gr.appendChild(th);
  }
  const thActions = document.createElement("th");
  thActions.className = "row-actions";
  thActions.textContent = "";
  gr.appendChild(thActions);
  thead.appendChild(gr);

  const hr = document.createElement("tr");
  cols.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = labels[c] || c;
    if (c === "course_code") th.classList.add("sticky-l");
    hr.appendChild(th);
  });
  const thA2 = document.createElement("th");
  thA2.className = "row-actions";
  hr.appendChild(thA2);
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  visible.forEach(({ r, i }) => {
    const tr = document.createElement("tr");
    if (saved[kind][i] && rowChanged(kind, r, saved[kind][i])) tr.classList.add("edited");
    cols.forEach((c) => {
      const td = document.createElement("td");
      if (c === "course_code") td.classList.add("sticky-l");
      const set = (v) => {
        state[kind][i][c] = v;
        tr.classList.toggle("edited", saved[kind][i] && rowChanged(kind, r, saved[kind][i]));
        updateBadge(kind);
      };
      if (COURSE_SELECTS[c]) {
        const sel = document.createElement("select");
        const opts = COURSE_SELECTS[c](r);
        if (!opts.includes(String(r[c] ?? ""))) opts.unshift(String(r[c] ?? ""));
        opts.forEach((o) => sel.appendChild(new Option(o, o)));
        sel.value = String(r[c] ?? "");
        sel.addEventListener("change", () => {
          set(sel.value);
          if (c === "programme" || c === "level") {
            state[kind][i].sections = "";
          }
        });
        td.appendChild(sel);
      } else if (c === "sections") {
        if (r.special) {
          const input = document.createElement("input");
          input.value = r[c] === undefined || r[c] === null ? "" : r[c];
          input.placeholder = "e.g. CE100-A,CE100-B,PM100-A";
          input.title = "Class composition kept because it spans other programme(s) and cannot be derived from the Cohorts tab. Edit only if the combination changes.";
          input.addEventListener("input", () => set(input.value));
          td.appendChild(input);
        } else {
          const text = document.createElement("span");
          text.className = "derived";
          const secs = state[kind][i].sections || sectionsForCourse(r);
          text.textContent = secs || "auto";
          text.title = "Derived automatically from the Cohorts tab for this programme and level.";
          td.appendChild(text);
        }
      } else if (c === "course_code") {
        const input = document.createElement("input");
        input.value = r[c] === undefined || r[c] === null ? "" : r[c];
        input.placeholder = labels[c] || c;
        input.addEventListener("input", () => set(input.value));
        td.appendChild(input);
        if (r.special) {
          const badge = document.createElement("span");
          badge.className = "badge-warn";
          badge.textContent = "!";
          badge.title = "Shared with another programme (e.g. CE 158/PM 138): the class composition is kept as-is. See the Sections field under Advanced fields.";
          td.appendChild(badge);
        }
      } else {
        const input = document.createElement("input");
        input.value = r[c] === undefined || r[c] === null ? "" : r[c];
        input.placeholder = labels[c] || c;
        input.addEventListener("input", () => set(input.value));
        td.appendChild(input);
      }
      tr.appendChild(td);
    });
    const tdAct = document.createElement("td");
    tdAct.className = "row-actions";
    const dup = document.createElement("button");
    dup.title = "Duplicate row";
    dup.textContent = "⧉";
    dup.addEventListener("click", () => {
      state[kind].splice(i + 1, 0, clone(r));
      tableFor(kind);
    });
    const del = document.createElement("button");
    del.className = "del";
    del.title = "Delete row";
    del.textContent = "\u2715";
    del.addEventListener("click", () => {
      state[kind].splice(i, 1);
      tableFor(kind);
    });
    tdAct.append(dup, del);
    tr.appendChild(tdAct);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  wrap.replaceChildren(table);
  if (visible.length !== rows.length) {
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = `Showing ${visible.length} of ${rows.length} rows`;
    wrap.appendChild(note);
  }
  updateBadge(kind);
}

function wire(kind) {
  $("add-" + kind).addEventListener("click", async () => {
    if (!(await requireAdmin())) return;
    state[kind].push(defaults(kind));
    tableFor(kind);
    const firstInput = $("wrap-" + kind).querySelector("tbody input, tbody select");
    if (firstInput) firstInput.focus();
  });
  if (kind === "courses" && $("toggle-advanced-courses")) {
    $("toggle-advanced-courses").classList.toggle("active", showAdvancedCourses);
    $("toggle-advanced-courses").addEventListener("click", () => {
      showAdvancedCourses = !showAdvancedCourses;
      localStorage.setItem("courses-advanced", showAdvancedCourses ? "1" : "0");
      $("toggle-advanced-courses").classList.toggle("active", showAdvancedCourses);
      tableFor(kind);
    });
  }
  $("reload-" + kind).addEventListener("click", async () => {
    try {
      state[kind] = await api(withSem("/api/" + kind));
      saved[kind] = clone(state[kind]);
      tableFor(kind);
      toast("Reloaded");
    } catch (e) { toast(e.message, true); }
  });
  $("save-" + kind).addEventListener("click", async () => {
    if (!(await requireAdmin())) return;
    if (kind === "courses") {
      const touched = state[kind].filter((row, i) =>
        row.special && saved[kind][i] && rowChanged(kind, row, saved[kind][i]));
      if (touched.length) {
        const names = touched.slice(0, 4).map((r) => r.course_code + (r.course_name ? " \u2014 " + r.course_name : "")).join("\n");
        const extra = touched.length > 4 ? "\n\u2026and " + (touched.length - 4) + " more" : "";
        if (!confirm(
          "These courses are shared with another programme (the class is attached to another programme's course). " +
          "Changing them will rebuild the class from the sections shown under Advanced fields:\n\n" +
          names + extra + "\n\nContinue?")) return;
      }
    }
    const btn = $("save-" + kind);
    btn.disabled = true;
    try {
      const r = await apiAuthed(withSem("/api/" + kind), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state[kind]),
      });
      saved[kind] = clone(state[kind]);
      tableFor(kind);
      toast(`Saved ${r.rows} rows`);
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
  });
  $("search-" + kind).addEventListener("input", () => tableFor(kind));
}

/* ---------------- summary ---------------- */

function summaryCards(sum) {
  const wrap = $("summary");
  if (!sum || !sum.status) { wrap.replaceChildren(); return; }
  const cards = [];
  const ok = sum.status.startsWith("OPTIMAL") || sum.status.startsWith("FEASIBLE");
  cards.push({ k: "Status", v: sum.status, cls: ok ? "good" : (sum.status === "INFEASIBLE" ? "bad" : "") });
  cards.push({ k: "Objective", v: sum.objective === null || sum.objective === undefined ? "\u2014" : sum.objective });
  cards.push({ k: "Sessions", v: sum.sessions ?? "\u2014" });
  cards.push({ k: "Rooms", v: sum.rooms ?? "\u2014" });
  const totalConf = sum.conflicts ? Object.values(sum.conflicts).reduce((a, b) => a + b, 0) : 0;
  cards.push({ k: "Conflicts", v: totalConf, cls: totalConf === 0 ? "good" : "bad" });
  if (sum.lecturer_overlaps) cards.push({ k: "Lecturer overlaps (soft)", v: sum.lecturer_overlaps });
  if (sum.built_from) cards.push({ k: "Source", v: sum.built_from.replace(/\.xlsx$/i, ""), cls: "" });
  wrap.replaceChildren(...cards.map((c) => {
    const d = document.createElement("div");
    d.className = "card";
    const k = document.createElement("div");
    k.className = "k"; k.textContent = c.k;
    const v = document.createElement("div");
    v.className = "v" + (c.cls ? " " + c.cls : ""); v.textContent = c.v;
    d.append(k, v);
    return d;
  }));
}

/* ---------------- timetable grid ---------------- */

function renderLegend() {
  const wrap = $("legend");
  const progs = [...new Set(timetable.rows.map((r) => r.programme).filter(Boolean))].sort();
  const chips = progs.map((p) => {
    const c = document.createElement("span");
    c.className = "chip";
    const i = document.createElement("i");
    i.style.background = colourOf(p);
    c.append(i, document.createTextNode(p));
    return c;
  });
  const onl = document.createElement("span");
  onl.className = "chip";
  const oi = document.createElement("i");
  oi.style.background = "#fff";
  oi.style.border = "2px dashed #0d9488";
  onl.append(oi, document.createTextNode("Online / VLE"));
  chips.push(onl);
  if (timetable.rows.some((r) => r.room === "FIELD WORK")) {
    const fw = document.createElement("span");
    fw.className = "chip";
    const fi = document.createElement("i");
    fi.style.background = "#16a34a";
    fw.append(fi, document.createTextNode("Field Work"));
    chips.push(fw);
  }
  wrap.replaceChildren(...chips);
}

function slotIndex(time) {
  return meta.slot_times.indexOf(time);
}

function buildGridOptions() {
  const kind = $("grid-kind").value;
  const pick = $("grid-pick");
  pick.replaceChildren();
  if (kind === "lecturer") {
    [...new Set(timetable.rows.map((r) => r.lecturer).filter(Boolean))].sort()
      .forEach((l) => pick.appendChild(new Option(l, l)));
  } else if (kind === "room") {
    [...new Set(timetable.rows.map((r) => r.room).filter(Boolean))].sort()
      .forEach((r) => pick.appendChild(new Option(r, r)));
  } else if (kind === "day") {
    meta.days.forEach((d) => pick.appendChild(new Option(d, d)));
  } else {
    const sections = state.cohorts
      .filter((c) => c.programme && c.level)
      .map((c) => `${c.programme} ${c.level} ${c.section}`)
      .filter((v, i, a) => a.indexOf(v) === i)
      .sort();
    sections.forEach((s) => pick.appendChild(new Option(s, s)));
  }
  if (pick.options.length) pick.selectedIndex = 0;
  if (kind === "day") renderDayGrid();
  else renderGrid();
}

function gridRowsFor(kind, pickVal) {
  if (kind === "lecturer") return timetable.rows.filter((r) => r.lecturer === pickVal);
  if (kind === "room") return timetable.rows.filter((r) => r.room === pickVal);
  const [prog, lvl, sec] = pickVal.split(" ");
  const tags = sec === "A" ? ["A", "AB"] : sec === "B" ? ["B", "AB"] : ["AB"];
  const secId = `${prog}${lvl}-${sec}`;
  return timetable.rows.filter((r) => {
    if (String(r.programme) === prog && String(r.level) === lvl && tags.includes(r.cohort)) return true;
    return String(r.cohort).split(",").includes(secId);
  });
}

function makeBlock(r, small) {
  const div = document.createElement("div");
  const isOn = r.room === "ONLINE";
  const isFw = r.room === "FIELD WORK";
  div.className = "block" + (isOn ? " online" : isFw ? " field" : "");
  div.title = `${r.code} ${r.name}\n${r.programme} ${r.level} ${r.cohort}\n${r.lecturer}\n${r.room}\n${r.day} ${r.time}`;
  if (!isOn && !isFw) div.style.background = colourOf(r.programme);
  const b = document.createElement("b");
  b.textContent = r.code;
  const s = document.createElement("small");
  s.textContent = small || "";
  div.append(b, s);
  if (r.lecturer) {
    const em = document.createElement("em");
    em.textContent = r.lecturer;
    div.appendChild(em);
  }
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = r.type === "Online" ? "VLE" : r.room;
  div.appendChild(tag);
  return div;
}

function renderGrid() {
  const grid = $("grid");
  const kind = $("grid-kind").value;
  const rows = gridRowsFor(kind, $("grid-pick").value);

  const dayIdx = new Map(meta.days.map((d, i) => [d, i]));
  const start = {};
  rows.forEach((r) => {
    const di = dayIdx.get(r.day);
    const si = slotIndex(r.time);
    if (di === undefined || si === undefined) return;
    (start[di + "_" + si] = start[di + "_" + si] || []).push(r);
  });

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  const th0 = document.createElement("th");
  th0.className = "time"; th0.textContent = "Time";
  hr.appendChild(th0);
  const today = todayIdx();
  meta.days.forEach((d, di) => {
    const th = document.createElement("th");
    th.textContent = d;
    if (hlToday && di === today) th.classList.add("today");
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const skip = {};
  const nSlots = meta.slot_times.length;
  for (let si = 0; si < nSlots; si++) {
    const tr = document.createElement("tr");
    const td0 = document.createElement("td");
    td0.className = "time";
    td0.textContent = meta.slot_times[si];
    tr.appendChild(td0);
    meta.days.forEach((_, di) => {
      if (skip[di + "_" + si]) return;
      const list = start[di + "_" + si];
      const td = document.createElement("td");
      if (hlToday && di === today) td.classList.add("today");
      if (list && list.length) {
        const dur = Math.max(1, parseInt(list[0].duration, 10) || 1);
        if (list.length === 1) {
          const span = Math.min(dur, nSlots - si);
          if (span > 1) {
            td.rowSpan = span;
            for (let k = 1; k < span; k++) skip[di + "_" + (si + k)] = true;
          }
        }
        const r0 = list[0];
        const block = makeBlock(r0, kind === "lecturer" ? `${r0.programme} ${r0.level} ${r0.cohort}` : r0.room);
        if (list.length > 1) {
          block.classList.add("collide");
        }
        td.appendChild(block);
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  grid.replaceChildren(table);
}

function renderDayGrid() {
  const grid = $("grid");
  const day = $("grid-pick").value;
  const di = meta.days.indexOf(day);
  const nSlots = meta.slot_times.length;

  const capOf = {};
  const kindOf = {};
  state.rooms.forEach((r) => { capOf[r.name] = r.capacity; kindOf[r.name] = r.kind; });
  const rooms = state.rooms.map((r) => r.name);
  const byRoom = {};
  rooms.forEach((r) => (byRoom[r] = {}));
  const onlineBySlot = {};
  const fieldBySlot = {};
  timetable.rows.forEach((r) => {
    if (r.day !== day) return;
    const si = slotIndex(r.time);
    if (si === undefined) return;
    if (r.room === "ONLINE") {
      (onlineBySlot[si] = onlineBySlot[si] || []).push(r);
    } else if (r.room === "FIELD WORK") {
      (fieldBySlot[si] = fieldBySlot[si] || []).push(r);
    } else {
      (byRoom[r.room][si] = byRoom[r.room][si] || []).push(r);
    }
  });

  const colFor = (si) => (si >= 6 ? si + 2 : si + 1);

  const table = document.createElement("table");
  table.className = "day-grid";
  const thead = document.createElement("thead");

  const hr1 = document.createElement("tr");
  const hA = document.createElement("th");
  hA.className = "time room";
  hA.rowSpan = 2;
  hA.textContent = "Room";
  hr1.appendChild(hA);
  const OFFPEAK = new Set([0, 1, 10, 11]);
  for (let si = 0; si < nSlots; si++) {
    const th = document.createElement("th");
    if (si === 5) {
      th.colSpan = 2;
      th.textContent = "Period 6";
    } else {
      th.textContent = `Period ${si + 1}`;
    }
    if (OFFPEAK.has(si)) th.className = "offpeak";
    hr1.appendChild(th);
  }
  thead.appendChild(hr1);

  const hr2 = document.createElement("tr");
  for (let si = 0; si < nSlots; si++) {
    if (si === 5) {
      const th = document.createElement("th");
      th.textContent = "11:30-12:30";
      hr2.appendChild(th);
      const thb = document.createElement("th");
      thb.className = "break";
      thb.textContent = "\u2615 BREAK";
      hr2.appendChild(thb);
    } else {
      const th = document.createElement("th");
      th.textContent = meta.slot_times[si];
      if (OFFPEAK.has(si)) th.className = "offpeak";
      hr2.appendChild(th);
    }
  }
  thead.appendChild(hr2);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const roomLabel = (name) => {
    const cap = capOf[name];
    const kind = kindOf[name];
    if (cap) return `${name} (${cap})${kind === "lab" ? " \u00b7 Lab" : ""}`;
    return name;
  };
  const BREAK_COL = 7;
  const renderRow = (label, slotMap, kind) => {
    const tr = document.createElement("tr");
    const tdR = document.createElement("td");
    tdR.className = "time room" + (kind === "online" ? " online-row" : kind === "field" ? " field-row" : "");
    tdR.textContent = label;
    tr.appendChild(tdR);

    const cells = [];
    for (let ci = 1; ci <= 13; ci++) {
      const td = document.createElement("td");
      if (ci === BREAK_COL) {
        td.className = "break";
        td.textContent = "\u2615";
      }
      if (ci === 1 || ci === 2 || ci === 12 || ci === 13) td.classList.add("offpeak");
      cells[ci] = td;
    }
    const skipCols = new Set();
    const smallLabel = (r) => {
      const hrs = Math.max(1, parseInt(r.duration, 10) || 1);
      return (hrs > 1 ? hrs + "h · " : "") + `${r.programme} ${r.level}${r.cohort}`;
    };
    for (let si = 0; si < nSlots; si++) {
      const list = slotMap[si];
      if (!list || !list.length) continue;
      const dur = kind === "online" ? 1 : Math.max(1, parseInt(list[0].duration, 10) || 1);
      const c0 = colFor(si);
      let c1 = colFor(Math.min(si + dur - 1, nSlots - 1));
      if (c0 <= 6 && c1 >= 8) c1 = c0; // never span across the lunch-break column
      const cell = cells[c0];
      if (list.length === 1) {
        cell.appendChild(makeBlock(list[0], smallLabel(list[0])));
      } else {
        cell.classList.add("stacked");
        const st = document.createElement("div");
        st.className = "cell-stack";
        list.forEach((r) => st.appendChild(makeBlock(r, smallLabel(r))));
        cell.appendChild(st);
      }
      if (c1 > c0) {
        cell.colSpan = c1 - c0 + 1;
        for (let ci = c0 + 1; ci <= c1; ci++) skipCols.add(ci);
      }
    }
    for (let ci = 1; ci <= 13; ci++) {
      if (skipCols.has(ci)) continue;
      tr.appendChild(cells[ci]);
    }
    tbody.appendChild(tr);
  };

  const fwCount = Object.values(fieldBySlot).reduce((a, l) => a + l.length, 0);
  renderRow(`FIELD WORK${fwCount ? ` (${fwCount})` : ""}`, fieldBySlot, "field");
  rooms.forEach((r) => renderRow(roomLabel(r), byRoom[r] || {}, ""));
  renderRow("ONLINE (VLE)", onlineBySlot, "online");
  table.appendChild(tbody);
  grid.replaceChildren(table);
}

function todayIdx() {
  const d = new Date().getDay();
  return (d + 6) % 7; // Monday=0 ... Sunday=6
}

/* ---------------- data loading ---------------- */

async function loadTimetable() {
  try {
    timetable = await api(withSem("/api/timetable"));
    summaryCards(timetable.summary);
    renderLegend();
    buildGridOptions();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- solve overlay animations ---------------- */

const ANIM_MSGS = [
  "Loading data...",
  "Finding a conflict-free layout...",
  "Balancing room usage...",
  "Smoothing cohort gaps...",
  "Optimising lecturer workload...",
  "Finalising the timetable...",
];
let animTimer = null;
let elapsedTimer = null;
let solveT0 = 0;

function buildSolveLoaders() {
  const grid = $("solve-overlay").querySelector(".loader-grid");
  if (grid && !grid.children.length) {
    for (let i = 0; i < 42; i++) {
      const t = document.createElement("i");
      t.style.setProperty("--i", i);
      grid.appendChild(t);
    }
  }
  const dom = $("solve-overlay").querySelector(".loader-dominoes");
  if (dom && !dom.children.length) {
    for (let i = 0; i < 8; i++) {
      const s = document.createElement("span");
      s.style.setProperty("--i", i);
      dom.appendChild(s);
    }
  }
  const dots = $("solve-overlay").querySelector(".loader-dots");
  if (dots && !dots.children.length) {
    for (let i = 0; i < 5; i++) {
      const d = document.createElement("i");
      d.style.setProperty("--i", i);
      dots.appendChild(d);
    }
  }
}

function startSolveAnim() {
  buildSolveLoaders();
  const anims = [...$("solve-overlay").querySelectorAll(".loader")];
  let idx = 0;
  const cycle = () => {
    anims.forEach((a, i) => a.classList.toggle("active", i === idx));
    idx = (idx + 1) % anims.length;
  };
  cycle();
  animTimer = setInterval(cycle, 3000);
}

function stopSolveAnim() {
  clearInterval(animTimer);
  animTimer = null;
}

function startElapsed() {
  solveT0 = Date.now();
  elapsedTimer = setInterval(() => {
    const s = Math.round((Date.now() - solveT0) / 1000);
    $("solve-elapsed").textContent =
      `Elapsed ${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  }, 500);
}

function stopElapsed() {
  clearInterval(elapsedTimer);
  elapsedTimer = null;
  $("solve-elapsed").textContent = "";
}

function showSolveOverlay() {
  ["summary", "legend", "grid"].forEach((id) => { $(id).style.display = "none"; });
  $("solve-status").style.display = "none";
  $("solve-overlay").classList.add("show");
  startSolveAnim();
  startElapsed();
}

function renderSolveLive(job) {
  const el = $("solve-metrics");
  if (!el) return;
  const live = job.live;
  if (!live) {
    el.textContent = "";
    return;
  }
  const parts = [];
  if (live.phase) parts.push(live.phase === "phase1" ? "Phase 1 - conflict-free layout" : "Phase 2 - optimisation");
  if (typeof live.objective === "number") parts.push(`objective ${Math.round(live.objective)}`);
  if (typeof live.bound === "number" && live.bound !== live.objective) parts.push(`best bound ${Math.round(live.bound)}`);
  if (typeof live.conflicts === "number") parts.push(`${live.conflicts} search nodes`);
  if (typeof live.elapsed === "number") parts.push(`solver ${Math.floor(live.elapsed / 60)}m ${String(Math.floor(live.elapsed) % 60).padStart(2, "0")}s`);
  el.textContent = parts.join("  \u00b7  ");
}

function hideSolveOverlay() {
  stopSolveAnim();
  stopElapsed();
  $("solve-overlay").classList.remove("show");
  $("solve-status").style.display = "";
}

async function runSolve() {
  if (!(await requireAdmin())) return;
  const btn = $("run-solve");
  btn.disabled = true;
  $("solve-status").className = "status";
  showSolveOverlay();
  try {
    const { job_id } = await apiAuthed("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ time_limit: 300, semester }),
    });
    const poll = setInterval(async () => {
      try {
        const job = await api("/api/solve/" + job_id);
        if (job.status === "running") {
          if (job.progress) $("solve-text").textContent = job.progress;
          renderSolveLive(job);
          return;
        }
        clearInterval(poll);
        btn.disabled = false;
        hideSolveOverlay();
        if (job.ok) {
          $("solve-status").className = "status done";
          $("solve-status").textContent =
            `Solve complete: ${job.summary.status} (objective ${job.summary.objective ?? "\u2014"})` +
            (job.summary.built_from ? ` \u00b7 Built from ${job.summary.built_from}` : "") +
            (job.note ? ` \u00b7 ${job.note}` : "");
          await loadTimetable();
        } else {
          $("solve-status").className = "status error";
          $("solve-status").textContent = job.error || "Solve failed";
        }
      } catch (e) {
        clearInterval(poll);
        btn.disabled = false;
        hideSolveOverlay();
        $("solve-status").className = "status error";
        $("solve-status").textContent = e.message;
      }
    }, 2000);
  } catch (e) {
    btn.disabled = false;
    hideSolveOverlay();
    $("solve-status").className = "status error";
    $("solve-status").textContent = e.message;
  }
}

/* ---------------- init ---------------- */

async function loadLecturers() {
  const lecturers = await api("/api/lecturers");
  state.lecturers = lecturers;
  saved.lecturers = clone(lecturers);
  tableFor("lecturers");
}

async function loadSemesterData() {
  const [courses, rooms, cohorts] = await Promise.all([
    api(withSem("/api/courses")), api(withSem("/api/rooms")), api(withSem("/api/cohorts")),
  ]);
  state.courses = courses; state.rooms = rooms; state.cohorts = cohorts;
  saved.courses = clone(courses); saved.rooms = clone(rooms); saved.cohorts = clone(cohorts);
  tableFor("courses"); tableFor("rooms"); tableFor("cohorts");
}

async function switchSemester(next) {
  if (next === semester) return;
  semester = next;
  $("download-xlsx").href = withSem("/api/timetable.xlsx");
  $("publish-result").hidden = true;
  $("summary").replaceChildren();
  $("grid").replaceChildren();
  $("legend").replaceChildren();
  try {
    await loadSemesterData();
    await loadTimetable();
    toast(`Switched to ${meta.semesters[semester] || semester}`);
  } catch (e) { toast(e.message, true); }
}

async function init() {
  updateLoginUI();
  refreshAuth();
  try {
    const m = await api("/api/meta");
    Object.assign(meta, m);
    const sel = $("semester");
    sel.replaceChildren();
    Object.keys(meta.semesters).forEach((key) => sel.appendChild(new Option(meta.semesters[key], key)));
    sel.value = semester;
    $("download-xlsx").href = withSem("/api/timetable.xlsx");
    await loadLecturers();
    await loadSemesterData();
    activateTab(location.hash ? location.hash.slice(1) : "timetable");
    await loadTimetable();
  } catch (e) { toast(e.message, true); }
}

wire("courses"); wire("rooms"); wire("cohorts"); wire("lecturers");
$("run-solve").addEventListener("click", runSolve);
$("reload-timetable").addEventListener("click", loadTimetable);
$("grid-kind").addEventListener("change", buildGridOptions);
$("grid-pick").addEventListener("change", () => {
  if ($("grid-kind").value === "day") renderDayGrid();
  else renderGrid();
});
$("grid-today").addEventListener("click", () => {
  hlToday = !hlToday;
  $("grid-today").classList.toggle("active", hlToday);
  if ($("grid-kind").value === "day") renderDayGrid();
  else renderGrid();
});

$("login-btn").addEventListener("click", () => {
  if (authToken) doLogout();
  else openLogin();
});
$("login-cancel").addEventListener("click", closeLogin);
$("login-ok").addEventListener("click", doLogin);
$("login-pass").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});
$("login-modal").addEventListener("click", (e) => {
  if (e.target === $("login-modal")) closeLogin();
});
$("publish").addEventListener("click", async () => {
  if (!(await requireAdmin())) return;
  const box = $("publish-result");
  try {
    const r = await apiAuthed("/api/publish?semester=" + encodeURIComponent(semester), { method: "POST" });
    const url = new URL(r.url, location.origin).href;
    box.hidden = false;
    box.textContent = "Published! Share link for the other platform:";
    const code = document.createElement("code");
    code.textContent = url;
    box.appendChild(document.createElement("br"));
    box.appendChild(code);
    if (r.student_app) {
      const note = document.createElement("div");
      note.style.marginTop = "8px";
      note.textContent = r.student_app.ok
        ? `Mobile app updated: ${r.student_app.detail} classes pushed to ${new URL(meta.student_app_url || "", location.origin).host}`
        : `Mobile app push failed: ${r.student_app.detail}`;
      note.style.color = r.student_app.ok ? "#15803d" : "#b91c1c";
      box.appendChild(note);
    }
  } catch (e) { toast(e.message, true); }
});

$("semester").addEventListener("change", (e) => switchSemester(e.target.value));

init();
