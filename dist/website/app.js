const DATA_URL = "data/stats.json";

// Your GATE exam date -- computed from "169 days from 23 Aug 2026" per
// your Excel tracker. Double-check this against the actual admit-card /
// official date and adjust if it's off.
const EXAM_DATE = "2027-02-06";

// How many weeks back the pace chart looks, and how many of the most
// recent weeks count toward the "current pace" projection.
const PACE_WEEKS_SHOWN = 12;
const PACE_PROJECTION_WEEKS = 4;

const QUOTES = [
  "One question at a time. That's the whole game.",
  "Weak subjects don't stay weak \u2014 they just haven't had enough reps yet.",
  "Consistency beats intensity. Show up today.",
  "The gap between L3 and L1 is just repetition.",
  "Every pending question is just practice you haven't done yet.",
  "Progress isn't linear. Keep marking them anyway.",
  "Small daily gains compound into February.",
  "You don't need to feel ready. You need to start.",
  "The syllabus doesn't care about motivation. Show up anyway.",
  "Today's weak spot is next month's strong suit.",
  "Streaks aren't about perfection \u2014 they're about showing up.",
  "Every attempted question moves the needle, even the wrong ones.",
];

let STATE = null;   // parsed stats.json
let SCOPE = "all";  // "all" or a source id (string)

function renderQuote() {
  const el = document.getElementById("motivational-quote");
  if (!el) return;
  el.textContent = QUOTES[Math.floor(Math.random() * QUOTES.length)];
}

async function boot() {
  renderQuote();
  const res = await fetch(DATA_URL, { cache: "no-store" });
  STATE = await res.json();
  renderVolumeSwitch();
  renderCountdown();
  renderAll();
  wireShareButton();
}

function renderCountdown() {
  const el = document.getElementById("exam-countdown");
  if (!el) return;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const exam = new Date(EXAM_DATE + "T00:00:00");
  const days = Math.round((exam - today) / 86400000);
  if (Number.isNaN(days)) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = days > 0 ? `T\u2212${days} to GATE` : days === 0 ? "GATE is today" : "GATE has passed";
}

function currentSlice() {
  if (SCOPE === "all") return STATE.unified;
  return STATE.by_source[SCOPE];
}

function renderVolumeSwitch() {
  const el = document.getElementById("volume-switch");
  const items = [
    { id: "all", label: "All volumes" },
    ...STATE.sources.map((s) => ({ id: String(s.id), label: s.filename.replace(/\.pdf$/i, "") })),
  ];
  el.innerHTML = "";
  items.forEach((item) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = item.label;
    btn.className = "pill" + (SCOPE === item.id ? " active" : "");
    btn.addEventListener("click", () => {
      SCOPE = item.id;
      renderVolumeSwitch();
      renderAll();
    });
    el.appendChild(btn);
  });
}

function renderAll() {
  renderSummary();
  renderChapters();
  renderPace();
  renderActivity();
  renderFooter();
}

// ---------- pace chart + projection ----------

function weeklyBuckets(daily, weeksBack) {
  // Buckets are Monday-start weeks, oldest to newest, ending on the
  // current week. Weeks with no data still appear (as zero) so gaps in
  // your activity actually show up as gaps in the chart.
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const mondayOffset = (today.getDay() + 6) % 7;
  const thisWeekStart = new Date(today);
  thisWeekStart.setDate(thisWeekStart.getDate() - mondayOffset);

  const buckets = [];
  for (let i = weeksBack - 1; i >= 0; i--) {
    const start = new Date(thisWeekStart);
    start.setDate(start.getDate() - i * 7);
    buckets.push({ start, total: 0 });
  }

  daily.forEach((d) => {
    const dt = new Date(d.date + "T00:00:00");
    for (const b of buckets) {
      const end = new Date(b.start);
      end.setDate(end.getDate() + 7);
      if (dt >= b.start && dt < end) {
        b.total += d.total;
        break;
      }
    }
  });

  return buckets;
}

