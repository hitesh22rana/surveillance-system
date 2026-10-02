/* Zone Console — read-only view of four cameras.
 *
 * Rules this file follows on purpose:
 *   - Every value shown is read from the camera service or counted from what it
 *     returned. An empty list is never presented as "all clear".
 *   - The live picture and the detector are judged separately and never borrow
 *     each other's health. A camera can be detecting perfectly while this
 *     browser refuses to decode its main stream, and the page has to say so
 *     instead of reporting a green camera it cannot actually see.
 *   - The live view is the camera's own main stream, handed over by go2rtc
 *     without re-encoding. Those streams are 2560×1440 at 25 fps as configured
 *     on the cameras; that is the source's own rate, never a measurement made
 *     here. The only numbers counted about the picture are the ones read off
 *     decoded frames.
 *   - HEVC is never negotiated down. If this browser cannot decode it, the card
 *     says exactly that; no lower quality stream is substituted quietly.
 *   - All text goes in through textContent. No innerHTML, no insertAdjacentHTML,
 *     no document.write.
 *   - Snapshot paths are only ever built from ids that match the camera
 *     service's own tracker id shape, which is also what nginx accepts.
 *   - Timers never overlap: one loop for stats and events, both stopped while
 *     the tab is hidden.
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

  const POLL_MS = 5000; // status + events
  const TIMEOUT_MS = 4000; // per request, below the proxy's own timeout
  const STATS_STALE_MS = 20000; // an older stats sample is not current state

  /* A 25 fps stream delivers a frame every ~40 ms and timeupdate fires several
   * times a second while it plays, so four seconds with neither is a stalled
   * stream rather than a quiet scene. */
  const VIDEO_STALE_MS = 4000;
  const CONNECT_TIMEOUT_MS = 12000; // no frame at all after connecting
  const RECONNECT_MS = 5000;
  const MAX_ATTEMPTS = 3;

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
    statLive: $("stat-live"),
    statDetectRate: $("stat-detect-rate"),
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
      frame: root.querySelector(".feed__frame"),
      placeholder: root.querySelector(".feed__placeholder"),
      liveText: root.querySelector(".feed__live-text"),
      liveTick: root.querySelector(".feed__live .tick"),
      detectText: root.querySelector(".feed__detect-text"),
      detectTick: root.querySelector(".feed__detect .tick"),
      stamp: root.querySelector(".feed__stamp"),
      live: {
        player: null,
        video: null,
        state: "starting", // starting | playing | error | unsupported
        reason: "",
        lastFrameAt: 0,
        attempts: 0,
        token: 0,
        timer: null,
      },
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
    if (!node) return;
    node.hidden = !shown;
  }

  function setBarState(tone, text) {
    setTick(el.barTick, tone);
    el.barStateText.textContent = text;
  }

  /* ------------------------------------------------------------- player -- */

  /* go2rtc's own web component, from the copy it serves at /video-rtc.js, so no
   * second player is vendored into this page. It is imported dynamically on
   * purpose: a static import would stop this whole module from evaluating if
   * that file were unreachable, taking the detection panels down with it. A
   * player that cannot load costs the live view and nothing else. */
  let VideoRTC = null;
  let playerState = "loading"; // loading | ready | failed

  const playerLoaded = import("/video-rtc.js")
    .then((mod) => {
      VideoRTC = mod.VideoRTC || mod.default || null;
      if (typeof VideoRTC !== "function") throw new Error("no VideoRTC export");

      /* The native element builds its <video> in oninit and only starts
       * connecting when .src is assigned, so init has to come first: a src set
       * before the video exists has nothing to attach to and gives up. */
      class ZoneLive extends VideoRTC {
        oninit() {
          super.oninit();
          const video = this.video;
          if (!video) return;
          video.muted = true;
          video.defaultMuted = true;
          video.autoplay = true;
          video.controls = false;
          video.playsInline = true;
          video.preload = "auto";
        }
      }

      if (!customElements.get("zone-live")) {
        customElements.define("zone-live", ZoneLive);
      }
      playerState = "ready";
      return true;
    })
    .catch(() => {
      VideoRTC = null;
      playerState = "failed";
      return false;
    });

  /* The main streams are HEVC. MediaSource has to be asked up front whether it
   * can decode that codec: a MediaSource the browser cannot play does not raise
   * an error, it simply never produces a frame, which would sit on screen as a
   * camera that is fine. Asking first also keeps four undecodable streams from
   * being pulled off the cameras for nothing.

   * The strings are the full codec strings go2rtc's own bundled player asks for.
   * A bare "hvc1"/"hev1" asks an open question: browsers answer it differently,
   * and some say yes while still refusing the profile and level actually on the
   * wire. That would label a stream this browser plays perfectly as unsupported,
   * which is the one answer that must never be wrong in that direction. */
  const HEVC_TYPES = Object.freeze([
    'video/mp4; codecs="hvc1.1.6.L153.B0"',
    'video/mp4; codecs="hev1.1.6.L153.B0"',
  ]);

  /* Both source kinds are consulted, because go2rtc plays through either: on
   * Safari the managed path is the one that has HEVC, and a card marked
   * unsupported there would be refusing a stream the browser is about to show. */
  const MEDIA_SOURCES = [
    typeof MediaSource !== "undefined" ? MediaSource : null,
    typeof ManagedMediaSource !== "undefined" ? ManagedMediaSource : null,
  ].filter((source) => source && typeof source.isTypeSupported === "function");

  let hevcPlayable = null;

  function canPlayMainStream() {
    if (hevcPlayable !== null) return hevcPlayable;
    hevcPlayable = false;
    for (const source of MEDIA_SOURCES) {
      for (const type of HEVC_TYPES) {
        try {
          if (source.isTypeSupported(type)) {
            hevcPlayable = true;
            return true;
          }
        } catch (err) {
          // A browser that cannot answer counts as unsupported, never as fine.
        }
      }
    }
    return hevcPlayable;
  }

  const NO_PLAYER = "the live player could not be loaded";
  const NO_DECODER = "this browser cannot decode the camera main stream (HEVC)";
  const NO_MEDIA_SOURCE = "this browser has no MediaSource support at all";
  const NO_PICTURE = "the camera main stream could not be played";

  function noDecoder() {
    return typeof MediaSource === "undefined" ? NO_MEDIA_SOURCE : NO_DECODER;
  }

  function unmountPlayer(card) {
    const live = card.live;
    if (live.timer !== null) {
      clearTimeout(live.timer);
      live.timer = null;
    }
    // Bumped so an event still in flight from the element being dropped cannot
    // be reported against the next attempt.
    live.token += 1;
    const player = live.player;
    live.player = null;
    live.video = null;
    if (!player) return;
    player.onerror = null;
    const video = player.video;
    if (video) {
      video.pause();
      video.removeAttribute("src");
    }
    player.remove();
  }

  function mountPlayer(card) {
    const live = card.live;
    unmountPlayer(card);
    live.token += 1;
    const token = live.token;
    live.state = "starting";
    live.reason = "";
    live.lastFrameAt = 0;

    if (playerState === "failed") {
      live.state = "error";
      live.reason = NO_PLAYER;
      return;
    }
    if (playerState === "loading") return;
    if (!canPlayMainStream()) {
      live.state = "unsupported";
      live.reason = noDecoder();
      return;
    }

    const player = document.createElement("zone-live");
    player.mode = "mse";
    /* Zeroed before oninit, and before the element is connected. The native
     * element watches itself with an IntersectionObserver at visibilityThreshold
     * and calls disconnectedCallback on itself when it reads as hidden, dropping
     * the socket. While a card is starting, the picture is covered by the
     * placeholder rather than hidden, so the default threshold finds no
     * intersection, tears the connection down before a single frame arrives, and
     * the card would read "starting" for ever. Page visibility is still honoured
     * here: the visibilitychange handler below unmounts and rebuilds every
     * player when the tab comes back. */
    player.visibilityThreshold = 0;
    if (!player.video) player.oninit();
    const video = player.video;
    if (!video) {
      live.state = "error";
      live.reason = NO_PLAYER;
      return;
    }
    /* Stated again here so the element is muted and inline whatever the native
     * oninit did or did not set: autoplay is only allowed while muted, and this
     * is a surveillance view that is never unmuted. */
    video.muted = true;
    video.defaultMuted = true;
    video.autoplay = true;
    video.controls = false;
    video.playsInline = true;
    video.preload = "auto";

    const onerror = () => {
      if (token === live.token) failCard(card, NO_PICTURE);
    };
    video.setAttribute("aria-label", "Live main stream from " + cameraLabel(card.camera));
    video.addEventListener("playing", () => {
      if (token === live.token) noteFrame(card);
    });
    video.addEventListener("timeupdate", () => {
      if (token === live.token) noteFrame(card);
    });
    video.addEventListener("error", onerror);
    player.onerror = onerror;

    live.player = player;
    live.video = video;

    /* The proxy path is fixed per camera and takes no query string, so this
     * element cannot be aimed at any other route. Initialised before it enters
     * the document, which is the order the native element expects. */
    player.src = "/live/" + card.camera;
    card.frame.append(player);

    live.timer = setTimeout(() => {
      live.timer = null;
      if (token !== live.token || live.lastFrameAt !== 0) return;
      failCard(card, "no picture from the main stream within " + Math.round(CONNECT_TIMEOUT_MS / 1000) + " s");
    }, CONNECT_TIMEOUT_MS);
  }

  function mountAll(force) {
    for (const card of cards.values()) {
      if (!force && card.live.player) continue;
      mountPlayer(card);
    }
  }

  /* A frame arrived. timeupdate only fires while currentTime advances, so this
   * is counted evidence that the stream is running, not that it once was. */
  function noteFrame(card) {
    const live = card.live;
    if (live.state === "error" || live.state === "unsupported") return;
    const first = live.lastFrameAt === 0;
    live.state = "playing";
    live.lastFrameAt = Date.now();
    if (!first) return;
    if (live.timer !== null) {
      clearTimeout(live.timer);
      live.timer = null;
    }
    renderCard(card);
    renderStrip();
    renderBarState();
  }

  function failCard(card, reason) {
    const live = card.live;
    if (live.timer !== null) {
      clearTimeout(live.timer);
      live.timer = null;
    }
    if (live.state === "unsupported") return;
    live.state = "error";
    live.reason = reason;
    live.attempts += 1;
    renderCard(card);
    renderStrip();
    renderBarState();
    renderNotice();

    if (live.attempts < MAX_ATTEMPTS) {
      live.timer = setTimeout(() => {
        live.timer = null;
        mountPlayer(card);
      }, RECONNECT_MS);
      return;
    }
    // Attempts spent: drop the element so its socket is released and let the
    // page's own Retry button rebuild it.
    unmountPlayer(card);
  }

  /* ------------------------------------------------------------- status -- */

  /* The live picture, judged only by frames this page has counted. Nothing
   * here consults the detector, so a card can never look alive on the strength
   * of detection happening behind it. */
  function liveStatus(card) {
    const live = card ? card.live : null;
    if (!live) {
      return { tone: "pending", playing: false, label: "live · no card", detail: "" };
    }
    if (live.state === "unsupported") {
      return {
        tone: "bad",
        playing: false,
        label: "live · HEVC unsupported",
        detail: live.reason + ". No lower quality stream is substituted; detection still runs separately.",
      };
    }
    if (live.state === "error") {
      return {
        tone: "bad",
        playing: false,
        label: "live · unavailable",
        detail: live.reason || NO_PICTURE,
      };
    }
    if (live.state === "starting") {
      return { tone: "pending", playing: false, label: "live · starting", detail: "Connecting to the camera main stream" };
    }
    if (!live.lastFrameAt) {
      return { tone: "pending", playing: false, label: "live · starting", detail: "Waiting for the first frame" };
    }

    const age = Date.now() - live.lastFrameAt;
    if (age > VIDEO_STALE_MS) {
      return {
        tone: "warn",
        playing: false,
        label: "live · stalled " + Math.round(age / 1000) + " s",
        detail: "No frame for " + Math.round(age / 1000) + " s",
      };
    }
    return { tone: "ok", playing: true, label: "live · playing", detail: "" };
  }

  /* The detector, judged only by the camera service's own stats. It says
   * nothing about the picture, which is why the fps here is labelled as the
   * detector's rate and not as a stream rate. */
  function detectorStatus(camera, sample) {
    const stats = isObject(sample) && isObject(sample.cameras) ? sample.cameras[camera] : null;
    if (!isObject(stats)) {
      return { tone: "pending", label: "detector · no data", known: false, running: false, detecting: false, process: null };
    }

    const capture = numberOrNull(stats.camera_fps);
    const process = numberOrNull(stats.process_fps);
    // Frigate reports detection_enabled as a JSON boolean. The numeric 1 is
    // accepted because Python's True == 1 and the upstream shape is not
    // something this page should assume; a string is never a yes.
    const detecting = stats.detection_enabled === true || stats.detection_enabled === 1;
    const running = capture !== null && capture > 0 && process !== null && process > 0;

    if (!running) {
      return { tone: "bad", label: "detector · not running", known: true, running: false, detecting: false, process: null };
    }
    if (!detecting) {
      return { tone: "warn", label: "detector · person detection off", known: true, running: true, detecting: false, process: process };
    }
    const rate = process >= 10 ? process.toFixed(0) : process.toFixed(1);
    return { tone: "ok", label: "detector · " + rate + " fps", known: true, running: true, detecting: true, process: process };
  }

  function renderCard(card) {
    const live = liveStatus(card);
    const detector = detectorStatus(card.camera, state.sample);

    setTick(card.liveTick, live.tone);
    card.liveText.textContent = live.label;
    setTick(card.detectTick, detector.tone);
    card.detectText.textContent = detector.label;

    /* The player element is never hidden. The native element tears its own
     * connection down when it reads as not visible, so hiding it while it is
     * still starting would stop it before a frame could ever arrive. Instead the
     * frame goes dark under the placeholder, which is opaque, sits over the
     * blank video and carries the reason — so a missing or frozen picture is
     * never left on screen reading as live. */
    setShown(card.placeholder, !live.playing);
    card.frame.dataset.video = live.playing ? "on" : "off";
    if (!live.playing) card.placeholder.textContent = live.detail;

    const video = card.live.video;
    const size = video && video.videoWidth > 0 ? video.videoWidth + "×" + video.videoHeight : "";
    if (live.playing) {
      card.stamp.textContent = (size ? size + " · " : "") + "frame " + clockTime(card.live.lastFrameAt);
    } else if (card.live.lastFrameAt) {
      card.stamp.textContent =
        "last frame " + clockTime(card.live.lastFrameAt) +
        " · " + Math.round((Date.now() - card.live.lastFrameAt) / 1000) + " s ago";
    } else {
      card.stamp.textContent = card.live.state === "starting" ? "no frame yet" : "—";
    }
  }

  function renderCards() {
    for (const card of cards.values()) renderCard(card);
  }

  function renderStrip() {
    let playing = 0;
    let detecting = 0;
    const rates = [];

    for (const camera of CAMERAS) {
      const card = cards.get(camera);
      if (card && liveStatus(card).playing) playing += 1;
      const detector = detectorStatus(camera, state.sample);
      if (detector.detecting) detecting += 1;
      rates.push(detector.process === null ? "—" : detector.process.toFixed(1));
    }

    el.statLive.textContent = playing + " of " + CAMERAS.length + " playing";
    // Four slots, in camera order, with a dash for anything unknown, so a short
    // list can never be read as all four running.
    el.statDetectRate.textContent = rates.join(" · ");

    if (!state.sample) {
      el.statInference.textContent = "—";
      el.statAge.textContent = state.statsStale ? "out of date" : "—";
      return;
    }

    /* With nothing streaming, inference_speed is Frigate's 10 ms default, not a
     * measurement. Showing it would read as a real detector figure. */
    const useDetectors = detecting > 0 && isObject(state.sample.detectors) ? state.sample.detectors : null;
    const parts = [];
    if (useDetectors) {
      for (const name of Object.keys(useDetectors)) {
        const ms = numberOrNull(useDetectors[name].inference_speed);
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

      // Frigate is holding this snapshot indefinitely. It is stated, not
      // changed: there is no keep, release or delete path on this page.
      const kept = event.retain_indefinitely === true || event.retain_indefinitely === 1;
      const badge = node.querySelector(".event__kept");
      setShown(badge, kept);
      if (kept) badge.textContent = "Kept for review";

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

  // Live views that ran out of attempts, so the notice offers the Retry button
  // that rebuilds them. A view still retrying is not an error worth announcing.
  function deadLiveViews() {
    let count = 0;
    for (const card of cards.values()) {
      if (card.live.state === "error" && card.live.attempts >= MAX_ATTEMPTS) count += 1;
    }
    return count;
  }

  function renderNotice() {
    const dead = deadLiveViews();

    if (!state.statsError && !state.eventsError && !state.statsStale && dead === 0) {
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
    } else if (state.eventsError) {
      tone = "warn";
      text =
        "Recent detections unavailable (" + state.eventsError.message +
        "). The list below is not a current answer.";
    } else {
      tone = "warn";
      text =
        plural(dead, "live view") + " unavailable (" +
        (playerState === "failed" ? NO_PLAYER : "the camera main stream did not play") +
        "). Detection status and events below are unaffected.";
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

    const detector = CAMERAS.map((camera) => detectorStatus(camera, state.sample));
    const live = CAMERAS.map((camera) => liveStatus(cards.get(camera)));

    const down = detector.filter((s) => s.tone === "bad").length;
    const detectorWarn = detector.filter((s) => s.tone === "warn").length;
    const detectorPending = detector.filter((s) => s.tone === "pending").length;
    const liveBad = live.filter((s) => s.tone === "bad").length;
    const liveWarn = live.filter((s) => s.tone === "warn").length;
    const livePending = live.filter((s) => s.tone === "pending").length;

    if (down > 0) {
      setBarState("bad", plural(down, "camera") + " not streaming");
      return;
    }
    if (detectorWarn > 0) {
      setBarState("warn", detectorWarn + (detectorWarn === 1 ? " camera needs" : " cameras need") + " attention");
      return;
    }
    if (detectorPending > 0) {
      setBarState("pending", "Waiting for camera data");
      return;
    }
    /* A camera can be detecting perfectly with no picture on screen here. That
     * is worth attention but it is not a dead camera, so it never reads as one. */
    if (liveBad > 0) {
      setBarState("warn", liveBad + (liveBad === 1 ? " live view is" : " live views are") + " unavailable");
      return;
    }
    if (liveWarn > 0) {
      setBarState("warn", liveWarn + (liveWarn === 1 ? " live view stalled" : " live views stalled"));
      return;
    }
    if (livePending > 0) {
      setBarState("pending", "Waiting for live video");
      return;
    }
    setBarState("ok", "All " + CAMERAS.length + " cameras live and watching");
  }

  /* ---------------------------------------------------------------- loop -- */

  let running = false;
  let pollTimer = null;
  let polling = false;

  function render() {
    renderBarState();
    renderCards();
    renderStrip();
    renderNotice();
    renderEvents();
  }

  function setBusy(busy) {
    el.retry.disabled = busy;
    el.retry.dataset.busy = busy ? "true" : "false";
    el.retryLabel.textContent = busy ? "Reading…" : "Retry now";
  }

  // Exactly one pending timer for the read loop: scheduling always replaces the
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

  /* Players are rebuilt whenever the tab comes back: a picture left on screen
   * through a long background tab is not evidence that the stream is still
   * running, and the native element may have dropped its socket meanwhile. */
  function rebuildLive() {
    for (const card of cards.values()) card.live.attempts = 0;
    mountAll(true);
  }

  function start() {
    if (running) return;
    running = true;
    schedulePoll(0);
    mountAll(false);
  }

  function stop() {
    running = false;
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
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
    rebuildLive();
    start();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
    } else {
      rebuildLive();
      start();
    }
  });

  setBarState("pending", "Connecting to cameras");
  render();
  start();

  playerLoaded.then(() => {
    if (!running) return;
    mountAll(false);
    render();
  });
})();