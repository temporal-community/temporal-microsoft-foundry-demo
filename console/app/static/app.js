"use strict";

/* Booth console page. Everything the model wrote is untrusted: it reaches the document only
   through textContent and the small markdown renderer below, never through innerHTML. */

(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const UP = new Set(["starting", "working", "idle", "releasing"]);
  const PHASE = {
    starting: { label: "Starting", tone: "active" },
    planning: { label: "Planning", tone: "active" },
    awaiting_approval: { label: "Waiting for approval", tone: "waiting" },
    researching: { label: "Researching", tone: "active" },
    writing: { label: "Writing the brief", tone: "active" },
    waiting_for_follow_up: { label: "Waiting for the follow-up", tone: "waiting" },
    writing_follow_up: { label: "Writing the follow-up", tone: "active" },
    completed: { label: "Completed", tone: "done" },
    rejected: { label: "Rejected", tone: "bad" },
  };
  const CLOSED = { FAILED: "Failed", TERMINATED: "Terminated", TIMED_OUT: "Timed out", CANCELED: "Canceled" };
  const SANDBOX_STATE = {
    starting: "Starting", working: "Working", idle: "Idle", releasing: "Stopping",
    released: "Released", gone: "Gone", unknown: "No report",
  };
  const WAKE_FOR = {
    start: "for the new run", signal: "for the approval", timer: "for the timer",
    activity: "for more work", activity_result: "to continue", retry: "for a retry",
  };

  const wakeFor = (cause, run) => {
    const decision = run && (run.decision || run.decision_sent);
    if (cause === "signal" && decision && decision.approved === false) return "for the rejection";
    return WAKE_FOR[cause] || "";
  };

  const state = {
    config: null,
    runId: null,
    run: null,
    boxes: { up: 0, sandboxes: [] },
    followUp: 60,
    clockOffset: 0, // server time minus this browser's time, in ms
    sig: {},
    shown: new Set(),
    firstRender: true, // the first draw of a run jumps to the right place; later ones glide
    revealNext: null,
    planEditFor: null,
    decisionSent: {},
    pollError: false, // the run view is showing a message about a failed read
    pending: {}, // sandbox short id -> note about a presenter action in flight
  };

  // ------------------------------------------------------------------ plumbing

  /* The page keeps asking once a second, so a lost connection mends itself. Until it does,
     the header says so: the counters keep moving and would otherwise look live. Each kind of
     read is tracked by itself, so one that works does not hide one that fails. */
  const failing = new Set();
  function reconnecting(read, lost) {
    if (read) {
      if (lost) failing.add(read);
      else failing.delete(read);
    }
    const value = failing.size ? "1" : "0";
    if (document.body.dataset.offline !== value) document.body.dataset.offline = value;
  }

  // One console per booth station can sit behind a path prefix (/2/, /3/): requests go to
  // the directory this page was served from.
  const BASE = location.pathname.replace(/\/[^/]*$/, "");

  async function api(path, body) {
    const options = { method: body === undefined ? "GET" : "POST", credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    // Reads repeat, so a failed one is retried by the next; a failed POST is reported where it was made.
    const read = body !== undefined ? null : path.startsWith("/api/runs/") ? "run" : path;
    let response;
    try {
      response = await fetch(BASE + path, options);
    } catch (error) {
      reconnecting(read, true);
      const failure = new Error("The console is not reachable.");
      failure.status = 0;
      throw failure;
    }
    // 502 to 504: the proxy cannot reach the console, or the console cannot reach Temporal.
    reconnecting(read, response.status >= 502 && response.status <= 504);
    let data = null;
    try { data = await response.json(); } catch (error) { /* no body */ }
    if (!response.ok) {
      // The API answers in lower-case fragments; the page shows them as sentences.
      const text = data && data.error ? `${data.error.charAt(0).toUpperCase()}${data.error.slice(1)}${/[.!?]$/.test(data.error) ? "" : "."}` : "";
      const failure = new Error(text || `Request failed (${response.status}).`);
      failure.status = response.status;
      throw failure;
    }
    return data;
  }

  const now = () => Date.now() + state.clockOffset;
  const syncClock = (serverTime) => { if (serverTime) state.clockOffset = Date.parse(serverTime) - Date.now(); };
  const changed = (key, value) => {
    const signature = JSON.stringify(value);
    if (state.sig[key] === signature) return false;
    state.sig[key] = signature;
    return true;
  };
  const clockTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "");
  const seconds = (value) => (value >= 10 ? `${Math.round(value)} s` : `${value.toFixed(1)} s`);
  const shortId = (id) => {
    if (!id) return "";
    if (id.includes("/")) return id.split("/").slice(-2).join("/");
    return id.slice(0, 8);
  };
  const ago = (iso) => {
    const minutes = Math.max(0, Math.round((now() - Date.parse(iso)) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    return `${Math.round(minutes / 60)} h ago`;
  };

  function show(view) {
    document.body.dataset.view = view;
    $("login-view").hidden = view !== "login";
    $("app-view").hidden = view === "login" || view === "loading";
    $("ready-view").hidden = view !== "ready";
    $("run-view").hidden = view !== "run";
  }

  // ------------------------------------------------------------------ markdown

  const INLINE = /(`[^`]+`)|(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*]+)\*\*)|(\*([^*\s][^*]*)\*)/g;

  function safeUrl(text) {
    try {
      const url = new URL(text);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (error) {
      return null;
    }
  }

  function inline(text, parent) {
    let last = 0;
    // matchAll works on its own copy of the pattern, so the recursion below (bold text that
    // contains a link) cannot disturb this loop's position.
    for (const match of text.matchAll(INLINE)) {
      if (match.index > last) parent.append(text.slice(last, match.index));
      if (match[1]) {
        parent.append(el("code", null, match[1].slice(1, -1)));
      } else if (match[2]) {
        const url = safeUrl(match[4]);
        if (url) {
          const link = el("a", null, match[3]);
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          parent.append(link);
        } else {
          parent.append(match[3]);
        }
      } else if (match[5]) {
        const strong = el("strong");
        inline(match[6], strong);
        parent.append(strong);
      } else if (match[7]) {
        const emphasis = el("em");
        inline(match[8], emphasis);
        parent.append(emphasis);
      }
      last = match.index + match[0].length;
    }
    if (last < text.length) parent.append(text.slice(last));
  }

  /* Headings, paragraphs, bullet and numbered lists, bold, italics, inline code and
     http(s) links. Anything else stays text. */
  function markdown(text, into) {
    const fragment = document.createDocumentFragment();
    let list = null;
    let listKind = null;
    let paragraph = [];
    const flush = () => {
      if (!paragraph.length) return;
      const p = el("p");
      inline(paragraph.join(" "), p);
      fragment.append(p);
      paragraph = [];
    };
    const item = (kind, content) => {
      flush();
      if (listKind !== kind) {
        list = el(kind);
        listKind = kind;
        fragment.append(list);
      }
      const li = el("li");
      inline(content, li);
      list.append(li);
    };
    for (const raw of String(text || "").replace(/\r\n?/g, "\n").split("\n")) {
      const line = raw.trimEnd();
      let match;
      if (!line.trim()) {
        flush();
        list = null;
        listKind = null;
      } else if ((match = /^\s{0,3}(#{1,4})\s+(.*?)\s*#*$/.exec(line))) {
        flush();
        list = null;
        listKind = null;
        const heading = el(`h${Math.min(match[1].length + 2, 5)}`);
        inline(match[2], heading);
        fragment.append(heading);
      } else if ((match = /^\s*[-*•]\s+(.*)$/.exec(line))) {
        item("ul", match[1]);
      } else if ((match = /^\s*\d{1,3}[.)]\s+(.*)$/.exec(line))) {
        item("ol", match[1]);
      } else if (list && /^\s{2,}\S/.test(raw)) {
        list.lastChild.append(" ");
        inline(line.trim(), list.lastChild);
      } else {
        list = null;
        listKind = null;
        paragraph.push(line.trim());
      }
    }
    flush();
    into.classList.add("md");
    into.replaceChildren(fragment);
  }
  window.tfdRenderMarkdown = markdown; // for tests/test_page.py; it only ever writes text nodes

  // ------------------------------------------------------------------ live text

  /* Elements carrying data-since or data-until are counters; one ticker rewrites their
     text, so re-rendering is not needed for a number to move. */
  function tick() {
    for (const node of document.querySelectorAll("[data-since]")) {
      const elapsed = Math.max(0, (now() - Date.parse(node.dataset.since)) / 1000);
      node.textContent = node.dataset.precise ? `${elapsed.toFixed(1)} s` : `${Math.floor(elapsed)} s`;
    }
    for (const node of document.querySelectorAll("[data-until]")) {
      const left = Math.max(0, Math.ceil((Date.parse(node.dataset.until) - now()) / 1000));
      node.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    }
  }

  function reveal(block) {
    // Scroll to a section the first time it appears, then leave the reader alone.
    if (state.shown.has(block.id)) return;
    state.shown.add(block.id);
    state.revealNext = block;
  }

  function settleScroll(run) {
    const first = state.firstRender;
    state.firstRender = false;
    let block = state.revealNext;
    state.revealNext = null;
    if (!block) return;
    // Opening a finished run: show the brief, which is what it was for.
    if (first && run.status !== "RUNNING" && !$("report-block").hidden) block = $("report-block");
    if (block.id === "plan-block") return; // the plan sits at the top already
    const where = block.id === "follow-up-block" && !first ? "center" : "start";
    requestAnimationFrame(() => block.scrollIntoView({ behavior: first ? "instant" : "smooth", block: where }));
  }

  // ------------------------------------------------------------------ ready view

  function renderConfig() {
    const config = state.config;
    $("meta").textContent = `namespace ${config.temporal_namespace} · task queue ${config.task_queue}`;
    $("foundry-meta").textContent = [config.agent_name, config.model_name, config.region].filter(Boolean).join(" · ");
    $("question-input").maxLength = config.limits.max_question_length;

    const presets = $("presets");
    presets.replaceChildren();
    for (const question of config.presets) {
      const button = el("button", "preset", question);
      button.type = "button";
      button.dataset.testid = "preset-question";
      button.addEventListener("click", () => {
        $("question-input").value = question;
        $("question-input").focus();
      });
      presets.append(button);
    }

    state.followUp = config.follow_up_default;
    const choice = $("follow-up-choice");
    choice.replaceChildren();
    for (const value of config.follow_up_choices) {
      const button = el("button", null, value ? `Follow up after ${value} s` : "No follow-up");
      button.type = "button";
      button.setAttribute("role", "radio");
      button.dataset.value = String(value);
      button.addEventListener("click", () => {
        state.followUp = value;
        renderFollowUpChoice();
      });
      choice.append(button);
    }
    renderFollowUpChoice();
  }

  function renderFollowUpChoice() {
    for (const button of $("follow-up-choice").children) {
      button.setAttribute("aria-checked", String(Number(button.dataset.value) === state.followUp));
    }
  }

  function renderRecent(runs) {
    if (!changed("recent", runs.map((run) => [run.id, run.status, run.phase]))) return;
    const list = $("recent");
    list.replaceChildren();
    $("recent-title").hidden = runs.length === 0;
    for (const run of runs.slice(0, 6)) {
      const button = el("button");
      button.type = "button";
      button.append(
        el("span", "q", run.question || run.id),
        el("span", "p", run.status === "RUNNING" ? (PHASE[run.phase] || PHASE.starting).label : (CLOSED[run.status] || "Completed")),
        el("span", "t", run.started_at ? ago(run.started_at) : ""),
      );
      button.addEventListener("click", () => openRun(run.id));
      const li = el("li");
      li.append(button);
      list.append(li);
    }
  }

  // ------------------------------------------------------------------ run view

  /* The decision for a run, from the first place that has it: the workflow's memo, the Signal
     in Temporal's history, or this page a moment after the button was pressed. */
  function decisionFor(run) {
    return run.decision || run.decision_sent || state.decisionSent[run.id] || null;
  }

  function phaseOf(run) {
    if (run.status !== "RUNNING" && run.status !== "COMPLETED") {
      return { key: "failed", label: CLOSED[run.status] || "Failed", tone: "bad" };
    }
    const key = PHASE[run.phase] ? run.phase : "starting";
    const phase = { key, ...PHASE[key] };
    const decision = key === "awaiting_approval" && run.status === "RUNNING" ? decisionFor(run) : null;
    if (decision) {
      // Temporal has the Signal; the workflow has not run yet, so its memo still says waiting.
      phase.decided = decision.approved ? "approved" : "rejected";
      phase.label = decision.approved ? "Approved, waking Foundry" : "Rejected, closing the run";
      phase.tone = "active";
    }
    return phase;
  }

  function renderSteps(run, phase) {
    const names = ["Plan", "Approval", "Research", "Brief"];
    if (run.follow_up_after_seconds > 0 || run.follow_up_due || run.follow_up) names.push("Follow-up");
    const at = { starting: 0, planning: 0, awaiting_approval: 1, rejected: 1, researching: 2, writing: 3, waiting_for_follow_up: 4, writing_follow_up: 4 };
    let current = phase.key === "completed" ? names.length : at[run.phase] !== undefined ? at[run.phase] : 0;
    if (phase.key === "failed" && at[run.phase] === undefined) current = 0;
    if (phase.decided === "approved") current = 2;
    const states = names.map((_, index) => {
      if (index < current) return "done";
      if (index > current) return "todo";
      if (phase.tone === "bad") return "bad";
      return phase.tone === "waiting" ? "waiting" : "current";
    });
    if (!changed("steps", [names, states])) return;
    const steps = $("steps");
    steps.replaceChildren(...names.map((name, index) => {
      const li = el("li", null, name);
      li.dataset.state = states[index];
      return li;
    }));
  }

  function statusLine(run, phase) {
    const line = $("status-line");
    const say = (...parts) => line.replaceChildren(...parts);
    const strong = (text) => el("strong", null, text);
    const key = [phase.key, phase.decided, run.failure, run.research.length, run.follow_up_due, run.sandboxes.length];
    if (!changed("status", key)) return;
    switch (phase.key) {
      case "starting":
        return say("The run is recorded in Temporal. ", strong("Temporal is waking a Foundry sandbox."));
      case "planning":
        return say("The planner agent is drafting a plan on Foundry.");
      case "awaiting_approval":
        if (phase.decided === "approved") return say(strong("Approved."), " Temporal recorded the decision and is waking Foundry for the research.");
        if (phase.decided === "rejected") return say(strong("Rejected."), " Temporal recorded the decision and is waking Foundry to close the run.");
        return say(strong("Waiting for you."), " The plan is held in Temporal, so no sandbox is needed while it waits.");
      case "researching":
        return say(strong(`${run.research.length} researcher agents`), " are searching the web in parallel.");
      case "writing":
        return say("The writer agent is turning the findings into a brief.");
      case "waiting_for_follow_up": {
        if (!run.follow_up_due) return say("A durable timer is running in Temporal. No sandbox is needed while it waits.");
        const countdown = el("strong");
        countdown.dataset.until = run.follow_up_due;
        return say("The brief is done. A durable timer is running in Temporal: ", countdown, " left. No sandbox is needed while it waits.");
      }
      case "writing_follow_up":
        return say("The timer fired and Temporal woke Foundry. ", strong("The follow-up agent is checking what changed."));
      case "completed":
        return say(strong("Done."), ` The run used ${run.sandboxes.length} Foundry sandbox${run.sandboxes.length === 1 ? "" : "es"}, each only while there was work.`);
      case "rejected":
        return say("The plan was rejected. Nothing else will run.");
      default:
        return say(strong("The run stopped: "), run.failure || "no reason was recorded.");
    }
  }

  function fitHeight(area) {
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight + 2}px`;
  }
  const fitPlan = () => { for (const area of $("plan").querySelectorAll("textarea")) fitHeight(area); };

  function renderPlan(run, phase) {
    const block = $("plan-block");
    if (!run.plan) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    reveal(block);
    const decision = decisionFor(run);
    const editable = phase.key === "awaiting_approval" && !decision;
    const box = $("plan");

    if (editable) {
      if (state.planEditFor === run.id) return; // the visitor may be typing: leave it alone
      state.planEditFor = run.id;
      state.sig.plan = null;
      const list = el("ol", "plan-list");
      for (const question of run.plan.subquestions) {
        // A text area, so a long sub-question wraps on a laptop instead of running off the box.
        const input = el("textarea");
        input.rows = 1;
        input.value = question;
        input.maxLength = 200;
        input.setAttribute("aria-label", "Sub-question");
        input.addEventListener("input", () => fitHeight(input));
        input.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
        const li = el("li");
        li.append(input);
        list.append(li);
      }
      const note = el("input");
      note.type = "text";
      note.maxLength = 300;
      note.placeholder = "Optional note for the writer, for example: keep it practical";
      note.setAttribute("aria-label", "Note for the writer");
      const noteRow = el("div", "plan-note");
      noteRow.append(note);

      const approve = el("button", "primary", "Approve and research");
      approve.type = "button";
      approve.dataset.testid = "approve-button";
      const reject = el("button", "secondary", "Reject");
      reject.type = "button";
      reject.dataset.testid = "reject-button";
      const actions = el("div", "plan-actions");
      actions.append(approve, reject);

      const decide = async (approved) => {
        approve.disabled = true;
        reject.disabled = true;
        $("run-error").textContent = "";
        state.pollError = false;
        const subquestions = [...list.querySelectorAll("textarea")].map((input) => input.value.replace(/\s+/g, " ").trim()).filter(Boolean);
        try {
          await api(`/api/runs/${encodeURIComponent(run.id)}/decision`, { approved, note: note.value, subquestions });
          state.decisionSent[run.id] = { approved, note: note.value.trim(), subquestions };
          if (state.runId === run.id) renderRun();
        } catch (error) {
          approve.disabled = false;
          reject.disabled = false;
          $("run-error").textContent = error.message;
        }
      };
      approve.addEventListener("click", () => decide(true));
      reject.addEventListener("click", () => decide(false));

      box.replaceChildren(el("p", "plan-summary", run.plan.summary || ""), list, noteRow, actions);
      requestAnimationFrame(fitPlan);
      return;
    }

    const compact = run.research.length > 0;
    if (!changed("plan", [run.plan, decision, compact, phase.key === "awaiting_approval"])) return;
    state.planEditFor = null;
    const questions = decision && decision.subquestions && decision.subquestions.length ? decision.subquestions : run.plan.subquestions;
    if (compact) {
      // The researchers below each show their sub-question; no need to list them twice.
      const line = el("p", "plan-compact");
      line.append(el("strong", null, "Approved."), ` ${questions.length} sub-questions, one researcher each.`);
      if (decision && decision.note) line.append(` Note for the writer: ${decision.note}`);
      box.replaceChildren(line);
      return;
    }
    const list = el("ol", "plan-list");
    for (const question of questions) list.append(el("li", null, question));
    const parts = [el("p", "plan-summary", run.plan.summary || ""), list];
    if (decision) {
      const verdict = el("p", decision.approved ? "decision" : "decision rejected");
      verdict.append(el("strong", null, decision.approved ? "Approved." : "Rejected."));
      if (decision.note) verdict.append(` Note for the writer: ${decision.note}`);
      parts.push(verdict);
    }
    box.replaceChildren(...parts);
  }

  function renderResearch(run) {
    const block = $("research-block");
    if (!run.research.length) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    const grid = $("research");
    run.research.forEach((item, index) => {
      let card = grid.children[index];
      if (!card) {
        card = el("div", "research-item");
        card.dataset.testid = "research-item";
        const head = el("div", "research-head");
        head.append(el("span", "research-who", `Researcher ${index + 1}`), el("span", "chip"));
        card.append(head, el("div", "research-q"), el("div", "research-body"));
        grid.append(card);
      }
      if (!changed(`research-${index}`, item)) return;
      const status = item.status === "done" ? "done" : item.status === "failed" ? "failed" : "running";
      card.dataset.status = status;
      const chip = card.querySelector(".chip");
      chip.className = `chip ${status}`;
      chip.textContent = status === "done" ? "Done" : status === "failed" ? "Failed" : "Searching";
      card.querySelector(".research-q").textContent = item.subquestion || "";
      const body = card.querySelector(".research-body");
      body.className = "research-body";
      delete body.dataset.more;
      if (status === "done") {
        markdown(item.findings, body);
        requestAnimationFrame(() => markOverflow(body));
      } else if (status === "failed") {
        body.replaceChildren(el("span", null, item.error || "This researcher failed."));
      } else {
        body.classList.add("searching");
        const dots = el("span", "dots");
        dots.append(el("i"), el("i"), el("i"));
        body.replaceChildren(dots, el("span", null, "Searching the web with Foundry"));
      }
    });
    while (grid.children.length > run.research.length) grid.lastChild.remove();
    reveal(block);
  }

  /* A findings box taller than its card scrolls; fade its last lines while there is more below. */
  function markOverflow(body) {
    const more = body.scrollHeight - body.scrollTop - body.clientHeight > 6;
    body.dataset.more = more ? "1" : "0";
    if (!body.dataset.watched) {
      body.dataset.watched = "1";
      body.addEventListener("scroll", () => markOverflow(body), { passive: true });
    }
  }

  function renderReport(run) {
    const block = $("report-block");
    if (!run.report) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    if (changed("report", run.report)) markdown(run.report, $("report"));
    reveal(block);
  }

  function renderFollowUp(run) {
    // Shown only once the follow-up exists. While the timer runs, the countdown is in the
    // status line and beside the sandbox count, where it can be seen without scrolling.
    const block = $("follow-up-block");
    if (!run.follow_up) {
      block.hidden = true;
      return;
    }
    block.hidden = false;
    if (changed("follow-up", run.follow_up)) markdown(run.follow_up, $("follow-up"));
    reveal(block);
  }

  function renderRun() {
    const run = state.run;
    if (!run) return;
    const phase = phaseOf(run);
    const chip = $("phase");
    chip.textContent = phase.label;
    chip.dataset.tone = phase.tone;
    chip.dataset.value = PHASE[run.phase] ? run.phase : "starting"; // the workflow's own word
    chip.dataset.status = run.status;
    $("run-id").textContent = run.id;
    $("question").textContent = run.question || "";
    $("bar-question").textContent = run.question || "";
    renderSteps(run, phase);
    statusLine(run, phase);
    renderPlan(run, phase);
    renderResearch(run);
    renderReport(run);
    renderFollowUp(run);
    // Sections scrolled to the top stop just under the pinned bar, whatever its height is now.
    document.documentElement.style.setProperty("--bar", `${$("run-bar").offsetHeight}px`);
    settleScroll(run);
    tick();
  }

  // ------------------------------------------------------------------ what is running

  function wakingNow() {
    const run = state.run;
    const pending = run && run.status === "RUNNING" ? run.pending_wake : null;
    return pending && pending.seconds >= 0.8 ? pending : null;
  }

  // Sandboxes with a step of the open run running on them right now, from Temporal's own
  // record. A worker reports what it is doing every 15 s, which is longer than most steps.
  function busyOnRun() {
    const run = state.run && state.runId ? state.run : null;
    const busy = new Set();
    if (run && run.status === "RUNNING") {
      for (const item of run.timeline) {
        if ((item.type === "agent" || item.type === "activity") && item.status === "running" && item.sandbox) busy.add(item.sandbox);
      }
    }
    return busy;
  }

  function stateOf(box, busy) {
    return (box.state === "idle" || box.state === "starting") && busy.has(box.short) ? "working" : box.state;
  }

  function renderCount() {
    const boxes = state.boxes;
    const waking = wakingNow();
    const busy = busyOnRun();
    const states = boxes.sandboxes.filter((box) => UP.has(box.state)).map((box) => stateOf(box, busy));
    const run = state.run && state.runId ? state.run : null;
    const waitingOn = run && run.status === "RUNNING" ? run.phase : run ? run.status : null;
    if (!changed("count", [boxes.up, states, waking ? waking.since : null, waitingOn, run && run.follow_up_due])) return;
    $("sandbox-count").textContent = String(boxes.up);
    $("sandbox-count").dataset.value = String(boxes.up);
    const sub = $("count-sub");
    sub.replaceChildren();
    const card = $("count-card");
    card.dataset.up = boxes.up > 0 ? "1" : "0";
    card.dataset.waking = waking ? "1" : "0";
    const line = $("count-line");
    if (waking && boxes.up === 0) {
      const counter = el("span");
      counter.dataset.since = waking.since;
      counter.dataset.precise = "1";
      const text = el("span", "waking");
      text.append("Temporal is waking Foundry… ", counter);
      line.replaceChildren(text);
      sub.textContent = `A task is waiting ${wakeFor(waking.cause, state.run) || "for a worker"}, and no sandbox is up.`;
    } else if (boxes.up === 0) {
      line.replaceChildren(el("strong", null, "Nothing is running."), " No sandbox compute is billed.");
      if (waitingOn === "awaiting_approval") {
        sub.textContent = "Temporal is holding the run until a person approves the plan.";
      } else if (waitingOn === "waiting_for_follow_up" && run.follow_up_due) {
        const countdown = el("strong");
        countdown.dataset.until = run.follow_up_due;
        sub.append("Temporal will wake Foundry when the timer fires, in ", countdown, ".");
      } else if (waitingOn === "COMPLETED") {
        sub.textContent = "The run is finished and every sandbox has been released.";
      }
    } else {
      const working = states.filter((item) => item === "working").length;
      const starting = states.filter((item) => item === "starting").length;
      const detail = working ? `${working} working` : starting ? "starting up" : states.includes("releasing") ? "being released" : "idle, about to be released";
      line.replaceChildren(el("strong", null, `${boxes.up} sandbox${boxes.up === 1 ? "" : "es"} up`), `, ${detail}.`);
    }
  }

  function sandboxRows() {
    const run = state.run && state.runId ? state.run : null;
    const registry = new Map(state.boxes.sandboxes.map((box) => [box.short, box]));
    const rows = [];
    const seen = new Set();
    if (run) {
      for (const used of run.sandboxes) {
        rows.push({ key: used.id, used, box: registry.get(used.id) || null, at: used.first_task_at });
        seen.add(used.id);
      }
    }
    const others = state.boxes.sandboxes.filter((box) => !seen.has(box.short));
    const extra = run ? others.filter((box) => UP.has(box.state)) : others.slice(0, 4);
    for (const box of extra) rows.push({ key: box.short, used: null, box, at: box.first_seen });
    rows.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
    return rows;
  }

  function renderSandboxes() {
    const run = state.run && state.runId ? state.run : null;
    $("sandboxes-title").textContent = run ? "Sandboxes in this run" : "Recent sandboxes";
    const rows = sandboxRows();
    const list = $("sandboxes");
    const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    const keys = rows.map((row) => row.key);
    if (changed("sandbox-keys", keys)) {
      const existing = new Map([...list.children].map((node) => [node.dataset.key, node]));
      list.replaceChildren(...rows.map((row) => existing.get(row.key) || newSandboxCard(row.key)));
    }
    rows.forEach((row, index) => updateSandboxCard(list.children[index], row, run));
    if (stick) list.scrollTop = list.scrollHeight;
  }

  function agentList(agents) {
    // "researcher 1, researcher 2, researcher 3, writer" reads better as "3 researchers, writer".
    const researchers = agents.filter((name) => name.startsWith("researcher"));
    if (researchers.length < 2) return agents.join(", ");
    const parts = [];
    for (const name of agents) {
      if (!name.startsWith("researcher")) parts.push(name);
      else if (name === researchers[0]) parts.push(`${researchers.length} researchers`);
    }
    return parts.join(", ");
  }

  function newSandboxCard(key) {
    const card = el("div", "sandbox");
    card.dataset.testid = "sandbox-card";
    card.dataset.key = key;
    state.sig[`sandbox-${key}`] = null; // a new card has nothing drawn on it yet
    card.append(el("span", "sandbox-id", shortId(key)), el("div", "sandbox-what"), el("span", "chip"), el("div", "sandbox-actions"));
    return card;
  }

  function updateSandboxCard(card, row, run) {
    const box = row.box;
    const stateName = box ? stateOf(box, busyOnRun()) : "unknown";
    const up = UP.has(stateName);
    const wake = run ? run.wakes.find((item) => item.sandbox === row.key) : null;
    const crashes = box ? box.crashes : [];
    const crashed = crashes.filter((crash) => crash.ok).length;
    const lastCrash = crashes[crashes.length - 1];
    // A presenter request shows as "requested" until the console has recorded what came of it.
    const asked = state.pending[row.key];
    let note = "";
    if (asked) {
      const recorded = asked.kind === "crash-worker" ? crashes.length > asked.crashes : !up || stateName === "releasing" || Boolean(box && box.stop);
      note = asked.failed || !recorded ? asked.text : "";
    }
    const signature = [stateName, box && box.stop_reason, box && box.lived_seconds && !up ? box.lived_seconds : null,
      row.used && row.used.agents, row.used && row.used.workflow_tasks, wake, note, crashes, box && box.stop];
    if (!changed(`sandbox-${row.key}`, signature)) return;
    card.dataset.up = up ? "1" : "0";
    const chip = card.querySelector(".chip");
    chip.className = `chip ${stateName}`;
    chip.textContent = stateName === "released" && box.stop_reason === "presenter" ? "Stopped" : SANDBOX_STATE[stateName];

    const what = card.querySelector(".sandbox-what");
    const parts = [];
    if (box && up) {
      const live = el("span");
      live.dataset.since = box.first_seen;
      parts.push(["up ", live]);
    } else if (box) {
      parts.push([`lived ${seconds(box.lived_seconds)}`]);
    }
    if (wake && wake.warm) parts.push(["was already up"]);
    else if (wake && wake.latency_seconds !== null) parts.push([`woken in ${seconds(wake.latency_seconds)} ${wakeFor(wake.cause, run)}`.trim()]);
    else if (wake && wake.cause === "retry") parts.push(["picked up a retry"]);
    if (row.used && row.used.agents.length) parts.push([`ran ${agentList(row.used.agents)}`]);
    if (run && !row.used && up && stateName !== "starting") parts.push(["no task from this run"]);
    if (crashed) parts.push([`worker crashed ${crashed === 1 ? "once" : `${crashed} times`} on request`]);
    // A request Foundry refused must not read as if it had worked.
    const why = (result) => (result.status_code ? ` (HTTP ${result.status_code})` : result.error ? `: ${String(result.error).slice(0, 70)}` : "");
    if (lastCrash && !lastCrash.ok) parts.push([el("span", "problem", `crash request failed${why(lastCrash)}`)]);
    if (box && box.stop && box.stop.ok === false) parts.push([el("span", "problem", `stop failed${why(box.stop)}`)]);
    if (note) parts.push([asked.failed ? el("span", "problem", note) : note]);
    what.replaceChildren();
    parts.forEach((part, index) => {
      if (index) what.append(" · ");
      what.append(...part);
    });
    what.title = what.textContent;

    const actions = card.querySelector(".sandbox-actions");
    const stopFailed = Boolean(box && box.stop && box.stop.ok === false);
    if (up && box && (stateName !== "releasing" || stopFailed)) {
      if (!actions.children.length) {
        const crash = el("button", "ghost", "Crash the worker");
        crash.type = "button";
        crash.dataset.testid = "crash-worker-button";
        crash.addEventListener("click", () => sandboxAction(box.session, row.key, "crash-worker", "crash requested"));
        const stop = el("button", "ghost", "Stop the sandbox");
        stop.type = "button";
        stop.dataset.testid = "stop-sandbox-button";
        stop.addEventListener("click", () => sandboxAction(box.session, row.key, "stop", "stop requested"));
        actions.append(crash);
        if (state.config && state.config.show_stop_button) actions.append(stop);
      }
    } else {
      actions.replaceChildren();
    }
  }

  async function sandboxAction(session, key, action, note) {
    const box = state.boxes.sandboxes.find((item) => item.session === session);
    state.pending[key] = { kind: action, text: note, crashes: box ? box.crashes.length : 0 };
    renderSandboxes();
    try {
      await api(`/api/sandboxes/${encodeURIComponent(session)}/${action}`, {});
    } catch (error) {
      state.pending[key] = { kind: action, text: error.message, failed: true };
      state.sig[`sandbox-${key}`] = null;
      renderSandboxes();
    }
    setTimeout(() => {
      delete state.pending[key];
      state.sig[`sandbox-${key}`] = null;
    }, 6000);
  }

  function timelineRow(kind, at, ...content) {
    const row = el("li", `tl ${kind}`);
    const text = el("div", "text");
    text.append(...content);
    row.append(el("time", null, clockTime(at)), el("span", "dot"), text);
    return row;
  }

  function timelineRows(run) {
    const rows = [];
    const sub = (text) => el("span", "sub", text);
    const hasTimer = run.timeline.some((item) => item.type === "timer_started");
    for (const item of run.timeline) {
      if (item.type === "started") {
        rows.push(timelineRow("started", item.at, "Run started"));
      } else if (item.type === "wake") {
        let badge;
        if (item.warm) badge = el("span", "wake-badge plain", "Picked up by a running sandbox");
        else if (item.latency_seconds === null) badge = el("span", "wake-badge plain", "A new sandbox picked up the retry");
        else badge = el("span", "wake-badge", `Temporal woke Foundry in ${seconds(item.latency_seconds)}`);
        badge.dataset.testid = "wake-badge";
        rows.push(timelineRow("wake", item.at, badge, sub(`sandbox ${shortId(item.sandbox)} ${wakeFor(item.cause, run)}`.trim())));
      } else if (item.type === "agent" || item.type === "activity") {
        const name = el("span", "name", item.agent);
        const parts = [name];
        if (item.status === "scheduled") {
          parts.push(sub("waiting for a sandbox"));
        } else if (item.status === "running") {
          const live = el("span", "sub");
          live.dataset.since = item.started_at;
          parts.push(live, sub(`on ${shortId(item.sandbox)}`));
        } else if (item.status === "completed") {
          parts.push(sub(`${item.duration_seconds === null ? "done" : seconds(item.duration_seconds)} on ${shortId(item.sandbox)}`));
        } else {
          parts.push(sub(item.status.replace("_", " ")));
        }
        if (item.attempt > 1) parts.push(el("span", "attempt", `attempt ${item.attempt}`));
        const problem = item.status !== "completed" && item.status !== "running" && item.status !== "scheduled" ? item.message : null;
        if (problem) parts.push(el("span", "sub msg", problem.slice(0, 120)));
        const row = timelineRow("agent", item.at, ...parts);
        row.dataset.status = item.status;
        rows.push(row);
      } else if (item.type === "phase") {
        if (item.phase === "awaiting_approval") rows.push(timelineRow("phase", item.at, "Waiting for a person. Nothing needs to run."));
        if (item.phase === "waiting_for_follow_up" && !hasTimer) rows.push(timelineRow("phase", item.at, "Waiting on a durable timer."));
      } else if (item.type === "signal") {
        const text = item.name !== "decide" ? `Signal ${item.name}` : item.approved === false ? "Rejected. Temporal recorded the Signal." : "Approved. Temporal recorded the Signal.";
        rows.push(timelineRow("signal", item.at, text));
      } else if (item.type === "timer_started") {
        rows.push(timelineRow("timer", item.at, `Durable timer set for ${Math.round(item.seconds)} s. Nothing needs to run.`, sub(`fires ${clockTime(item.fires_at)}`)));
      } else if (item.type === "timer_fired") {
        rows.push(timelineRow("timer", item.at, "Timer fired"));
      } else if (item.type === "closed") {
        const bad = item.status !== "completed";
        rows.push(timelineRow(bad ? "closed bad" : "closed", item.at, bad ? `Run ${item.status.replace("_", " ")}${item.message ? `: ${item.message.slice(0, 80)}` : ""}` : "Run completed"));
      }
    }
    return rows;
  }

  function renderTimeline() {
    const run = state.run && state.runId ? state.run : null;
    const list = $("timeline");
    const waking = wakingNow();
    const key = run ? [run.id, run.timeline.map((item) => [item.type, item.at, item.status, item.attempt, item.sandbox, item.duration_seconds]), waking ? waking.since : null] : null;
    if (!changed("timeline", key)) return;
    const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
    if (!run) {
      list.replaceChildren();
      return;
    }
    const rows = timelineRows(run);
    if (waking) {
      const counter = el("span");
      counter.dataset.since = waking.since;
      counter.dataset.precise = "1";
      const row = timelineRow("waking", new Date(now()).toISOString(), "Waking Foundry… ", counter);
      rows.push(row);
    }
    list.replaceChildren(...rows);
    if (stick) list.scrollTop = list.scrollHeight;
    list.dataset.scrolled = list.scrollTop > 4 ? "1" : "0";
  }

  function renderSide() {
    renderCount();
    renderSandboxes();
    renderTimeline();
    const run = state.run && state.runId ? state.run : null;
    const link = $("temporal-ui-link");
    const target = run ? run.temporal_ui_url : state.config && state.config.temporal_ui_url;
    const url = target && /^(\/|https?:\/\/)/.test(target) ? target : null;
    link.hidden = !url;
    if (url) link.href = url;
    link.textContent = run ? "Open this run in the Temporal UI" : "Open the Temporal UI";
    tick();
  }

  // ------------------------------------------------------------------ navigation and polling

  function resetRunView() {
    state.run = null;
    state.shown = new Set();
    state.firstRender = true;
    state.revealNext = null;
    state.planEditFor = null;
    for (const key of Object.keys(state.sig)) if (key !== "recent" && key !== "count") delete state.sig[key];
    for (const id of ["plan-block", "research-block", "report-block", "follow-up-block"]) $(id).hidden = true;
    $("research").replaceChildren();
    $("plan").replaceChildren();
    $("report").replaceChildren();
    $("follow-up").replaceChildren();
    $("run-error").textContent = "";
    state.pollError = false;
    reconnecting("run", false); // whichever run was being read, it is not being read now
    $("status-line").replaceChildren();
    $("steps").replaceChildren();
    $("phase").textContent = "";
    $("question").textContent = "";
    $("bar-question").textContent = "";
    $("run-id").textContent = "";
    $("work").scrollTop = 0;
  }

  function openRun(id) {
    resetRunView();
    state.runId = id;
    if (location.hash !== `#/run/${id}`) history.replaceState(null, "", `#/run/${id}`);
    reconnecting("/api/runs", false); // the list of recent runs is not read in this view
    show("run");
    renderSide();
    loadRun();
  }

  function openReady() {
    state.runId = null;
    resetRunView();
    if (location.hash) history.replaceState(null, "", location.pathname);
    show("ready");
    renderSide();
    loadRecent();
    $("question-input").focus();
  }

  async function loadRun() {
    const id = state.runId;
    if (!id) return;
    try {
      const run = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (state.runId !== id) return;
      syncClock(run.server_time);
      state.run = run;
      if (state.pollError) {
        // The message was about a read that failed; this one worked.
        state.pollError = false;
        $("run-error").textContent = "";
      }
      renderRun();
      renderSide();
    } catch (error) {
      if (error.status === 401) return showLogin();
      if (state.runId !== id) return;
      if (error.status === 404) {
        openReady();
        $("start-error").textContent = "That run was not found.";
      } else if (error.status !== 0) {
        state.pollError = true;
        $("run-error").textContent = error.message;
      }
    }
  }

  async function loadSandboxes() {
    try {
      const boxes = await api("/api/sandboxes");
      syncClock(boxes.server_time);
      state.boxes = boxes;
      renderSide();
    } catch (error) {
      if (error.status === 401) showLogin();
    }
  }

  async function loadRecent() {
    try {
      renderRecent((await api("/api/runs")).runs);
    } catch (error) {
      if (error.status === 401) showLogin();
    }
  }

  async function pollLoop() {
    let beat = 0;
    for (;;) {
      if (!$("app-view").hidden) {
        const closed = state.run && state.run.status !== "RUNNING";
        const work = [loadSandboxes()];
        if (state.runId && (!closed || beat % 5 === 0)) work.push(loadRun());
        if (!state.runId && beat % 5 === 0) work.push(loadRecent());
        await Promise.all(work);
      }
      beat += 1;
      await sleep(1000);
    }
  }

  function showLogin() {
    show("login");
    $("access-code").focus();
  }

  async function enter() {
    try {
      state.config = await api("/api/config");
    } catch (error) {
      if (error.status === 401) return showLogin();
      $("login-error").textContent = error.message;
      return showLogin();
    }
    renderConfig();
    const match = /^#\/run\/([A-Za-z0-9-]+)$/.exec(location.hash);
    if (match) openRun(match[1]);
    else openReady();
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-error").textContent = "";
    $("login-button").disabled = true;
    try {
      await api("/api/login", { code: $("access-code").value });
      $("access-code").value = "";
      await enter();
    } catch (error) {
      $("login-error").textContent = error.message;
    }
    $("login-button").disabled = false;
  });

  $("start-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("start-error").textContent = "";
    const question = $("question-input").value.trim();
    if (!question) {
      $("start-error").textContent = "Type a question or pick one below.";
      return;
    }
    $("start-button").disabled = true;
    try {
      const started = await api("/api/runs", { question, follow_up_after_seconds: state.followUp });
      $("question-input").value = "";
      openRun(started.id);
    } catch (error) {
      if (error.status === 401) showLogin();
      else $("start-error").textContent = error.message;
    }
    $("start-button").disabled = false;
  });

  $("question-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("start-form").requestSubmit();
    }
  });
  $("new-run-button").addEventListener("click", openReady);
  window.addEventListener("resize", fitPlan);
  $("timeline").addEventListener("scroll", (event) => {
    event.target.dataset.scrolled = event.target.scrollTop > 4 ? "1" : "0";
  }, { passive: true });
  window.addEventListener("hashchange", () => {
    const match = /^#\/run\/([A-Za-z0-9-]+)$/.exec(location.hash);
    if (match && match[1] !== state.runId) openRun(match[1]);
    else if (!match && state.runId) openReady();
  });

  // Show the small copy of the question in the run bar once the big one has scrolled away.
  new IntersectionObserver(
    (entries) => { $("run-bar").dataset.stuck = entries[0].isIntersecting ? "0" : "1"; },
    { root: $("work"), threshold: 0.2 },
  ).observe($("question"));

  setInterval(tick, 200);
  enter().then(pollLoop);
})();