function renderPace() {
  const panel = document.getElementById("pace-panel");
  if (!panel) return;
  const slice = currentSlice();
  const daily = slice.activity.daily;
  const pending = slice.stats.NONE;

  const hasAnyData = daily.length > 0;
  const earliestDate = hasAnyData
    ? new Date(Math.min(...daily.map((d) => new Date(d.date + "T00:00:00"))))
    : null;
  const daysOfHistory = earliestDate ? Math.round((new Date() - earliestDate) / 86400000) : 0;
  const weeksBack = Math.max(1, Math.min(PACE_WEEKS_SHOWN, Math.ceil((daysOfHistory + 1) / 7)));

  const buckets = weeklyBuckets(daily, weeksBack);
  const maxVal = Math.max(1, ...buckets.map((b) => b.total));

  const barsHtml = buckets
    .map((b) => {
      const h = Math.round((b.total / maxVal) * 100);
      const label = b.start.toLocaleDateString("en-US", { day: "2-digit", month: "short" });
      return `
        <div class="pace-bar-col">
          <div class="pace-bar" style="height:${Math.max(h, b.total > 0 ? 4 : 0)}%" data-count="${b.total}" data-week="${label}"></div>
        </div>`;
    })
    .join("");

  document.getElementById("pace-chart").innerHTML = hasAnyData
    ? barsHtml
    : `<div class="empty">Nothing marked yet \u2014 this fills in once you start tracking.</div>`;

  document.querySelectorAll(".pace-bar").forEach((bar) => {
    bar.addEventListener("mouseenter", () => {
      document.getElementById("pace-hint").textContent = `Week of ${bar.dataset.week}: ${bar.dataset.count} marked`;
    });
  });

  // Projection off the last PACE_PROJECTION_WEEKS full weeks (excludes
  // the current, still-in-progress week so a slow Monday doesn't
  // artificially tank the average).
  const completeBuckets = buckets.slice(0, -1);
  const recent = completeBuckets.slice(-PACE_PROJECTION_WEEKS);
  const avgPace = recent.length ? recent.reduce((sum, b) => sum + b.total, 0) / recent.length : 0;

  const projectionEl = document.getElementById("pace-projection");
  if (!hasAnyData || avgPace <= 0) {
    projectionEl.textContent = pending > 0
      ? "Mark a few more over the next week or two and a pace projection will show up here."
      : "Everything's attempted at least once \u2014 nothing left to project.";
  } else if (pending <= 0) {
    projectionEl.textContent = "Everything's attempted at least once. Nice.";
  } else {
    const weeksNeeded = Math.ceil(pending / avgPace);
    const projected = new Date();
    projected.setDate(projected.getDate() + weeksNeeded * 7);
    const projectedLabel = projected.toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "numeric" });
    projectionEl.innerHTML = `At ~<b>${Math.round(avgPace)}/week</b> (last ${recent.length} week${recent.length === 1 ? "" : "s"}), the ${pending} pending would take about <b>${weeksNeeded} more week${weeksNeeded === 1 ? "" : "s"}</b> \u2014 around <b>${projectedLabel}</b>.`;
  }
}

// ---------- share card ----------

function wireShareButton() {
  const btn = document.getElementById("share-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const statusEl = document.getElementById("share-status");
    statusEl.textContent = "";
    try {
      const blob = await buildShareCard();
      let copied = false;
      if (navigator.clipboard && window.ClipboardItem) {
        try {
          await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
          copied = true;
        } catch (e) {
          copied = false;
        }
      }
      if (copied) {
        statusEl.textContent = "Copied to clipboard.";
      } else {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "gate-progress.png";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        statusEl.textContent = "Downloaded.";
      }
    } catch (e) {
      statusEl.textContent = "Couldn't generate the image.";
    }
  });
}

