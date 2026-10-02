/* Zone Console — read-only view of four cameras.
 *
 * Rules this file follows on purpose:
 *   - Every value shown is read from the camera service or counted from what it
 *     returned. An empty list is never presented as "all clear".
 *   - A camera is only called healthy when fresh stats AND a recent image agree.
 *     A failed or stale read clears the sample, so failure reads as "no data",
 *     never as healthy.
 *   - Previews and stats are judged separately: the preview endpoint can serve a
 *     cached last frame for a camera that is down, so an image is only shown
 *     while the stats say the camera is actually streaming.
 *   - All text goes in through textContent. No innerHTML, no insertAdjacentHTML,
 *     no document.write.
 *   - Snapshot paths are only ever built from ids that match the camera
 *     service's own tracker id shape, which is also what nginx accepts.
 *   - Timers never overlap: one loop per source, each scheduling its next run
 *     after the current one settles, both stopped while the tab is hidden.
 */
(() => {
  "use strict";

  const CAMERAS = Object.freeze(["camera_1", "camera_2", "camera_3", "camera_4"]);

  const REQUIRED_ZONE = Object.freeze({
    camera_1: "camera_1_area",
    camera_2: "camera_2_area",
    camera_3: "camera_3_area",
    camera_4: "camera_4_area",
  });

  const IMAGE_MS = 2000; // previews: one picture every two seconds
  const POLL_MS = 5000; // status + events
  const TIMEOUT_MS = 4000; // per request, below the proxy's own timeout
  const PREVIEW_STALE_MS = 8000; // no picture for this long is not a live view
  const STATS_STALE_MS = 20000; // an older stats sample is not current state
  const EVENT_LIMIT = 50; // rows shown per camera
  // "<frame_time>-<rand_id>": timestamp with optional decimal, hyphen, 6 chars.
  const EVENT_ID = /^[0-9]+(?:\.[0-9]+)?-[a-z0-9]{6}$/;

  const $ = (id) => document.getElementById(id);

  const el = {
    barStateText: $("backend-state-text"),
    barTick: $("backend-state").querySelector(".tick"),
    notice: $("notice"),
    noticeText: $("notice-text"),
    retry: $("retry"),
    retryLabel: $("retry-label"),
    filters: $("filters"),
    tally: $("events-tally"),
    events: $("events"),
    eventsEmpty: $("events-empty"),
    statOnline: $("stat-online"),
    statInference: $("stat-inference"),
    statAge: $("stat-age"),
    eventTemplate: $("event-item"),
  };

  const state = {
    filter: "all",
    sample: null, // newest usable stats entry, or null
    statsRead: false,
    statsStale: false,
    statsAge: null, // seconds since service.last_updated, when known
    eventsRead: false,
    statsError: null,
    eventsError: null,
    lastEvents: null, // { rows } for all four cameras; selection is a render filter
  };

  const cards = new Map();

  for (const camera of CAMERAS) {
    const root = document.querySelector('.feed[data-camera="' + camera + '"]');
    if (!root) continue;
    cards.set(camera, {
      camera,
      root,
      img: root.querySelector(".feed__img"),
      placeholder: root.querySelector(".feed__placeholder"),
      statusText: root.querySelector(".feed__status-text"),
      statusTick: root.querySelector(".feed__status .tick"),
      stamp: root.querySelector(".feed__stamp"),
      lastFrameAt: 0,
      failures: 0,
      inFlight: false,
      token: 0,
    });
  }

  /* ------------------------------------------------------------ helpers -- */

  const numberOrNull = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;

  const isObject = (value) =>
    Boolean(value) && typeof value === "object" && !Array.isArray(value);

  const clockTime = (ms) => {
    const d = new Date(ms);
    const pad = (n) => (n < 10 ? "0" + n : String(n));
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  };

  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  // "camera_2" -> "Camera 2". Internal names never reach the page.
  const cameraLabel = (camera) => "Camera " + String(camera).slice(-1);

  const cameraSet = (cameras) => new Set(cameras);

  async function fetchJSON(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function describe(err) {
    if (err && err.name === "AbortError") return "request timed out";
    if (err && err.name === "TypeError") return "no response from the camera service";
    if (err && err.message) return err.message;
    return "read failed";
  }

  function setTick(node, tone) {
    if (node) node.dataset.tone = tone;
  }

  // One [hidden] rule in style.css beats any author display declaration, so the
  // attribute alone is enough and no inline style is written.
  function setShown(node, shown) {
    node.hidden = !shown;
  }

  function setBarState(tone, text) {
    setTick(el.barTick, tone);
    el.barStateText.textContent = text;
  }

  /* ------------------------------------------------------------- status -- */

  /* Healthy means: stats say it is capturing and processing frames with
   * detection on, and a picture actually arrived recently. Anything else is a
   * distinct tone with its own reason, so "no picture" never reads as "fine". */
  function cameraStatus(camera, sample, card) {
    const stats = isObject(sample) && isObject(sample.cameras) ? sample.cameras[camera] : null;
    const received = Boolean(card && card.lastFrameAt);
    const age = received ? Date.now() - card.lastFrameAt : 0;
    const fresh = received && age <= PREVIEW_STALE_MS;

    if (!isObject(stats)) {
      return { tone: "pending", label: "no data", known: false, streaming: false, fresh: fresh };
    }

    const capture = numberOrNull(stats.camera_fps);
    const process = numberOrNull(stats.process_fps);
    // Frigate reports detection_enabled as a JSON boolean. The numeric 1 is
    // accepted because Python's True == 1 and the upstream shape is not
    // something this page should assume; a string is never a yes.
    const detecting = stats.detection_enabled === true || stats.detection_enabled === 1;
    const streaming = capture !== null && capture > 0 && process !== null && process > 0;

    if (!streaming) {
      return { tone: "bad", label: "no signal", known: true, streaming: false, fresh: fresh };
    }
    if (!detecting) {
      return { tone: "warn", label: "not watching for people", known: true, streaming: true, fresh: fresh };
    }
    if (!received) {
      return { tone: "pending", label: "starting", known: true, streaming: true, fresh: false };
    }
    if (!fresh) {
      return {
        tone: "warn",
        label: "no picture for " + Math.round(age / 1000) + " s",
        known: true,
        streaming: true,
        fresh: false,
      };
    }
    const rate = capture >= 10 ? capture.toFixed(0) : capture.toFixed(1);
    return { tone: "ok", label: "watching · " + rate + " fps", known: true, streaming: true, fresh: true };
  }

  function renderCardStatus() {
    const statsKnown = Boolean(state.sample);
    for (const card of cards.values()) {
      const status = cameraStatus(card.camera, state.sample, card);
      setTick(card.statusTick, status.tone);
      card.statusText.textContent = status.label;

      // A cached last frame must not sit under a camera that is not streaming,
      // and a failed or stale stats read hides the image rather than implying
      // the picture is current.
      const show = status.streaming && status.fresh;
      setShown(card.img, show);
      setShown(card.placeholder, !show);

      if (!show) {
        card.placeholder.textContent = !statsKnown
          ? "camera status unavailable"
          : !status.known
            ? "no data for this camera"
            : !status.streaming
              ? "camera is not streaming"
              : !card.lastFrameAt
                ? "waiting for the first picture"
                : "no new picture for " + Math.round((Date.now() - card.lastFrameAt) / 1000) + " s";
      }

      if (!statsKnown) {
        card.stamp.textContent = state.statsStale ? "status out of date" : "—";
      } else if (!status.streaming) {
        card.stamp.textContent = "—";
      } else if (!card.lastFrameAt) {
        card.stamp.textContent = card.failures > 0 ? "picture unavailable, retrying" : "no picture yet";
      } else if (status.fresh) {
        card.stamp.textContent = "received " + clockTime(card.lastFrameAt);
      } else {
        card.stamp.textContent =
          "last picture " + clockTime(card.lastFrameAt) +
          " · " + Math.round((Date.now() - card.lastFrameAt) / 1000) + " s ago";
      }
    }
  }

  function renderStrip() {
    if (!state.sample) {
      el.statOnline.textContent = "—";
      el.statInference.textContent = "—";
      el.statAge.textContent = state.statsStale ? "out of date" : "—";
      return;
    }

    let watching = 0;
    for (const camera of CAMERAS) {
      if (cameraStatus(camera, state.sample, cards.get(camera)).tone === "ok") watching += 1;
    }
    el.statOnline.textContent = watching + " of " + CAMERAS.length;

    /* With nothing streaming, inference_speed is Frigate's 10 ms default, not a
     * measurement. Showing it would read as a real detector figure. */
    const detectors = watching > 0 && isObject(state.sample.detectors) ? state.sample.detectors : null;
    const parts = [];
    if (detectors) {
      for (const name of Object.keys(detectors)) {
        const ms = numberOrNull(detectors[name].inference_speed);
        if (ms !== null) parts.push(Math.round(ms) + " ms");
      }
    }
    el.statInference.textContent = parts.length ? parts.join(" · ") : "—";

    const seconds = state.statsAge;
    el.statAge.textContent =
      seconds === null
        ? "—"
        : seconds * 1000 > STATS_STALE_MS
          ? "out of date"
          : seconds + " s";
  }

  /* ------------------------------------------------------------ previews -- */

  /* One request in flight per card, bounded by TIMEOUT_MS. A slow or hung
   * response is dropped rather than left to resolve into a later tick, and a
   * result is only applied if it belongs to the newest request for that card. */
  function refreshPreview(camera) {
    const card = cards.get(camera);
    if (!card || card.inFlight) return;

    card.inFlight = true;
    card.token += 1;
    const token = card.token;

    const probe = new Image();
    probe.decoding = "async";
    let settled = false;
    let timer = null;

    const finish = (ok) => {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimeout(timer);
      probe.onload = null;
      probe.onerror = null;
      if (token !== card.token) {
        card.inFlight = false;
        return;
      }
      card.inFlight = false;
      if (ok) {
        card.img.src = probe.src;
        card.lastFrameAt = Date.now();
        card.failures = 0;
      } else {
        card.failures += 1;
      }
      renderCardStatus();
    };

    timer = setTimeout(() => {
      probe.removeAttribute("src"); // release the hung request
      finish(false);
    }, TIMEOUT_MS);

    probe.onload = () => finish(true);
    probe.onerror = () => finish(false);
    probe.src = "/api/" + camera + "/latest.jpg?t=" + Date.now();
  }

  function previewTick() {
    imageTimer = null;
    if (!running) return;
    for (const camera of CAMERAS) refreshPreview(camera);
    renderCardStatus();
    imageTimer = setTimeout(previewTick, IMAGE_MS);
  }

  /* -------------------------------------------------------------- events -- */

  const selectedCameras = () =>
    state.filter === "all" ? CAMERAS.slice() : [state.filter];

  /* A row must be a person, must carry a snapshot, must come from a selected
   * camera, and must have entered that camera's required zone. wantedCameras is
   * a Set of camera names, never a Set of zone names. */
  function zoneEvent(event, wantedCameras) {
    if (!isObject(event)) return false;
    if (event.label !== "person") return false;
    if (event.has_snapshot !== true) return false;
    if (typeof event.id !== "string" || !EVENT_ID.test(event.id)) return false;
    if (typeof event.camera !== "string") return false;
    if (!wantedCameras.has(event.camera)) return false;
    if (!Array.isArray(event.zones)) return false;
    return event.zones.includes(REQUIRED_ZONE[event.camera]);
  }

  const zoneLabel = (event) => {
    const list = Array.isArray(event.zones) ? event.zones : [];
    const named = list.filter((zone) => zone !== REQUIRED_ZONE[event.camera]);
    if (named.length > 0) return named.join(" + ");
    return "Monitored area";
  };

  // Selection is applied here, at render time, so changing the filter never
  // shows rows that belong to a camera that is not selected — including while a
  // read is still in flight.
  function visibleRows() {
    const data = state.lastEvents;
    const rows = data && Array.isArray(data.rows) ? data.rows : [];
    return selectedCameras()
      .reduce((acc, camera) => acc.concat(rows.filter((row) => row.camera === camera)), [])
      .sort((a, b) => (b.start_time || 0) - (a.start_time || 0))
      .slice(0, EVENT_LIMIT);
  }

  let renderedSignature = null;

  function renderEvents() {
    const rows = visibleRows();

    if (!state.eventsRead) {
      setShown(el.events, false);
      setShown(el.eventsEmpty, false);
      while (el.events.firstChild) el.events.removeChild(el.events.firstChild);
      renderedSignature = null;
      el.tally.textContent =
        state.eventsError === null
          ? "Waiting for detection data."
          : "Recent detections unavailable: " + state.eventsError.message;
      return;
    }

    const scope = state.filter === "all" ? "all cameras" : cameraLabel(state.filter);
    el.tally.textContent = plural(rows.length, "detection") + " · " + scope;

    // Rebuild only when the row set actually changes, so a focused row is not
    // destroyed by an unrelated poll.
    const signature = state.filter + "#" + rows.map((row) => row.id).join("|");
    if (signature === renderedSignature) {
      setShown(el.events, rows.length !== 0);
      setShown(el.eventsEmpty, rows.length === 0);
      return;
    }
    renderedSignature = signature;

    while (el.events.firstChild) el.events.removeChild(el.events.firstChild);

    for (const event of rows) {
      const node = el.eventTemplate.content.firstElementChild.cloneNode(true);
      const label = cameraLabel(event.camera);
      const at = clockTime(
        numberOrNull(event.start_time) !== null ? event.start_time * 1000 : Date.now()
      );
      const href = "/api/events/" + event.id + "/snapshot.jpg";

      const link = node.querySelector(".event__shot");
      const img = node.querySelector(".event__img");
      link.href = href;
      link.setAttribute("aria-label", "Open snapshot from " + label + " at " + at);
      img.alt = "Person detected by " + label;
      img.src = href;

      node.querySelector(".event__camera").textContent = label;
      node.querySelector(".event__zone").textContent = zoneLabel(event);
      node.querySelector(".event__time").textContent = at;

      el.events.appendChild(node);
    }

    const empty = rows.length === 0;
    setShown(el.eventsEmpty, empty);
    setShown(el.events, !empty);
  }

  /* --------------------------------------------------------------- reads -- */

  /* A stats sample is only usable if it has the shape the page reads and a
   * timestamp we can age. Anything else is a failed read, not a healthy one. */
  function usableSample(payload) {
    if (!Array.isArray(payload) || payload.length === 0) return null;
    const sample = payload[payload.length - 1];
    if (!isObject(sample) || !isObject(sample.cameras)) return null;
    if (!isObject(sample.service)) return null;
    const updated = numberOrNull(sample.service.last_updated);
    if (updated === null) return null;
    const asMs = updated > 1e11 ? updated : updated * 1000; // seconds or millis
    return { sample: sample, age: Math.max(0, (Date.now() - asMs) / 1000) };
  }

  async function readStats() {
    try {
      const payload = await fetchJSON("/api/stats");
      const parsed = usableSample(payload);
      if (!parsed) throw new Error("unexpected response shape");
      state.statsStale = parsed.age * 1000 > STATS_STALE_MS;
      // A stale sample is dropped, not displayed: old numbers must not be
      // rendered as current, and must never read as healthy.
      state.sample = state.statsStale ? null : parsed.sample;
      state.statsAge = parsed.age;
      state.statsRead = true;
      state.statsError = null;
    } catch (err) {
      state.sample = null;
      state.statsAge = null;
      state.statsStale = false;
      state.statsRead = false;
      state.statsError = { message: describe(err) };
    }
  }

  async function readEvents() {
    // The proxy pins the upstream query to the whole four-camera person
    // payload, so one read covers every camera and switching the filter is a
    // client-side re-slice rather than a second request that could land out of
    // order with the one before it.
    try {
      const payload = await fetchJSON("/api/events");
      if (!Array.isArray(payload)) throw new Error("unexpected response shape");
      const rows = payload.filter((event) => zoneEvent(event, cameraSet(CAMERAS)));
      state.lastEvents = { rows: rows };
      state.eventsRead = true;
      state.eventsError = null;
    } catch (err) {
      // An unreadable list is an error, not an empty zone.
      state.lastEvents = null;
      state.eventsRead = false;
      state.eventsError = { message: describe(err) };
    }
  }

  /* -------------------------------------------------------------- notice -- */

  function renderNotice() {
    if (!state.statsError && !state.eventsError && !state.statsStale) {
      setShown(el.notice, false);
      el.notice.dataset.tone = "pending";
      el.noticeText.textContent = "";
      setBusy(false);
      return;
    }

    let tone = "bad";
    let text;
    if (state.statsError) {
      text =
        "Camera status unavailable (" + state.statsError.message +
        "). Nothing below is current.";
    } else if (state.statsStale) {
      tone = "warn";
      text =
        "Camera status is out of date (last update " +
        Math.round(state.statsAge) + " s ago). Values below are not current.";
    } else {
      tone = "warn";
      text =
        "Recent detections unavailable (" + state.eventsError.message +
        "). The list below is not a current answer.";
    }

    setShown(el.notice, true);
    el.notice.dataset.tone = tone;
    el.noticeText.textContent = text;
    setBusy(false);
  }

  function renderBarState() {
    if (!state.statsRead) {
      setBarState("bad", state.statsError ? "Camera service unreachable" : "Connecting to cameras");
      return;
    }
    if (state.statsStale) {
      setBarState("bad", "Camera status out of date");
      return;
    }

    const states = CAMERAS.map((camera) => cameraStatus(camera, state.sample, cards.get(camera)));
    const down = states.filter((s) => s.tone === "bad").length;
    const warn = states.filter((s) => s.tone === "warn").length;
    const ok = states.filter((s) => s.tone === "ok").length;

    if (down > 0) {
      setBarState("bad", plural(down, "camera") + " not streaming");
      return;
    }
    if (warn > 0) {
      setBarState("warn", warn + (warn === 1 ? " camera needs" : " cameras need") + " attention");
      return;
    }
    if (ok === CAMERAS.length) {
      setBarState("ok", "All " + CAMERAS.length + " cameras watching");
      return;
    }
    setBarState("pending", "Waiting for camera pictures");
  }

  /* ---------------------------------------------------------------- loop -- */

  let running = false;
  let pollTimer = null;
  let imageTimer = null;
  let polling = false;

  function render() {
    renderBarState();
    renderCardStatus();
    renderStrip();
    renderNotice();
    renderEvents();
  }

  function setBusy(busy) {
    el.retry.disabled = busy;
    el.retry.dataset.busy = busy ? "true" : "false";
    el.retryLabel.textContent = busy ? "Reading…" : "Retry now";
  }

  // Exactly one pending timer per source: scheduling always replaces the
  // previous one, so stop()/start() around an in-flight read cannot leave two
  // loops running.
  function schedulePoll(delay) {
    if (!running) return;
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = setTimeout(() => {
      pollTimer = null;
      poll();
    }, delay);
  }

  async function poll() {
    if (!running || document.hidden) return;
    // A read started before the last stop() may still be settling; let it
    // finish rather than running two reads that would interleave their state.
    if (polling) {
      schedulePoll(POLL_MS);
      return;
    }
    polling = true;
    setBusy(true);
    try {
      await Promise.all([readStats(), readEvents()]);
    } finally {
      polling = false;
      setBusy(false);
    }
    render();
    schedulePoll(POLL_MS);
  }

  function start() {
    if (running) return;
    running = true;
    schedulePoll(0);
    if (imageTimer === null) previewTick();
  }

  function stop() {
    running = false;
    if (pollTimer !== null) clearTimeout(pollTimer);
    if (imageTimer !== null) clearTimeout(imageTimer);
    pollTimer = null;
    imageTimer = null;
  }

  /* --------------------------------------------------------------- wiring -- */

  el.filters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-camera]");
    if (!button) return;
    const chosen = button.dataset.camera;
    if (chosen !== "all" && REQUIRED_ZONE[chosen] === undefined) return;
    state.filter = chosen;
    for (const other of el.filters.querySelectorAll("button[data-camera]")) {
      const on = other === button;
      other.classList.toggle("is-on", on);
      other.setAttribute("aria-pressed", on ? "true" : "false");
    }
    // Re-slice what is already on screen immediately, then refresh in the
    // background.
    renderEvents();
    if (!polling) schedulePoll(0);
  });

  el.retry.addEventListener("click", () => {
    stop();
    start();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
    } else {
      start();
    }
  });

  for (const card of cards.values()) {
    card.img.alt = "Latest picture from " + cameraLabel(card.camera);
  }

  setBarState("pending", "Connecting to cameras");
  render();
  start();
})();
