/* Surveillance Console — live view of four cameras, with manual snapshot review.
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
 *     service's own tracker id shape, which is also what nginx accepts and what
 *     the delete request is re-checked against before it is sent.
 *   - Timers never overlap: one loop for stats and events, both stopped while
 *     the tab is hidden. The archive is re-read only on its latest page, so a
 *     page the operator is reading through history stays where they put it.
 *   - The page and the camera are the whole of the URL state. Both are read from
 *     the query string on load, both are pushed onto the history when an operator
 *     asks for a different view, and Back restores them and reads that page
 *     again. A poll never writes history, and the worker's own clamp is written
 *     back by replacement so the URL always names the page actually on screen.
 *   - Deleting a snapshot is the one write this page can make, and it takes an
 *     explicit confirmation: two fixed endpoints, one POST, no retry, a finite
 *     timeout, and a captured id list so nothing that happens while the
 *     confirmation is open can widen what is deleted. The result claims the list
 *     was refreshed only once a fresh read of it has actually landed.
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

  /* The secondary zone, 60 seconds of tracked presence in the same area. A row
   * qualifies for review by entering it, and its internal name is never shown:
   * the row reads "Loitering ≥60 s", which says how long someone was tracked
   * there and nothing about why. That 60 s is the camera service's own count of
   * frames it tracked inside the zone, not a stopwatch run here. */
  const LOITERING_ZONE = Object.freeze({
    camera_1: "camera_1_loitering",
    camera_2: "camera_2_loitering",
    camera_3: "camera_3_loitering",
    camera_4: "camera_4_loitering",
  });

  const LOITERING_LABEL = "Loitering ≥60 s";

  /* The only two paths this page may write to, both served by the retention
   * worker, both fixed: no template, no query string, no second shape. */
  const SUMMARY_PATH = "/api/snapshots/summary";
  const DELETE_PATH = "/api/snapshots/delete";

  /* The one read this page forwards a query string to. The worker owns both
   * parameters and answers a fixed page of 25 rows plus the total and the page
   * count, so this page never asks for a size and never slices one for itself. */
  const EVENTS_PATH = "/api/snapshots/events";

  const POLL_MS = 5000; // status + events
  const TIMEOUT_MS = 4000; // per live request, below the proxy's own timeout
  const STATS_STALE_MS = 20000; // an older stats sample is not current state

  /* Counting a real total means walking the archive, so both archive reads — the
   * count behind the archive-wide delete and the page walk behind the pager —
   * are given the same 60 s the proxy gives them rather than the 4 s a live read
   * gets. A walk that outlives its window is reported, never read as an empty
   * archive. */
  const ARCHIVE_READ_MS = 60000;

  /* The delete is the one request allowed a long answer: the proxy gives it the
   * same 60 s and this page gives up at that same finite moment. There is no
   * retry — a delete that may already have happened is never sent twice. */
  const DELETE_TIMEOUT_MS = 60000;

  /* The worker's own ceiling on one id list, so the page stops at it rather than
   * sending a body it already knows will be refused. */
  const MAX_SELECTIONS = 1000;

  /* A 25 fps stream delivers a frame every ~40 ms and timeupdate fires several
   * times a second while it plays, so four seconds with neither is a stalled
   * stream rather than a quiet scene. */
  const VIDEO_STALE_MS = 4000;
  const CONNECT_TIMEOUT_MS = 12000; // no frame at all after connecting
  const RECONNECT_MS = 5000;
  const MAX_ATTEMPTS = 3;

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
    selectShown: $("select-shown"),
    clearSelection: $("clear-selection"),
    selectionCount: $("selection-count"),
    deleteSelected: $("delete-selected"),
    deleteAll: $("delete-all"),
    deleteResult: $("delete-result"),
    dialog: $("delete-dialog"),
    confirmTitle: $("delete-dialog-title"),
    confirmScope: $("delete-dialog-scope"),
    confirmCancel: $("delete-cancel"),
    confirmButton: $("delete-confirm"),
    confirmLabel: $("delete-confirm-label"),
    events: $("events"),
    eventsEmpty: $("events-empty"),
    pagePrev: $("page-prev"),
    pageNumbers: $("page-numbers"),
    pageNext: $("page-next"),
    pageLatest: $("page-latest"),
    statLive: $("stat-live"),
    statDetectRate: $("stat-detect-rate"),
    statInference: $("stat-inference"),
    statAge: $("stat-age"),
    eventTemplate: $("event-item"),
  };

  const state = {
    filter: "all",
    page: 1,
    pages: 1,
    pageSize: 1,
    total: 0,
    generation: 0, // one counter for every archive read, so a slow one cannot
    // answer a page the operator has already moved on from
    sample: null, // newest usable stats entry, or null
    statsRead: false,
    statsStale: false,
    statsAge: null, // seconds since service.last_updated, when known
    rows: [], // the rows of the page on screen, newest first, and nothing else
    eventsRead: false,
    eventsLoading: false,
    statsError: null,
    eventsError: null,
    deleting: false, // a confirmed delete is in flight; no second one can start
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

  /* Archive rows are read days apart, so they carry the date as well as the
   * clock: native Intl, in this browser's own locale and time zone, and no
   * formatting library. The live stamps above stay on the clock alone, because a
   * picture is never as old as a page. */
  const archiveStamp = (() => {
    try {
      const format = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "medium",
      });
      return (ms) => format.format(new Date(ms));
    } catch (err) {
      return (ms) => new Date(ms).toLocaleString();
    }
  })();

  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  // Frigate reports retain_indefinitely as a JSON boolean. The numeric 1 is
  // accepted for the same reason detection_enabled is; a string is never a yes.
  const isKept = (event) => event.retain_indefinitely === true || event.retain_indefinitely === 1;

  const inLoiterZone = (event) => {
    const zone = LOITERING_ZONE[event.camera];
    return zone !== undefined && Array.isArray(event.zones) && event.zones.includes(zone);
  };

  /* Retention, stated from what this event carries and never assumed. An
   * ordinary row expires after the six hours the camera service keeps it; a row
   * held by the secondary zone is held for review; a row held without that zone
   * is from the older archive, before the 60 s zone existed. */
  function retentionText(event) {
    if (!isKept(event)) return "6-hour retention";
    return inLoiterZone(event) ? "Kept for review" : "Previously kept";
  }

  /* A count the service reported, accepted only when it is already a whole
   * number of the right size. A decimal, a negative, a string or a missing
   * count is a reply this page does not understand, not a count of zero. */
  const wholeCount = (value) =>
    typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;

  // "camera_2" -> "Camera 2". Internal names never reach the page.
  const cameraLabel = (camera) => "Camera " + String(camera).slice(-1);

  /* The filters this page answers to: the one that is not a camera, plus the four
   * that are. A value read out of the URL is untrusted input like any other, so
   * both the URL and a clicked chip are checked against this and an unknown name
   * is refused here rather than forwarded to the worker. */
  const knownCamera = (value) => value === "all" || CAMERAS.includes(value);

  async function fetchJSON(url, timeoutMs = TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
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
     * one level up: the visibilitychange handler stops the read loop while the
     * tab is hidden and starts it again on the way back, leaving a player that
     * survived the background running instead of rebuilding it. */
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

  /* The archive is the worker's own set — a saved person snapshot on a
   * configured camera, which is exactly the set the archive-wide delete counts
   * and reaches — so it is listed as it answers and never re-filtered here.
   * A second filter in this file could only hide rows the total still counts,
   * which is how a page ends up claiming a range it is not showing. The one
   * guard kept is the id: it is the only field that becomes a delete body. */
  function listedEvent(event) {
    return isObject(event) && typeof event.id === "string" && EVENT_ID.test(event.id);
  }

  const zoneLabel = (event) => {
    const list = Array.isArray(event.zones) ? event.zones : [];
    // Both known zones are stripped: the area is stated in words and the tracked
    // presence is stated in words, so no configured zone name reaches the page.
    const named = list.filter(
      (zone) => zone !== REQUIRED_ZONE[event.camera] && zone !== LOITERING_ZONE[event.camera]
    );
    if (named.length > 0) return named.join(" + ");
    return "Monitored area";
  };

  /* The rows of the page on screen. Filtering is the worker's, paging is this
   * page's, and a selection outlives both: the Set below is never pruned
   * against a single page, because a page says nothing about any other one. */
  const visibleRows = () => state.rows;

  let renderedSignature = null;
  let renderedPages = null;

  // Rows are rebuilt from the template, and a row's checkbox is remembered by
  // node rather than by a data attribute, so a tracker id is never written into
  // markup that another script on this page could read.
  const checkIds = new WeakMap();

  function renderEvents() {
    renderEventList();
    renderPager();
    renderSelection();
  }

  function tallyText() {
    if (!state.eventsRead) {
      if (state.eventsError !== null) {
        return "Saved detections unavailable: " + state.eventsError.message;
      }
      return state.eventsLoading ? "Reading saved detections…" : "Waiting for detection data.";
    }
    const scope = state.filter === "all" ? "all cameras" : cameraLabel(state.filter);
    if (state.total === 0) return "No saved detections · " + scope;
    // The worker clamps a page past the end, so this is a shape this page does not
    // expect rather than a state it expects: it is said plainly, never as a range.
    if (state.rows.length === 0) {
      return "No detections on this page · page " + state.page + " of " + state.pages + " · " + scope;
    }
    const from = (state.page - 1) * state.pageSize + 1;
    return (
      "Showing " +
      from +
      "–" +
      (from + state.rows.length - 1) +
      " of " +
      state.total +
      " · page " +
      state.page +
      " of " +
      state.pages +
      " · " +
      scope
    );
  }

  /* How many page numbers are ever on screen, however long the archive grows.
   * This counts the interior run only: with a page pinned at each end beside it,
   * the number of buttons on screen never exceeds seven. The run is bounded so
   * the control is the same width on a hundred pages as on eight: a page count is
   * not allowed to become a layout. */
  const PAGE_WINDOW = 5;

  /* The run of page numbers to show, or null when the whole set already fits.
   * Five consecutive pages, slid so the current page sits in the middle and so
   * the run never runs off either end — near the first page it starts at 1, near
   * the last it ends at the last, and there is never a short gap at an edge. */
  function pageRun(pages, current) {
    if (pages <= PAGE_WINDOW) return null;
    const side = Math.floor(PAGE_WINDOW / 2);
    const from = Math.min(Math.max(current - side, 1), pages - PAGE_WINDOW + 1);
    return { from: from, to: from + PAGE_WINDOW - 1 };
  }

  /* A page number is a button, not an option in a select. A select would put the
   * whole page count behind one control that has to be opened before any of it is
   * reachable, and it renders as whatever the platform decides; buttons are the
   * same control everywhere, are reachable from the keyboard one at a time, and
   * are what the back button and a copied link come back to. It is returned
   * wrapped in an li because an ol may only hold li, not bare buttons. */
  function pageButton(page) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.className = "btn pager__page";
    button.type = "button";
    button.dataset.page = String(page);
    // Spoken as "Page 4" inside a named nav, which a bare "4" is not.
    button.setAttribute("aria-label", "Page " + page);
    button.textContent = String(page);
    item.appendChild(button);
    return item;
  }

  /* What stands between two runs of pages. An <li> and not a button: there is
   * nothing to press, nothing to focus and nothing to disable, and the numbers
   * either side of it are how the operator crosses it. */
  function pageGap() {
    const item = document.createElement("li");
    item.className = "pager__gap";
    item.setAttribute("aria-hidden", "true");
    item.textContent = "…";
    return item;
  }

  /* Every one of these controls is dead whenever a read is in flight or a delete
   * is: a page control that answered while the list behind it was still loading
   * would be describing a page nobody can see. The marked current page is disabled
   * too, because asking again for the page already on screen is not a move. */
  function renderPager() {
    const busy = state.eventsLoading || state.deleting;
    const pages = state.pages;
    const run = pageRun(pages, state.page);

    /* Buttons are rebuilt only when the visible set changes, so a poll landing on
     * the same page cannot take the focus off the page button an operator is on
     * with a keyboard. The current page is re-stated below every pass, however it
     * was arrived at: a click, a deep link, or the back button. */
    const signature = run === null ? "all:" + pages : run.from + ":" + run.to;
    if (renderedPages !== signature) {
      renderedPages = signature;
      while (el.pageNumbers.firstChild) el.pageNumbers.removeChild(el.pageNumbers.firstChild);
      if (run === null) {
        for (let page = 1; page <= pages; page += 1) el.pageNumbers.appendChild(pageButton(page));
      } else {
        if (run.from > 1) {
          el.pageNumbers.appendChild(pageButton(1));
          el.pageNumbers.appendChild(pageGap());
        }
        for (let page = run.from; page <= run.to; page += 1) {
          el.pageNumbers.appendChild(pageButton(page));
        }
        if (run.to < pages) {
          el.pageNumbers.appendChild(pageGap());
          el.pageNumbers.appendChild(pageButton(pages));
        }
      }
    }

    for (const button of el.pageNumbers.querySelectorAll(".pager__page")) {
      const current = button.dataset.page === String(state.page);
      if (current) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
      button.disabled = busy || current;
    }

    el.pagePrev.disabled = busy || state.page <= 1;
    el.pageNext.disabled = busy || state.page >= pages;
    el.pageLatest.disabled = busy || state.page === 1;
  }

  function renderEventList() {
    const rows = visibleRows();

    if (!state.eventsRead) {
      setShown(el.events, false);
      setShown(el.eventsEmpty, false);
      while (el.events.firstChild) el.events.removeChild(el.events.firstChild);
      renderedSignature = null;
      el.tally.textContent = tallyText();
      return;
    }

    el.tally.textContent = tallyText();

    // Rebuild only when a row's content actually changes, so a focused checkbox
    // is not destroyed by an unrelated poll. The signature carries the page it
    // was built for and the two facts that are printed on the row, so a change of
    // page, a change of retention or a change of zone is always a real change.
    const signature =
      state.filter +
      "#" +
      state.page +
      "#" +
      rows
        .map(
          (row) =>
            row.id + ":" + (isKept(row) ? "1" : "0") + (inLoiterZone(row) ? "L" : "-")
        )
        .join("|");
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
      const start = numberOrNull(event.start_time);
      // An event with no usable start_time says so rather than borrowing the
      // browser's own clock, which would date a snapshot to whenever it was read.
      const at = start === null ? "time not reported" : archiveStamp(start * 1000);
      const href = "/api/events/" + event.id + "/snapshot.jpg";

      const check = node.querySelector(".event__check");
      checkIds.set(check, event.id);
      check.checked = selection.has(event.id);
      // Named for this row and no other, so a tick is never an unnamed box in a
      // list. The label sits apart from the snapshot link, so ticking a row
      // neither opens the picture nor turns a page.
      check.setAttribute("aria-label", "Select snapshot from " + label + " at " + at);

      const link = node.querySelector(".event__shot");
      const img = node.querySelector(".event__img");
      link.href = href;
      link.setAttribute("aria-label", "Open snapshot from " + label + " at " + at);
      img.alt = "Person detected by " + label;
      img.src = href;

      node.querySelector(".event__camera").textContent = label;
      node.querySelector(".event__zone").textContent = zoneLabel(event);
      node.querySelector(".event__time").textContent = at;

      // Two facts, each from the event: whether the 60 s zone is among its zones,
      // and whether it is being held. Neither is a judgement about a person.
      const flag = node.querySelector(".event__flag");
      setShown(flag, inLoiterZone(event));
      if (inLoiterZone(event)) flag.textContent = LOITERING_LABEL;
      node.querySelector(".event__kept").textContent = retentionText(event);

      el.events.appendChild(node);
    }

    const empty = rows.length === 0;
    setShown(el.eventsEmpty, empty);
    setShown(el.events, !empty);
    el.eventsEmpty.textContent =
      state.total === 0
        ? state.filter === "all"
          ? "No saved detections yet"
          : "No saved detections for " + cameraLabel(state.filter)
        : "No detections on this page";
  }

  /* -------------------------------------------------------------- review -- */

  /* Tracker ids the operator has ticked, and nothing else. The Set outlives
   * pages, filters, polls and row rebuilds, so a tick is never lost because
   * something else was read; it is dropped only by a delete that accounted for
   * it, or by the operator clearing the selection. */
  const selection = new Set();

  /* What is known about each selected id, kept for the selected rows only, at
   * the moment they were ticked. A page read cannot be asked what is on the
   * other pages, so this is where an off-page selection's own facts come from:
   * the confirmation counts what it can really count, and the archive-wide
   * cutoff is applied to a start time that was read, never invented. */
  const selectedMeta = new Map();

  /* Only ids of the service's own shape are kept, and only ids of that shape are
   * ever sent: this Set is the one thing on the page that becomes a body. */
  const nativeIds = () => Array.from(selection).filter((id) => EVENT_ID.test(id));

  // Returns false at the worker's own ceiling rather than sending a body it has
  // already been told it will refuse.
  function selectRow(row) {
    if (selection.has(row.id)) return true;
    if (selection.size >= MAX_SELECTIONS) return false;
    selection.add(row.id);
    selectedMeta.set(row.id, {
      camera: row.camera,
      start: numberOrNull(row.start_time),
      kept: isKept(row),
    });
    return true;
  }

  function deselectRow(id) {
    selection.delete(id);
    selectedMeta.delete(id);
  }

  function overLimit() {
    return selection.size > MAX_SELECTIONS;
  }

  /* Checkbox state is written onto the rows already on screen, so select-all and
   * clear never rebuild the list and never take the focus off a row. */
  function syncChecks() {
    const busy = state.deleting;
    for (const box of el.events.querySelectorAll(".event__check")) {
      const id = checkIds.get(box);
      box.checked = id !== undefined && selection.has(id);
      // While a delete is in flight the whole bar is dead, and so is every row
      // box with it: the body was captured when the dialog opened, so a tick now
      // would change nothing about what is deleted and would read as if it had.
      box.disabled = busy;
    }
  }

  function renderSelection() {
    syncChecks();

    const rows = visibleRows();
    const shown = rows.reduce((total, row) => total + (selection.has(row.id) ? 1 : 0), 0);
    const total = selection.size;
    const busy = state.deleting;

    el.selectShown.disabled = busy || !state.eventsRead || rows.length === 0;
    el.selectShown.checked = rows.length > 0 && shown === rows.length;
    // Mixed is its own state: some shown rows are ticked, and the box says so.
    el.selectShown.indeterminate = shown > 0 && shown < rows.length;

    // "Shown" is this page and only this page, and it is said next to the total
    // so a selection carried across pages is never read as a selection on screen.
    el.selectionCount.textContent =
      total === 0
        ? "0 selected"
        : plural(total, "snapshot") +
            " selected · " +
            shown +
            " of " +
            rows.length +
            " shown" +
            (overLimit() ? " · over the " + MAX_SELECTIONS + " per-delete limit" : "");

    // Nothing to delete and no delete in flight: both controls say so by being
    // disabled rather than by answering with an empty request.
    el.clearSelection.disabled = busy || total === 0;
    el.deleteSelected.disabled = busy || total === 0;
    el.deleteAll.disabled = busy;
  }

  /* ------------------------------------------------------- confirmation -- */

  /* What the confirmation will send, captured when it opens. A poll, a filter
   * change, a new detection or another tick while the dialog is open cannot
   * change it: the body is built from this object and from nothing read after the
   * moment it was captured. */
  let intent = null;

  /* Only the two bodies in the contract can leave this page: a list of native
   * tracker ids, or the archive-wide cutoff the summary handed back. Anything
   * else is refused here, before a request exists. */
  function sendableBody(body) {
    if (!isObject(body)) return null;
    if (Array.isArray(body.ids)) {
      const ids = body.ids;
      if (ids.length === 0) return null;
      if (!ids.every((id) => typeof id === "string" && EVENT_ID.test(id))) return null;
      return { ids: ids.slice() };
    }
    const before = numberOrNull(body.before);
    if (body.all === true && before !== null && before > 0) return { all: true, before: before };
    return null;
  }

  function openConfirm(next) {
    // A second confirmation is never stacked on an open one, and a confirmation
    // with nothing valid to send is never opened at all.
    if (el.dialog.open) return false;
    const body = sendableBody(next.body);
    if (!body) return false;

    intent = { body: body, count: next.count };
    el.confirmTitle.textContent = next.title;
    el.confirmScope.textContent = next.scope;
    el.confirmLabel.textContent = next.label;
    // An archive that is already empty leaves nothing to confirm.
    el.confirmButton.disabled = next.count === 0;
    el.confirmButton.dataset.busy = "false";
    // Always armed on open: a dialog left over from a delete that has finished
    // must not arrive with its way out still switched off.
    el.confirmCancel.disabled = false;
    el.dialog.showModal();
    // Cancel is the default focus. An irreversible action never opens with the
    // destructive button already under the operator's thumb.
    el.confirmCancel.focus();
    return true;
  }

  /* The kept count is counted from what was read for each selected id when it was
   * ticked, so a selection carried across pages is counted as honestly as one
   * sitting on the page in front of the operator. An id this page has no record
   * of is left out of that count rather than guessed at. */
  function selectedScope(ids) {
    const kept = ids.reduce((total, id) => {
      const meta = selectedMeta.get(id);
      return total + (meta && meta.kept ? 1 : 0);
    }, 0);
    return (
      plural(ids.length, "selected snapshot") +
      " will be deleted" +
      (kept > 0 ? ", including " + kept + " kept for review" : "") +
      ", and nothing else. Detections that arrive while this is open are not affected."
    );
  }

  function archiveScope(summary) {
    if (summary.count === 0) {
      return "There are no saved snapshots to delete, so there is nothing to confirm.";
    }
    return (
      "All " +
      plural(summary.count, "saved snapshot") +
      " on all four cameras will be deleted, including every one held for review, " +
      "on every page of the archive and not only the pages shown above."
    );
  }

  /* ------------------------------------------------------------- deletes -- */

  /* One POST, once. No retry and no automatic second attempt, ever: a delete that
   * may already have happened must not be sent twice. Origin is added by the
   * browser on a POST and cannot be written from here, which is the point of
   * letting the worker check it. */
  async function postDelete(body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DELETE_TIMEOUT_MS);
    try {
      const res = await fetch(DELETE_PATH, {
        method: "POST",
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Surveillance-Action": "delete",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function describeDeleteError(err) {
    if (err && err.name === "AbortError") {
      return "no answer within " + Math.round(DELETE_TIMEOUT_MS / 1000) + " s";
    }
    if (err && err.name === "TypeError") return "no response from the snapshot service";
    if (err && err.message) return err.message;
    return "the request failed";
  }

  /* The reply is the only account of what happened, and it is taken at face value
   * or not at all. Every count must be a whole number that cannot be negative and
   * every entry of failed must be an id this page could itself have sent: a
   * missing field, a decimal, a negative, a float or an id of the wrong shape
   * makes the whole reply invalid, and an invalid reply is never repaired into a
   * number of zero. Guessing here would turn an answer this page does not
   * understand into a claim about what was deleted. */
  function deleteReply(payload) {
    if (!isObject(payload)) return null;
    const deleted = wholeCount(payload.deleted);
    const missing = wholeCount(payload.missing);
    const active = wholeCount(payload.active);
    if (deleted === null || missing === null || active === null) return null;
    if (!Array.isArray(payload.failed)) return null;
    // All of them, not the subset that happens to look familiar: one id this page
    // never sent means the reply is not the reply it claims to be.
    if (!payload.failed.every((id) => typeof id === "string" && EVENT_ID.test(id))) return null;
    return { deleted: deleted, missing: missing, active: active, failed: payload.failed.slice() };
  }

  function showOutcome(tone, text) {
    el.deleteResult.dataset.tone = tone;
    el.deleteResult.textContent = text;
  }

  /* A reply that came back settles the selection, and it settles it differently
   * for the two deletes. A selected delete is exactly the ids it was given: what
   * it accounted for leaves the selection and what it named as failed stays
   * selected, so a snapshot that survived is still on screen and still a
   * deliberate act. An archive-wide delete was never a selection at all, so
   * everything behind the captured cutoff leaves the selection whatever the
   * outcome: what failed is still saved, and leaving it ticked would imply a
   * second delete had been asked for. */
  function settleSelection(body, shape) {
    const chosen = Array.isArray(body.ids) ? new Set(body.ids) : null;

    if (chosen !== null) {
      const failed = new Set(shape.failed);
      for (const id of Array.from(selection)) {
        // Deleted and already-gone both leave the selection; a named failure stays
        // ticked, so a snapshot that survived is still a deliberate act.
        if (chosen.has(id) && !failed.has(id)) deselectRow(id);
      }
      return;
    }

    // Archive-wide: only a row behind the captured cutoff was in scope, so a
    // newer selection is left exactly as it was, and a selected row whose start
    // time was never read is left ticked rather than guessed out of scope.
    for (const id of Array.from(selection)) {
      const meta = selectedMeta.get(id);
      if (meta && meta.start !== null && meta.start < body.before) deselectRow(id);
    }
  }

  /* The clause about the list is written last, once the re-read behind it has
   * settled: a result never claims a refresh that did not happen, and a failed
   * read says so rather than reading as a refreshed list. */
  function refreshClause(refreshed) {
    return refreshed.ok
      ? " The list below has been refreshed."
      : " The list below could not be refreshed (" +
          refreshed.error +
          "), so it may be out of date.";
  }

  function showDeleteResult(body, shape, refreshed) {
    settleSelection(body, shape);

    const archive = !Array.isArray(body.ids);
    const parts = [];
    if (shape.deleted > 0) parts.push(plural(shape.deleted, "snapshot") + " deleted");
    if (shape.missing > 0) {
      parts.push(shape.missing + (shape.missing === 1 ? " was" : " were") + " already gone");
    }
    if (shape.failed.length > 0) {
      parts.push(
        plural(shape.failed.length, "snapshot") +
          " could not be deleted; still saved and " +
          (archive ? "not selected" : "still selected")
      );
    }
    if (parts.length === 0) parts.push("Nothing was deleted");
    // A detection that is still running keeps writing snapshots, so an emptied
    // archive is not the end of it and is not left to be assumed.
    if (shape.active > 0) {
      parts.push(
        plural(shape.active, "active detection") +
          (shape.active === 1 ? " is" : " are") +
          " still running and can save new snapshots"
      );
    }
    showOutcome(
      // A list that could not be re-read is not a clean result either, whatever
      // the delete itself reported.
      shape.failed.length > 0 || !refreshed.ok ? "warn" : "ok",
      parts.join(". ") + "." + refreshClause(refreshed)
    );
    // The selection settled just above, so the count and every row box are
    // re-stated from it rather than waiting for the next poll.
    renderSelection();
  }

  function showDeleteFailure(message, refreshed) {
    // No usable reply, so nothing is assumed in either direction: the selection
    // is untouched, no id is marked deleted, and nothing is sent again.
    showOutcome(
      "bad",
      "The delete did not finish (" +
        message +
        "). Some snapshots may already have been deleted. Nothing was sent again and " +
        "nothing is marked deleted here." +
        refreshClause(refreshed)
    );
  }

  async function runDelete() {
    const pending = intent;
    if (!pending || state.deleting) return;

    state.deleting = true;
    // Disabled before the request leaves, so a second click cannot start a second
    // delete while the first is still running.
    el.confirmButton.disabled = true;
    el.confirmButton.dataset.busy = "true";
    // The request is already out, so there is nothing left to cancel. Cancel goes
    // dead with the button and the dialog refuses Escape, or the delete would
    // keep running behind a closed dialog with nothing on screen saying so.
    el.confirmCancel.disabled = true;
    el.confirmLabel.textContent = "Deleting…";
    showOutcome("pending", "Deleting…");
    renderSelection();

    let reply = null;
    let failure = null;
    try {
      reply = await postDelete(pending.body);
    } catch (err) {
      failure = describeDeleteError(err);
    }

    const shape = reply === null ? null : deleteReply(reply);
    intent = null;
    el.dialog.close();

    // The list is re-read rather than patched: only the service knows what is
    // left, and new snapshots may have been written on the way. This is a read,
    // so it can fail without anything having been sent a second time — the POST
    // above is the only one, whether or not this one answers.
    const refreshed = await readEvents();

    /* Every control stays dead until the re-read has landed and the result is on
     * screen. Releasing the bar any earlier would put a live Delete button next to
     * a delete whose outcome is still unknown, which is the one state in which a
     * second click would start a second POST. */
    state.deleting = false;
    el.confirmCancel.disabled = false;
    render();

    if (failure !== null) showDeleteFailure(failure, refreshed);
    else if (shape === null) {
      showDeleteFailure("the reply was not a valid delete result", refreshed);
    } else showDeleteResult(pending.body, shape, refreshed);

    schedulePoll(POLL_MS);
  }

  /* The archive count and the cutoff that scopes it, read fresh every time the
   * archive-wide confirmation is opened. This is a read: a count of zero deletes
   * nothing and simply leaves the confirm button disabled. */
  function usableSummary(payload) {
    if (!isObject(payload)) return null;
    const count = payload.count;
    const before = numberOrNull(payload.before);
    if (typeof count !== "number" || !Number.isFinite(count) || count < 0) return null;
    if (before === null || before <= 0) return null;
    return { count: Math.floor(count), before: before };
  }

  async function readSummary() {
    try {
      return { summary: usableSummary(await fetchJSON(SUMMARY_PATH, ARCHIVE_READ_MS)) };
    } catch (err) {
      return { error: describe(err) };
    }
  }

  async function confirmArchiveDelete() {
    if (state.deleting) return;
    // Held while the count is read, so a second click cannot open a second
    // confirmation, and handed back whether or not one opens.
    el.deleteAll.disabled = true;
    const read = await readSummary();
    el.deleteAll.disabled = state.deleting;

    if (!read.summary) {
      showOutcome(
        "bad",
        "The saved snapshot count could not be read (" +
          (read.error || "unexpected response shape") +
          "), so no confirmation was opened and nothing was sent. Nothing is deleted until a " +
          "count is in front of you."
      );
      render();
      return;
    }

    const summary = read.summary;
    openConfirm({
      title: "Delete every saved snapshot?",
      scope: archiveScope(summary),
      label: summary.count === 0 ? "Delete saved snapshots" : "Delete all " + summary.count,
      count: summary.count,
      body: { all: true, before: summary.before },
    });
    render();
  }

  /* ------------------------------------------------------------- actions -- */

  function onSelectShown() {
    if (state.deleting) return;
    // This page and only this page, which is exactly what is on screen. Never the
    // whole archive, and never a page the operator is not looking at.
    const wanted = el.selectShown.checked;
    let refused = false;
    for (const row of visibleRows()) {
      if (wanted) {
        if (!selectRow(row)) refused = true;
      } else {
        deselectRow(row.id);
      }
    }
    renderSelection();
    if (refused) showLimit();
  }

  function onClearSelection() {
    if (state.deleting) return;
    selection.clear();
    selectedMeta.clear();
    renderSelection();
  }

  function onDeleteSelected() {
    if (state.deleting) return;
    const ids = nativeIds();
    if (ids.length === 0) return;
    if (ids.length > MAX_SELECTIONS) {
      showLimit();
      return;
    }
    openConfirm({
      title: "Delete the selected snapshots?",
      scope: selectedScope(ids),
      label: "Delete " + ids.length,
      count: ids.length,
      body: { ids: ids },
    });
  }

  /* The worker's ceiling, said in words rather than sent as a refusal: nothing
   * is deleted and nothing is de-selected, so the operator keeps the ticks they
   * already made and is told exactly what to do about them. */
  function showLimit() {
    showOutcome(
      "warn",
      "That would make " +
        plural(selection.size, "selected snapshot") +
        ", and one delete carries at most " +
        MAX_SELECTIONS +
        " ids. Nothing was deleted: clear the selection, or delete in smaller batches."
    );
  }

  /* A tick on one row, wherever it was reached from. One delegated handler on the
   * list itself, so a row that has just been built is already wired. */
  function onRowCheck(event) {
    const box = event.target.closest(".event__check");
    if (!box || !el.events.contains(box)) return;
    // While a delete is in flight every box is disabled, and a synthetic change is
    // put back where it was rather than acted on: the body was already captured.
    if (state.deleting) {
      syncChecks();
      return;
    }
    const id = checkIds.get(box);
    if (id === undefined) return;

    if (!box.checked) {
      deselectRow(id);
      renderSelection();
      return;
    }
    const row = visibleRows().find((candidate) => candidate.id === id);
    if (!row) {
      // The row this box belongs to is no longer the one on screen, so its facts
      // cannot be recorded. The box goes back rather than selecting what it names.
      syncChecks();
      return;
    }
    if (!selectRow(row)) {
      box.checked = false;
      renderSelection();
      showLimit();
      return;
    }
    renderSelection();
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

  /* One page of the archive, {events, page, page_size, total, pages}, from the
   * worker rather than from the raw event list. Every count has to be a whole
   * number of the right size and a page has to sit inside the page count, so a
   * decimal, a string, a missing field or a page past the end is a reply this
   * page does not understand, never a total of zero and never a row list read
   * from somewhere else. */
  function usablePage(payload) {
    if (!isObject(payload)) return null;
    if (!Array.isArray(payload.events)) return null;
    const page = wholeCount(payload.page);
    const pageSize = wholeCount(payload.page_size);
    const total = wholeCount(payload.total);
    const pages = wholeCount(payload.pages);
    if (page === null || pageSize === null || total === null || pages === null) return null;
    if (pageSize < 1 || pages < 1 || page < 1 || page > pages) return null;
    if (total < payload.events.length) return null;
    return { events: payload.events, page: page, pageSize: pageSize, total: total, pages: pages };
  }

  /* The camera filter and the page number are the worker's business, so changing
   * either is a new read rather than a re-slice of what is already on screen.
   *
   * One generation counter covers every read: a page walk is slow enough to still
   * be in flight when the operator has moved on, and a reply that arrives late is
   * dropped rather than filed under the page and camera that were asked for
   * afterwards. It also means only the newest read owns the loading and error
   * state, so a superseded one cannot clear a list that is still correct.
   *
   * transition is a move to a different page or camera, not a re-read of the page
   * already on screen: the old rows leave immediately, because leaving them under
   * a new page number is the one thing this must never do.
   *
   * Reports whether the list it left behind is a list that was actually read, so
   * a caller can say so rather than assume it. A failed read is never presented as
   * a refreshed list. */
  async function readEvents(transition) {
    const generation = ++state.generation;
    const page = state.page;
    const camera = state.filter;

    if (transition) {
      state.rows = [];
      state.eventsRead = false;
      state.eventsError = null;
      renderedSignature = null;
    }
    state.eventsLoading = true;
    // Only the archive controls move here: the notice, the status bar and the
    // Retry button belong to the read loop, which is not what is being waited on.
    renderEvents();

    const stale = { ok: false, error: "a newer read was still in flight", stale: true };
    try {
      const payload = await fetchJSON(
        EVENTS_PATH + "?page=" + page + "&camera=" + encodeURIComponent(camera),
        ARCHIVE_READ_MS
      );
      if (generation !== state.generation) return stale;
      const parsed = usablePage(payload);
      if (!parsed) throw new Error("unexpected response shape");

      state.rows = parsed.events.filter((event) => listedEvent(event));
      /* The worker's own page number is adopted, which is how a page that has just
       * been deleted out from under the operator is clamped to the last one, and
       * how a deep link past the end of a shorter archive lands on a real page.
       * The URL is corrected to the page that was actually read — and by
       * replacement rather than by a push, because the operator did not ask for
       * this page and it is not a step in the history they walked here. */
      state.page = parsed.page;
      if (parsed.page !== page) writeUrlState(false);
      state.pageSize = parsed.pageSize;
      state.total = parsed.total;
      state.pages = parsed.pages;
      state.eventsRead = true;
      state.eventsError = null;
      return { ok: true, error: null };
    } catch (err) {
      if (generation !== state.generation) return stale;
      // An unreadable page is an error, not an empty archive.
      state.rows = [];
      state.eventsRead = false;
      state.eventsError = { message: describe(err) };
      return { ok: false, error: describe(err) };
    } finally {
      if (generation === state.generation) {
        state.eventsLoading = false;
        render();
      }
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
        "Saved detections unavailable (" + state.eventsError.message +
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
      /* The archive is only re-read on its latest page, and only when no other
       * read of it is already in flight. A page the operator is reading through
       * history holds still until they ask for another one, and the status and
       * the live pictures are unaffected either way. */
      await Promise.all([
        readStats(),
        state.page === 1 && !state.eventsLoading ? readEvents(false) : Promise.resolve(),
      ]);
    } finally {
      polling = false;
      setBusy(false);
    }
    render();
    schedulePoll(POLL_MS);
  }

  /* The Retry button's own rebuild, and nothing else: it clears the attempt
   * count and remounts every card, so an operator asking for a fresh start gets
   * one. Resuming from a hidden tab deliberately does not come through here,
   * because that would drop four working streams to prove nothing. */
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

  /* ---------------------------------------------------------- url state --- */

  /* Page and camera are the two things an operator navigates by, so both live in
   * the query string: a deep link has to survive a reload, and the back button has
   * to step back through the pages somebody actually read. Nothing else is written
   * there — the archive is read-only apart from the one delete above, and a URL is
   * not a place to keep anything else.
   *
   * This is the initial parse, and it deliberately does not clamp to the page
   * count this page starts out with: state.pages is 1 until the worker's first
   * answer lands, so clamping here would turn a deep link to page 5 into a read of
   * page 1 and the operator would never see the archive they asked for. An unusable
   * value falls back to the first page and to every camera instead. */
  const PAGE_PARAM = /^[1-9][0-9]*$/;

  function readUrlState() {
    const params = new URLSearchParams(window.location.search);
    const rawPage = params.get("page");
    const rawCamera = params.get("camera");
    const page = Number(rawPage);
    return {
      page: rawPage !== null && PAGE_PARAM.test(rawPage) && Number.isSafeInteger(page) ? page : 1,
      filter: knownCamera(rawCamera) ? rawCamera : "all",
    };
  }

  /* Canonical: the page is written only when it is not the first, and the camera
   * only when it is not all, so the common case is a bare path and no two
   * navigations ever produce two spellings of the same view. Every other parameter
   * and the fragment are left exactly as they were found — this page did not put
   * them there and does not get to clean them up. A push is a move the operator
   * asked for; a replace is the same view being described more accurately. */
  function writeUrlState(push) {
    const url = new URL(window.location.href);
    if (state.page > 1) url.searchParams.set("page", String(state.page));
    else url.searchParams.delete("page");
    if (state.filter === "all") url.searchParams.delete("camera");
    else url.searchParams.set("camera", state.filter);
    const entry = url.pathname + url.search + url.hash;
    if (push) window.history.pushState(null, "", entry);
    else window.history.replaceState(null, "", entry);
  }

  /* The pressed state of the filter chips follows state.filter instead of being
   * written at the click, so a view restored from a link or from the back button
   * shows the filter it is rather than leaving the old chip lit. */
  function markFilter(camera) {
    for (const chip of el.filters.querySelectorAll("button[data-camera]")) {
      const on = chip.dataset.camera === camera;
      chip.classList.toggle("is-on", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  /* Page and camera are both the worker's to answer, so a move is a new read.
   * Nothing here touches a live player: turning a page is a different thing from
   * reconnecting four cameras. The move is pushed onto the history stack, because
   * the operator asked for this page and Back has to return them to the one they
   * were reading. */
  function goToPage(target) {
    if (state.deleting || state.eventsLoading) return;
    const wanted = Math.min(Math.max(target, 1), state.pages);
    if (wanted === state.page) return;
    state.page = wanted;
    writeUrlState(true);
    readEvents(true);
  }

  el.filters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-camera]");
    if (!button) return;
    const chosen = button.dataset.camera;
    if (!knownCamera(chosen)) return;
    // The filter already on screen, already on its first page: nothing to change,
    // and nothing to add to the stack Back walks.
    if (chosen === state.filter && state.page === 1) return;
    state.filter = chosen;
    // The filtered archive has its own paging, so page 7 of the old set is not a
    // page of the new one. The selection is left alone: a camera filter changes
    // what is on screen, never what the operator has already decided.
    state.page = 1;
    writeUrlState(true);
    markFilter(chosen);
    readEvents(true);
  });

  el.pagePrev.addEventListener("click", () => {
    goToPage(state.page - 1);
  });

  el.pageNext.addEventListener("click", () => {
    goToPage(state.page + 1);
  });

  /* One delegated handler on the run itself, because the run is rebuilt whenever
   * the visible set changes and a per-button listener would have to be re-attached
   * every time. The same reason the row boxes are delegated. */
  el.pageNumbers.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-page]");
    if (!button || button.disabled) return;
    goToPage(Number(button.dataset.page));
  });

  // The newest page, asked for rather than waited for.
  el.pageLatest.addEventListener("click", () => {
    goToPage(1);
  });

  /* Back and Forward are navigation, not a poll: they restore the page and the
   * camera the URL names and read that page. Nothing is pushed — the entry being
   * restored is already on the stack — and a read that was already in flight is
   * dropped by the generation counter in readEvents rather than answering a page
   * the operator has already walked away from.
   *
   * While a delete is in flight the restore is still applied to state, so the URL
   * and the page on screen agree, but its read is left to the re-read that follows
   * the POST: the captured body cannot be widened by any of this, and one read at
   * a time is the rule the whole file follows. */
  window.addEventListener("popstate", () => {
    const next = readUrlState();
    state.page = next.page;
    state.filter = next.filter;
    markFilter(next.filter);
    if (state.deleting) return;
    readEvents(true);
  });

  el.retry.addEventListener("click", () => {
    stop();
    rebuildLive();
    start();
  });

  // One delegated handler for every row box, including rows built a moment ago.
  el.events.addEventListener("change", onRowCheck);

  el.selectShown.addEventListener("change", onSelectShown);
  el.clearSelection.addEventListener("click", onClearSelection);
  el.deleteSelected.addEventListener("click", onDeleteSelected);
  el.deleteAll.addEventListener("click", confirmArchiveDelete);

  // The only place a delete is ever sent: the dialog's own confirm button.
  el.confirmButton.addEventListener("click", () => {
    runDelete();
  });

  // Escape, or the cancel button: the operator withdrew. Nothing is sent now and
  // nothing is remembered for later. While the request is in flight there is
  // nothing left to withdraw, so the dialog is held shut until the POST settles.
  el.dialog.addEventListener("cancel", (event) => {
    if (state.deleting) {
      event.preventDefault();
      return;
    }
    intent = null;
  });

  document.addEventListener("visibilitychange", () => {
    // Coming back only resumes the read loop. A player that is still healthy
    // through a hidden tab keeps the picture it already has rather than being
    // torn down and reconnected, and a card that did fail has already been
    // rebuilt by the watchdog above or is waiting for the Retry button.
    if (document.hidden) {
      stop();
    } else {
      start();
    }
  });

  /* The page and the camera are taken from the URL before anything is drawn and
   * before the first archive read, so a reload straight onto a deep link arrives
   * on the page it names rather than on page 1 of every camera. */
  const initial = readUrlState();
  state.page = initial.page;
  state.filter = initial.filter;
  markFilter(state.filter);
  // Canonical from the first paint, so the URL an operator copies out of the
  // address bar is one a reload comes back to. Nothing is pushed: nothing has been
  // navigated yet.
  writeUrlState(false);

  setBarState("pending", "Connecting to cameras");
  render();

  /* The first archive read is asked for whatever page the URL named, deep link
   * included. The poll loop below only re-reads the newest page, so a reload onto
   * page 5 would otherwise show four cameras and no archive at all. */
  readEvents(true);

  start();

  playerLoaded.then(() => {
    if (!running) return;
    mountAll(false);
    render();
  });
})();