function buildShareCard() {
  const slice = currentSlice();
  const s = slice.stats;
  const attempted = s.L1 + s.L2 + s.L3;
  const pct = s.total ? Math.round((attempted / s.total) * 100) : 0;
  const weakest = slice.by_chapter[0];
  const days = (() => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const exam = new Date(EXAM_DATE + "T00:00:00");
    return Math.round((exam - today) / 86400000);
  })();

  const canvas = document.createElement("canvas");
  canvas.width = 1200;
  canvas.height = 630;
  const ctx = canvas.getContext("2d");

  ctx.fillStyle = "#f5f6f8";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.fillStyle = "#2563eb";
  ctx.font = "600 22px monospace";
  ctx.fillText("GATE CSE TRACKER", 60, 80);

  ctx.fillStyle = "#1f2430";
  ctx.font = "800 56px sans-serif";
  ctx.fillText(`${pct}% attempted`, 60, 160);

  ctx.font = "600 26px sans-serif";
  ctx.fillStyle = "#6b7280";
  ctx.fillText(`${attempted} / ${s.total} questions`, 60, 205);

  const stats = [
    { label: "L1 easy", value: s.L1, color: "#2e7d32" },
    { label: "L2 forgot", value: s.L2, color: "#e65100" },
    { label: "L3 didn't understand", value: s.L3, color: "#c62828" },
    { label: "streak", value: `${slice.activity.current_streak}d`, color: "#2563eb" },
  ];
  let x = 60;
  stats.forEach((st) => {
    ctx.fillStyle = st.color;
    ctx.font = "800 38px monospace";
    ctx.fillText(String(st.value), x, 300);
    ctx.fillStyle = "#6b7280";
    ctx.font = "500 15px sans-serif";
    ctx.fillText(st.label, x, 325);
    x += 260;
  });

  if (weakest) {
    ctx.fillStyle = "#1f2430";
    ctx.font = "600 20px sans-serif";
    ctx.fillText("Weakest subject right now:", 60, 400);
    ctx.font = "700 26px sans-serif";
    ctx.fillStyle = "#c62828";
    ctx.fillText(`${weakest.chapter_name} (${weakest.pct_done}% done)`, 60, 435);
  }

  if (Number.isFinite(days) && days > 0) {
    ctx.fillStyle = "#2563eb";
    ctx.font = "700 22px monospace";
    ctx.fillText(`T\u2212${days} to GATE`, 60, 500);
  }

  ctx.fillStyle = "#9aa1ac";
  ctx.font = "13px monospace";
  ctx.fillText(`Generated ${new Date().toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "numeric" })}`, 60, 580);

  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

function renderSummary() {
  const s = currentSlice().stats;
  const attempted = s.L1 + s.L2 + s.L3;
  const pct = s.total ? Math.round((attempted / s.total) * 100) : 0;
  const cards = [
    { label: "Total questions", value: s.total, cls: "" },
    { label: "Attempted", value: `${attempted} (${pct}%)`, cls: "accent" },
    { label: "Pending", value: s.NONE, cls: "muted" },
    { label: "L1 \u00b7 easy", value: s.L1, cls: "l1" },
    { label: "L2 \u00b7 forgot something", value: s.L2, cls: "l2" },
    { label: "L3 \u00b7 didn't understand", value: s.L3, cls: "l3" },
  ];
  document.getElementById("summary-grid").innerHTML = cards
    .map(
      (c) => `
      <div class="stat-card ${c.cls}">
        <div class="stat-value">${c.value}</div>
        <div class="stat-label">${c.label}</div>
      </div>`
    )
    .join("");
}

function renderChapters() {
  const chapters = currentSlice().by_chapter;
  const list = document.getElementById("chapter-list");
  if (!chapters.length) {
    list.innerHTML = `<div class="empty">No questions indexed yet.</div>`;
    return;
  }
  list.innerHTML = chapters
    .map((c, i) => {
      const t = c.total || 1;
      const w = (n) => ((n / t) * 100).toFixed(2);
      return `
      <div class="chapter-row">
        <div class="chapter-rank">${String(i + 1).padStart(2, "0")}</div>
        <div class="chapter-main">
          <div class="chapter-top">
            <span class="chapter-name">${escapeHtml(c.chapter_name)}${
        c.urgent ? '<span class="urgent-dot" title="5+ questions marked didn\'t understand"></span>' : ""
      }</span>
            <span class="chapter-pct">${c.pct_done}%</span>
          </div>
          <div class="bar">
            <span style="width:${w(c.L1)}%; background:var(--l1)" title="L1: ${c.L1}"></span>
            <span style="width:${w(c.L2)}%; background:var(--l2)" title="L2: ${c.L2}"></span>
            <span style="width:${w(c.L3)}%; background:var(--l3)" title="L3: ${c.L3}"></span>
          </div>
          <div class="chapter-meta">
            ${c.total - c.NONE}/${c.total} attempted &middot; ${c.NONE} pending
            ${
              c.focus
                ? ` &middot; <a class="focus-link" href="${escapeHtml(c.focus.url)}" target="_blank" rel="noopener noreferrer">Next: ${escapeHtml(c.focus.question_id)} \u2192</a>`
                : ""
            }
          </div>
        </div>
      </div>`;
    })
    .join("");
}

function renderActivity() {
  const a = currentSlice().activity;
  const cards = [
    { label: "Current streak", value: `${a.current_streak} day${a.current_streak === 1 ? "" : "s"}`, cls: "accent" },
    { label: "Longest streak", value: `${a.longest_streak} day${a.longest_streak === 1 ? "" : "s"}`, cls: "" },
    { label: "Active days", value: a.active_days, cls: "" },
    { label: "Questions marked", value: a.total_marked, cls: "" },
  ];
  document.getElementById("activity-cards").innerHTML = cards
    .map(
      (c) => `
      <div class="stat-card small ${c.cls}">
        <div class="stat-value">${c.value}</div>
        <div class="stat-label">${c.label}</div>
      </div>`
    )
    .join("");

  renderHeatmap(a.daily);
  renderDailyCalendar(a.daily);
}

function heatColor(count) {
  if (count <= 0) return "#ebedf0";
  if (count <= 1) return "#9be9a8";
  if (count <= 3) return "#40c463";
  if (count <= 6) return "#30a14e";
  return "#216e39";
}

function heatTextColor(count) {
  return count > 3 ? "#ffffff" : "#1f2430";
}

function renderHeatmap(daily) {
  const countsByDate = Object.fromEntries(daily.map((d) => [d.date, d.total]));
  const weeks = 18;

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const start = new Date(today);
  start.setDate(start.getDate() - (weeks * 7 - 1));
  const mondayOffset = (start.getDay() + 6) % 7; // 0 = Monday
  start.setDate(start.getDate() - mondayOffset);

  const cols = [];
  let cursor = new Date(start);
  while (cursor <= today) {
    const col = [];
    for (let row = 0; row < 7; row++) {
      const d = new Date(cursor);
      d.setDate(d.getDate() + row);
      col.push(d > today ? null : d);
    }
    cols.push(col);
    cursor.setDate(cursor.getDate() + 7);
  }

  const dayLabelsEl = document.querySelector(".heatmap-daylabels") || document.createElement("div");
  const dayLabels = ["Mon", "", "Wed", "", "Fri", "", ""];
  dayLabelsEl.className = "heatmap-daylabels";
  dayLabelsEl.innerHTML = dayLabels.map((l) => `<span>${l}</span>`).join("");

  let lastMonth = null;
  const colsHtml = cols
    .map((col) => {
      const first = col.find((d) => d);
      let monthLabel = "";
      if (first && first.getMonth() !== lastMonth) {
        monthLabel = first.toLocaleString("en-US", { month: "short" });
        lastMonth = first.getMonth();
      }
      const cellsHtml = col
        .map((d) => {
          if (!d) return `<span class="heatmap-cell empty"></span>`;
          const iso = toIsoDate(d);
          const cnt = countsByDate[iso] || 0;
          return `<span class="heatmap-cell" data-date="${iso}" data-count="${cnt}" style="background:${heatColor(cnt)}"></span>`;
        })
        .join("");
      return `<div class="heatmap-col"><span class="heatmap-month">${monthLabel}</span>${cellsHtml}</div>`;
    })
    .join("");

  const el = document.getElementById("heatmap");
  el.innerHTML = "";
  el.appendChild(dayLabelsEl);
  const gridEl = document.createElement("div");
  gridEl.className = "heatmap-grid";
  gridEl.innerHTML = colsHtml;
  el.appendChild(gridEl);

  const hint = document.getElementById("heatmap-hint");
  el.querySelectorAll(".heatmap-cell[data-date]").forEach((cell) => {
    cell.addEventListener("mouseenter", () => {
      const d = new Date(cell.dataset.date + "T00:00:00");
      const label = d.toLocaleDateString("en-US", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
      hint.textContent = `${label}: ${cell.dataset.count} question(s) marked`;
    });
  });
}

function renderDailyCalendar(daily) {
  const countsByDate = Object.fromEntries(daily.map((d) => [d.date, d.total]));

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const dates = daily.map((d) => new Date(d.date + "T00:00:00"));
  const earliest = dates.length ? new Date(Math.min(...dates)) : new Date(today);

  const start = new Date(earliest.getFullYear(), earliest.getMonth(), 1);
  const end = new Date(today.getFullYear(), today.getMonth(), 1);

  const months = [];
  const cursor = new Date(start);
  while (cursor <= end) {
    months.push(new Date(cursor));
    cursor.setMonth(cursor.getMonth() + 1);
  }

  const weekdayLabels = ["S", "M", "T", "W", "T", "F", "S"];

  const monthsHtml = months
    .map((monthDate) => {
      const year = monthDate.getFullYear();
      const month = monthDate.getMonth();
      const daysInMonth = new Date(year, month + 1, 0).getDate();
      const leadingBlanks = new Date(year, month, 1).getDay(); // 0 = Sunday

      const cells = [];
      for (let i = 0; i < leadingBlanks; i++) cells.push(null);
      for (let day = 1; day <= daysInMonth; day++) cells.push(new Date(year, month, day));

      const cellsHtml = cells
        .map((d) => {
          if (!d) return `<span class="cal-cell pad"></span>`;
          if (d > today) return `<span class="cal-cell future">${d.getDate()}</span>`;
          const iso = toIsoDate(d);
          const cnt = countsByDate[iso] || 0;
          const bg = heatColor(cnt);
          const fg = heatTextColor(cnt);
          return `<span class="cal-cell" data-date="${iso}" data-count="${cnt}" style="background:${bg}; color:${fg}">${d.getDate()}</span>`;
        })
        .join("");

      const title = monthDate.toLocaleString("en-US", { month: "long", year: "numeric" });

      return `
        <div class="cal-month">
          <div class="cal-month-title">${title}</div>
          <div class="cal-weekdays">${weekdayLabels.map((l) => `<span>${l}</span>`).join("")}</div>
          <div class="cal-grid">${cellsHtml}</div>
        </div>`;
    })
    .join("");

  const el = document.getElementById("daily-calendar");
  el.innerHTML = monthsHtml || `<div class="empty">Nothing marked yet.</div>`;

  const hint = document.getElementById("daily-calendar-hint");
  el.querySelectorAll(".cal-cell[data-date]").forEach((cell) => {
    cell.addEventListener("mouseenter", () => {
      const d = new Date(cell.dataset.date + "T00:00:00");
      const label = d.toLocaleDateString("en-US", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
      hint.textContent = `${label}: ${cell.dataset.count} question(s) marked`;
    });
  });
}

function toIsoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function renderFooter() {
  const gen = new Date(STATE.generated_at);
  document.getElementById("synced-at").textContent = `Synced ${gen.toLocaleString()}`;
  document.getElementById("footer-total").textContent = `${STATE.unified.stats.total} questions across ${STATE.sources.length} volume(s)`;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot();
