/* Surveillance console - live view of four cameras, with manual snapshot review.
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
 *   - A row's entry tag is read off the event's own zones and never from the
 *     camera: a restricted-area entry or a 60 s loitering is claimed only
 *     against that camera's own zone, and only for a person. No configured zone
 *     name is ever printed.
 *   - Timers never overlap: one loop for stats and events, both stopped while
 *     the tab is hidden. The archive is re-read only on its latest page, so a
 *     page the operator is reading through history stays where they put it.
 *   - The page, the camera and the review scope are the whole of the URL state.
 *     All three are read from the query string on load, all three are pushed onto
 *     the history when an operator asks for a different view, and Back restores
 *     them and reads that page again. A poll never writes history, and the
 *     worker's own clamp is written back by replacement so the URL always names
 *     the page actually on screen.
 *   - Paging is three buttons and one read-only line saying which page is on
 *     screen. A thousand pages and eight pages are the same four controls, so
 *     the page count is never allowed to become a layout or a list to pick from.
 *   - Each camera card is one always-open, non-modal <dialog> styled to sit
 *     inline. Expanding closes that same element and reopens it with
 *     showModal(), so the frame, the <video> and the go2rtc player are never
 *     moved, cloned or re-parented: the stream before and after is the one
 *     session that was already open, not a second one. Minimising puts the very
 *     same element back inline.
 *   - One reusable <dialog> views every snapshot. It is handed the href off the
 *     row's own anchor, and only after that href has been checked to be a
 *     same-origin path of the shape the proxy serves. A modified or middle click
 *     is left to the browser, so the link is still a real link.
 *   - Deleting a snapshot is the one write this page can make, and it takes an
 *     explicit confirmation: two fixed endpoints, one POST, no retry, a finite
 *     timeout, and a captured id list so nothing that happens while the
 *     confirmation is open can widen what is deleted. The result claims the list
 *     was refreshed only once a fresh read of it has actually landed.
 *   - The experimental possible-fighting watch is a third read and nothing more. It
 *     arrives over one exact GET, this page has no control over it, and it cannot
 *     label, keep, delete or stop anything. Its status is checked field by field
 *     before any row is allowed to believe it, Monitoring needs a ready model and a
 *     camera that is analysing with recent data, and the read is fired beside the
 *     read cycle rather than awaited inside it so it can never hold up the live
 *     players, the archive or the arming status.
 *   - "Kept for review" always means retain_indefinitely and nothing else. A row the
 *     fighting watch labelled and kept is kept, so it reads as kept rather than as
 *     an inherited record, and that label can never widen what the kept filter
 *     means.
 *   - No copy here claims Home or a disarm stops everything. It stops armed
 *     person-presence review and only that; camera_4, the animal rule and the
 *     fighting watch are named as continuing wherever a disarm could be read as a
 *     total stop.
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

  /* The restricted area, on the two cameras that have one. Like the area zone its
   * internal name never reaches the page: a row reads "Restricted area entry", which
   * says a person crossed into that area and nothing at all about why, from where, or
   * who. camera_2 and camera_3 are not in this map because they have no restricted
   * area and never will. */
  const RESTRICTED_ZONE = Object.freeze({
    camera_1: "camera_1_restricted",
    camera_4: "camera_4_restricted",
  });

  /* The area a camera's people are judged in, which is what the worker maps for it:
   * the restricted polygon on camera_1 and camera_4, and the whole-frame monitored area
   * on camera_2 and camera_3. A kept person row carries one of these, whatever it means
   * for that camera, so "Kept for review" is not reserved for an entry. */
  const WATCHED_ZONE = Object.freeze({
    camera_1: "camera_1_restricted",
    camera_2: "camera_2_area",
    camera_3: "camera_3_area",
    camera_4: "camera_4_restricted",
  });

  const RESTRICTED_LABEL = "Restricted area entry";

  const PERSON_LABEL = "person";

  /* The sub-label word the camera service reports, and only that.

   * Frigate carries it as a bare string or as [label, score]. Both shapes are read
   * here, and the score is deliberately dropped rather than shown: a number beside a
   * row would read as a confidence, and it is neither one nor a measured accuracy.
   * Any other shape is not a sub-label this page understands and yields no word. */
  const subLabelWord = (event) => {
    const value = event.sub_label;
    if (typeof value === "string") return value;
    if (Array.isArray(value) && value.length === 2 && typeof value[0] === "string") {
      return value[0];
    }
    return "";
  };

  /* This snapshot is one the experimental watch labelled.

   * Only a person, and only on the two rooms it watches: the label is the camera
   * service's own record of what was written, so a room outside the watch can never
   * acquire this chip, and neither can an animal the watch does not label. It is a
   * possibility a model reported, and the row says so in words rather than claiming
   * a finding. */
  const isPossibleFighting = (event) =>
    event.label === PERSON_LABEL &&
    FIGHT_CAMERAS.includes(event.camera) &&
    subLabelWord(event) === FIGHT_SUB_LABEL;

  /* The ten COCO 91 animal classes, and the three cameras that watch for them.
   * These mirror config/config.example.yml and scripts/retain.py: the worker pins
   * an animal on sight and never for loitering, so an animal row is held without
   * ever carrying camera_N_loitering and this page has to say "Kept for review"
   * from the label rather than from the zone. camera_4 tracks person only. */
  const ANIMAL_LABELS = Object.freeze([
    "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe",
  ]);
  const ANIMAL_CAMERAS = Object.freeze(["camera_1", "camera_2", "camera_3"]);

  const isAnimal = (event) =>
    ANIMAL_LABELS.includes(event.label) && ANIMAL_CAMERAS.includes(event.camera);

  /* The only two paths this page may write to, both served by the retention
   * worker, both fixed: no template, no query string, no second shape. */
  const SUMMARY_PATH = "/api/snapshots/summary";
  const DELETE_PATH = "/api/snapshots/delete";

  /* Entry monitoring: the three cameras a person may arm by hand, and the one that is
   * always armed and has no control at all. Both bodies below are the same fixed path
   * the worker validates: {"mode":"home"|"away"} for the two presets, and
   * {"camera":"camera_1"|"camera_2"|"camera_3","armed":bool} for one camera. Neither
   * touches a camera, a zone or a configuration - the worker saves the setting in one
   * file and the status line says out loud that Camera 4 cannot be turned off. */
  const ENTRY_CAMERAS = Object.freeze(["camera_1", "camera_2", "camera_3"]);
  const ALWAYS_CAMERA = "camera_4";
  /* What each camera's area is, in words. Camera 1 has a drawn boundary to cross; the
   * other two are internal views whose area is the whole frame, so arming one of
   * them keeps anyone in view rather than an entry. */
  const ENTRY_PLACE = Object.freeze({
    camera_1: "monitored area 1",
    camera_2: "monitored area 2",
    camera_3: "area_3",
    camera_4: "area_4",
  });
  const ENTRY_PATH = "/api/security/entry";
  const ENTRY_ACTION = "arm-entry";

  /* Arming is the one write here whose outcome can be genuinely unknown: a POST
   * that times out may or may not have been saved. So it gets a longer window
   * than a live read, and its failure path is always a fresh read of the server's
   * own state rather than a guess at what happened. */
  const ENTRY_TIMEOUT_MS = 15000;

  /* The one read this page forwards a query string to. The worker owns all three
   * parameters and answers a fixed page of 25 rows plus the total and the page
   * count, so this page never asks for a size and never slices one for itself. */
  const EVENTS_PATH = "/api/snapshots/events";

  /* The review scope, as the worker accepts it. "kept" is its own
   * retain_indefinitely flag and nothing else, so a row this suite no longer has
   * a rule for is still a kept row; the alternative, re-deriving it from the label,
   * the zone and the armed state, is the guess this deliberately does not make. The
   * page adds no filter of its own on top: the total and the pages come from the
   * same narrowed set the rows do. */
  const REVIEW_FILTERS = Object.freeze(["all", "kept"]);
  const REVIEW_KEPT = "kept";
  const knownReview = (value) => REVIEW_FILTERS.includes(value);

  /* The experimental possible-fighting watch, read over one exact safe route.

   * It is a status read and nothing else. This page cannot arm it, label an event,
   * keep one, stop it or change a threshold: the proxy route it comes through is a
   * GET with no body and no action header, and the sidecar behind it publishes no
   * write at all. The two rooms are the same two the entry place names already give
   * (ENTRY_PLACE), so there is no second list of places to keep in step.

   * "Kept" still means retain_indefinitely and nothing else. This watch sets that
   * flag on the snapshots it labels, and reading its label can therefore never widen
   * what the kept filter means. */
  const FIGHT_PATH = "/api/activity/fighting";
  const FIGHT_CAMERAS = Object.freeze(["camera_2", "camera_3"]);
  const FIGHT_SUB_LABEL = "possible_fighting";
  const FIGHT_CHIP = "Possible fighting activity";
  const FIGHT_MODEL_STATES = Object.freeze(["loading", "ready", "refused"]);
  const FIGHT_CAMERA_STATES = Object.freeze(["warming", "analyzing", "unavailable"]);
  const FIGHT_MONITORING = "monitoring";

  /* How old the sidecar's own last_capture and last_scored may be, judged against
   * this browser's clock. The sidecar ages its own fields before answering, and
   * this is the same limit applied again on the far side of the proxy: a reply that
   * took a long time to arrive, or a sidecar whose clock has drifted, must not be
   * able to present an old moment as a current one. */
  const FIGHT_STALE_MS = 10000;

  const POLL_MS = 5000; // status + events
  const TIMEOUT_MS = 4000; // per live request, below the proxy's own timeout
  const STATS_STALE_MS = 20000; // an older stats sample is not current state

  /* Counting a real total means walking the archive, so both archive reads - the
   * count behind the archive-wide delete and the page walk behind the pager -
   * are given the same 60 s the proxy gives them rather than the 4 s a live read
   * gets. A walk that outlives its window is reported, never read as an empty
   * archive. */
  const ARCHIVE_READ_MS = 60000;

  /* The delete is the one request allowed a long answer: the proxy gives it the
   * same 60 s and this page gives up at that same finite moment. There is no
   * retry - a delete that may already have happened is never sent twice. */
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

  /* The one path a snapshot may be read from, on this origin only. A snapshot
   * the viewer is offered has to be one of this page's own already-validated
   * tracker ids at the fixed path the proxy serves; nothing else is ever put in
   * front of it, whatever an anchor somewhere in the document happens to say. */
  const SNAPSHOT_PATH = /^\/api\/events\/[0-9]+(?:\.[0-9]+)?-[a-z0-9]{6}\/snapshot\.jpg$/;

  const $ = (id) => document.getElementById(id);

  const el = {
    barStateText: $("backend-state-text"),
    barTick: $("backend-state").querySelector(".tick"),
    notice: $("notice"),
    noticeText: $("notice-text"),
    retry: $("retry"),
    retryLabel: $("retry-label"),
    feeds: $("feeds"),
    filters: $("filters"),
    reviewFilters: $("review-filters"),
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
    shotDialog: $("snapshot-dialog"),
    shotStage: $("snapshot-stage"),
    shotCaption: $("snapshot-caption"),
    shotImage: $("snapshot-image"),
    shotState: $("snapshot-state"),
    shotClose: $("snapshot-close"),
    events: $("events"),
    eventsEmpty: $("events-empty"),
    pagePrev: $("page-prev"),
    pageStatus: $("page-status"),
    pageNext: $("page-next"),
    pageLatest: $("page-latest"),
    statLive: $("stat-live"),
    statDetectRate: $("stat-detect-rate"),
    statInference: $("stat-inference"),
    statAge: $("stat-age"),
    entry: $("entry-controls"),
    entryResult: $("entry-result"),
    entryNotice: $("entry-notice"),
    entryConfirm: $("entry-confirm"),
    entryConfirmTitle: $("entry-confirm-title"),
    entryConfirmScope: $("entry-confirm-scope"),
    entryConfirmCancel: $("entry-cancel"),
    entryConfirmButton: $("entry-confirm-button"),
    entryConfirmLabel: $("entry-confirm-label"),
    fightingResult: $("fighting-result"),
    fightingMode: $("fighting-mode"),
    eventTemplate: $("event-item"),
  };

  const state = {
    filter: "all",
    review: "all",
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
    // Entry monitoring, read from the worker and never assumed: no optimistic state,
    // so a refused POST and a failed read both leave the page showing the last
    // answer the server actually gave.
    entry: {
      read: false,
      error: null,
      busy: null, // the camera or preset whose POST is in flight, or null
      epoch: 0, // moved by every write, so a read already in flight cannot land after it
      notice: "",
      mode: "home", // home | away | custom, as the worker derived it
      state: {}, // camera -> {ready, armed, armedSince}
    },
    /* The experimental fighting watch, read from its own status route and never
     * assumed: no optimistic state, so a refused reply and a sidecar that is simply
     * absent both leave the rows saying so rather than showing the last answer as
     * though it were still true. */
    fighting: {
      read: false,
      failed: false,
      status: null, // the parsed status, or null when the last read did not hold
    },
  };

  const cards = new Map();

  for (const camera of CAMERAS) {
    const root = document.querySelector('.feed[data-camera="' + camera + '"]');
    if (!root) continue;
    const card = {
      camera,
      root,
      panel: root.querySelector(".feed__panel"),
      frame: root.querySelector(".feed__frame"),
      placeholder: root.querySelector(".feed__placeholder"),
      liveText: root.querySelector(".feed__live-text"),
      liveTick: root.querySelector(".feed__live .tick"),
      detectText: root.querySelector(".feed__detect-text"),
      detectTick: root.querySelector(".feed__detect .tick"),
      stamp: root.querySelector(".feed__stamp"),
      expanded: false,
      opener: null, // what to hand focus back to when the card is minimised
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
    };
    cards.set(camera, card);

    // The panel is the card: the same element at rest and expanded, so this is
    // the only place a dialog is ever wired.
    if (card.panel) {
      const panel = card.panel;
      panel.addEventListener("close", () => {
        // A modal close is an ask to minimise. The close() on the way into
        // showModal() also lands here, but by then the dialog is open again, so
        // the two are told apart by the flag rather than by a guess.
        if (panel.open) return;
        minimizeCard(card);
      });
      // The backdrop, and the dialog's own padding, are the dialog element: there
      // is nothing behind a modal one to hit. At rest the same element is a plain
      // inline card, so it only ever closes the act of minimising a card that is
      // actually expanded.
      panel.addEventListener("click", (event) => {
        if (!card.expanded || event.target !== panel) return;
        panel.close();
      });
    }
  }

  /* The one card that is expanded. A native modal takes the top layer, so a
   * second one is never opened underneath the first. */
  let expandedCard = null;

  /* ------------------------------------------------------------ helpers -- */

  /* Native modal dialogs do not stop the page behind them scrolling, so the
   * scroll is held for exactly as long as one is open and handed straight back.
   * Counted rather than toggled, so a dialog that closes for a reason of its own
   * can never unlock a page another one is still holding. */
  let scrollLocks = 0;

  function lockScroll() {
    if (scrollLocks++ === 0) document.documentElement.style.overflow = "hidden";
  }

  function releaseScroll() {
    if (scrollLocks === 0) return;
    if (--scrollLocks === 0) document.documentElement.style.overflow = "";
  }

  const numberOrNull = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;

  const isObject = (value) =>
    Boolean(value) && typeof value === "object" && !Array.isArray(value);

  /* The one word the upstream reports for what it saw. Anything that is not a
   * plain string is not a word, so the row falls back to the same word for every
   * case rather than printing whatever the upstream happened to send. */
  const textOr = (value, fallback) => (typeof value === "string" && value ? value : fallback);

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

  const inRestrictedZone = (event) => {
    const zone = RESTRICTED_ZONE[event.camera];
    return (
      zone !== undefined &&
      event.label === PERSON_LABEL &&
      Array.isArray(event.zones) &&
      event.zones.includes(zone)
    );
  };

  /* The wider version: a person in whatever area that camera watches, which is a
   * restricted polygon on cameras 1 and 4 and the whole frame on cameras 2 and 3. The
   * row says "Kept for review" from this one, so a person kept on a house camera reads
   * as kept rather than as an old record. */
  const inWatchedZone = (event) => {
    const zone = WATCHED_ZONE[event.camera];
    return (
      zone !== undefined &&
      event.label === PERSON_LABEL &&
      Array.isArray(event.zones) &&
      event.zones.includes(zone)
    );
  };

  /* Retention, stated from what this event carries and never assumed. An ordinary
   * row expires after the six hours the camera service keeps it. A row held
   * because a person was inside that camera's watched area while its monitoring
   * was armed is held for review. So is a row held because an animal was seen on
   * a camera that watches for them, and so is one the experimental watch labelled
   * and kept: whatever put the flag there, the row is held for a person to look at
   * and must not read as an inherited record just because entry arming had nothing
   * to do with it. A person row that is held with none of those is a record from
   * before this rule, and says so rather than claiming it is a current risk: the
   * word follows the flag the service actually reports, never the zone on its own. */
  function retentionText(event) {
    if (!isKept(event)) return "6-hour retention";
    return inWatchedZone(event) || isAnimal(event) || isPossibleFighting(event)
      ? "Kept for review"
      : "Previously kept";
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
     * blank video and carries the reason - so a missing or frozen picture is
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
      card.stamp.textContent = card.live.state === "starting" ? "no frame yet" : "-";
    }
  }

  function renderCards() {
    for (const card of cards.values()) renderCard(card);
  }

  /* ------------------------------------------------------------- expand -- */

  /* The card is expanded, not moved. showModal() refuses a dialog that is still
   * open, so this one is closed and reopened as the same element: the frame, the
   * <video> and the go2rtc element inside it keep the identical parent they have
   * at rest. Nothing is cloned, re-parented or re-created, so the main stream is
   * the one session that was already open - not a second one, and not a dropped
   * one. Only the presentation and the top layer change. */
  function expandCard(card, opener) {
    if (!card.panel || card.expanded) return;
    if (expandedCard) minimizeCard(expandedCard);

    card.expanded = true;
    expandedCard = card;
    // The control the operator used, taken from the click rather than from
    // activeElement: a browser that leaves buttons unfocused on click would
    // otherwise hand focus back to the top of the document.
    card.opener = opener instanceof HTMLElement ? opener : document.activeElement;

    // A rest-state card is a group of camera facts, not a dialog. Dropping the
    // override hands the element back to the semantics its open state implies.
    card.panel.removeAttribute("role");
    card.panel.close();
    card.panel.showModal();
    lockScroll();

    const close = card.panel.querySelector('[data-feed="close"]');
    if (close) close.focus();
  }

  /* Minimising is the same element opened inline again, so the picture is
   * exactly where the operator last saw it. close() is a no-op on a dialog that
   * is already shut, which is how this serves both the operator's own close
   * control and the close event a dismiss raises. */
  function minimizeCard(card) {
    if (!card.panel || !card.expanded) return;
    card.expanded = false;
    if (expandedCard === card) expandedCard = null;
    card.panel.setAttribute("role", "group");
    card.panel.close();
    card.panel.show();
    releaseScroll();

    const opener = card.opener;
    card.opener = null;
    // The opener is usually still there, but the archive is rebuilt under the
    // operator's hands, so a detached one is passed over rather than revived.
    if (opener && opener.isConnected) opener.focus();
  }

  el.feeds.addEventListener("click", (event) => {
    const button = event.target.closest("[data-feed]");
    if (!button || !el.feeds.contains(button)) return;
    const card = cards.get(button.dataset.camera);
    if (!card) return;
    if (button.dataset.feed === "expand") expandCard(card, button);
    // The close icon and a click on the backdrop are one act, and both take the
    // same road: shut the dialog, and let its own close event put the card back
    // inline.
    else if (card.expanded) card.panel.close();
  });

  function renderStrip() {
    let playing = 0;
    let detecting = 0;
    const rates = [];

    for (const camera of CAMERAS) {
      const card = cards.get(camera);
      if (card && liveStatus(card).playing) playing += 1;
      const detector = detectorStatus(camera, state.sample);
      if (detector.detecting) detecting += 1;
      rates.push(detector.process === null ? "-" : detector.process.toFixed(1));
    }

    el.statLive.textContent = playing + " of " + CAMERAS.length + " playing";
    // Four slots, in camera order, with a dash for anything unknown, so a short
    // list can never be read as all four running.
    el.statDetectRate.textContent = rates.join(" · ");

    if (!state.sample) {
      el.statInference.textContent = "-";
      el.statAge.textContent = state.statsStale ? "out of date" : "-";
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
    el.statInference.textContent = parts.length ? parts.join(" · ") : "-";

    const seconds = state.statsAge;
    el.statAge.textContent =
      seconds === null
        ? "-"
        : seconds * 1000 > STATS_STALE_MS
          ? "out of date"
          : seconds + " s";
  }

  /* -------------------------------------------------------------- events -- */

  /* The archive is the worker's own set - a saved person snapshot on a
   * configured camera, which is exactly the set the archive-wide delete counts
   * and reaches - so it is listed as it answers and never re-filtered here.
   * A second filter in this file could only hide rows the total still counts,
   * which is how a page ends up claiming a range it is not showing. That holds
   * for the review scope as much as for the camera: the worker narrows the rows
   * before it counts and pages them, so the two always describe one set. The one
   * guard kept is the id: it is the only field that becomes a delete body. */
  function listedEvent(event) {
    return isObject(event) && typeof event.id === "string" && EVENT_ID.test(event.id);
  }

  /* The class the detector reported, in the same case as the rest of the row. It
   * is read off the event and is never guessed from the camera: camera_1 watches
   * for eleven classes, so the camera says nothing about which one was seen. */
  const classLabel = (event) => {
    const name = textOr(event.label, "");
    return name ? name.charAt(0).toUpperCase() + name.slice(1) : "";
  };

  const zoneLabel = (event) => {
    const list = Array.isArray(event.zones) ? event.zones : [];
    /* Configured zone names never reach the page: the area is stated in words, and
     * so is a restricted area. The two suffix tests cover the zones this file has
     * no entry for too - a restricted area on a camera that must not have one, and
     * the old camera_N_loitering name, which older rows in the archive still carry
     * and which no longer means anything. */
    const named = list.filter(
      (zone) =>
        zone !== REQUIRED_ZONE[event.camera] &&
        zone !== RESTRICTED_ZONE[event.camera] &&
        !String(zone).endsWith("_restricted") &&
        !String(zone).endsWith("_loitering")
    );
    const area = named.length > 0 ? named.join(" + ") : "Monitored area";
    /* An animal row has to say which animal it was. It is pinned on sight, so it
     * carries no restricted zone, and until it does the zone text is the only line
     * on the row that can name it: a held row that reads only "Monitored area"
     * says a camera saw something, not what. A person row is unchanged - "person"
     * is the only class it ever carries, so naming it would be noise. */
    return isAnimal(event) ? classLabel(event) + " · " + area : area;
  };

  /* The rows of the page on screen. Filtering is the worker's, paging is this
   * page's, and a selection outlives both: the Set below is never pruned
   * against a single page, because a page says nothing about any other one. */
  const visibleRows = () => state.rows;

  let renderedSignature = null;

  // Rows are rebuilt from the template, and a row's checkbox is remembered by
  // node rather than by a data attribute, so a tracker id is never written into
  // markup that another script on this page could read.
  const checkIds = new WeakMap();

  function renderEvents() {
    renderEventList();
    renderPager();
    renderSelection();
  }

  /* The page itself is stated once, in the pager's own status line, so the tally
   * stops repeating it here. */

  /* What the rows on screen are out of, in the words the two controls use. Both
   * filters are named whenever either one is narrowing something, so a kept-only
   * list on one camera can never read as the whole archive. */
  function scopeText() {
    const camera = state.filter === "all" ? "all cameras" : cameraLabel(state.filter);
    return state.review === REVIEW_KEPT ? camera + ", kept for review only" : camera;
  }

  function tallyText() {
    if (!state.eventsRead) {
      if (state.eventsError !== null) {
        return "Saved detections unavailable: " + state.eventsError.message;
      }
      return state.eventsLoading ? "Reading saved detections…" : "Waiting for detection data.";
    }
    const scope = scopeText();
    // Kept-only and all-detections are different empties, so the tally names which one
    // it is. With the kept scope the scope line already says so, so the subject does not
    // repeat it there.
    if (state.total === 0) {
      return (
        (state.review === REVIEW_KEPT ? "Nothing is kept yet" : "No saved detections yet") +
        " · " +
        scope
      );
    }
    // The worker clamps a page past the end, so this is a shape this page does not
    // expect rather than a state it expects: it is said plainly, never as a range.
    if (state.rows.length === 0) return "No detections on this page · " + scope;
    const from = (state.page - 1) * state.pageSize + 1;
    return (
      "Showing " +
      from +
      "–" +
      (from + state.rows.length - 1) +
      " of " +
      state.total +
      " · " +
      scope
    );
  }

  /* An empty list says which empty it is. "Nothing is kept for review yet" is a
   * different fact from "no detections at all", and reading one as the other is
   * how an operator concludes the cameras saw nothing when the answer was that
   * nothing was flagged. An empty page inside a non-empty scope is the third case
   * and gets its own words, because the archive is not empty. */
  function emptyText() {
    if (state.total !== 0) return "No detections on this page";
    if (state.review === REVIEW_KEPT) {
      return (
        "Nothing is kept for review yet" +
        (state.filter === "all" ? "" : " on " + cameraLabel(state.filter))
      );
    }
    return (
      "No saved detections yet" +
      (state.filter === "all" ? "" : " for " + cameraLabel(state.filter))
    );
  }

  /* Paging is navigation of a constant size. However long the archive grows,
   * the control is the same three steps and one read-only line: the page count
   * never becomes a row of numbered buttons, a field, or a list, and a page is
   * stepped to rather than picked from a set of thousands. */

  /* Every control is dead while a read the operator asked for is in flight or a
   * delete is: a step taken against a page nobody can see describes nothing. */
  function renderPager() {
    const busy = state.eventsLoading || state.deleting;
    // "of N" is only ever printed for a page count that was actually read, so a
    // deep link onto page 5 of an archive that has not answered yet never reads
    // as "Page 5 of 1".
    if (state.eventsRead) {
      el.pageStatus.textContent = "Page " + state.page + " of " + state.pages;
    } else if (state.eventsLoading) {
      el.pageStatus.textContent = "Loading page " + state.page + "…";
    } else {
      el.pageStatus.textContent = "Page " + state.page;
    }
    el.pagePrev.disabled = busy || state.page <= 1;
    el.pageNext.disabled = busy || state.page >= state.pages;
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
    // is not destroyed by an unrelated poll. The signature carries the scope the
    // page was built for and the facts that are printed on the row, so a change
    // of page, camera, review scope, retention or of the restricted zone is
    // always a real change.
    const signature =
      state.filter +
      ":" +
      state.review +
      "#" +
      state.page +
      "#" +
      rows
        .map(
          (row) =>
            row.id + ":" + (isKept(row) ? "1" : "0") + (inRestrictedZone(row) ? "R" : "-")
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
      img.alt = textOr(event.label, "Object") + " detected by " + label;
      img.src = href;

      node.querySelector(".event__camera").textContent = label;
      node.querySelector(".event__zone").textContent = zoneLabel(event);
      node.querySelector(".event__time").textContent = at;

      // One tag, and only ever about a restricted area: "Restricted area
      // entry" for a person whose own camera's restricted area carried them, and
      // nothing at all for every other row. A person is never tagged here for
      // duration of presence, another camera's zone is not an entry on this one,
      // and an animal is not an entry - none of which says anything about intent.
      const flag = node.querySelector(".event__flag");
      setShown(flag, inRestrictedZone(event));
      if (inRestrictedZone(event)) flag.textContent = RESTRICTED_LABEL;
      // The experimental watch's own label on this one snapshot. It is read from the
      // row's own sub-label and never inferred from the camera, the zone or the
      // armed state, and the kept word below is read separately from it: this chip
      // cannot widen what "Kept for review" means, which is still the flag alone.
      const fight = node.querySelector(".event__fight");
      const fighting = isPossibleFighting(event);
      setShown(fight, fighting);
      if (fighting) fight.textContent = FIGHT_CHIP;
      node.querySelector(".event__kept").textContent = retentionText(event);

      el.events.appendChild(node);
    }

    const empty = rows.length === 0;
    setShown(el.eventsEmpty, empty);
    setShown(el.events, !empty);
    el.eventsEmpty.textContent = emptyText();
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

  /* ----------------------------------------------------------- snapshots -- */

  /* One dialog views every snapshot, and it is handed the address off the row's
   * own anchor rather than one built here: a second construction could only ever
   * be a second answer about which file a row is showing. The href is checked
   * against this origin and the one path shape before it is read, so a row
   * cannot aim the viewer at anything this page does not already serve. */
  function snapshotURL(anchor) {
    const href = anchor.getAttribute("href");
    if (!href) return null;
    let url;
    try {
      url = new URL(href, window.location.href);
    } catch (err) {
      return null;
    }
    if (url.origin !== window.location.origin) return null;
    if (!SNAPSHOT_PATH.test(url.pathname)) return null;
    return url.href;
  }

  // Bumped on every open and on every close, so a decode that lands late paints
  // nothing over the snapshot that is on screen now.
  let shotToken = 0;
  let shotOpener = null;

  function shotReady() {
    el.shotStage.dataset.state = "ready";
    el.shotState.textContent = "";
    setShown(el.shotImage, true);
  }

  function shotFailed() {
    el.shotStage.dataset.state = "error";
    el.shotState.textContent =
      "That snapshot could not be read. It may already have been deleted, or the camera service is not answering.";
    setShown(el.shotImage, false);
  }

  function openSnapshot(anchor) {
    if (el.shotDialog.open) return false;
    const src = snapshotURL(anchor);
    if (!src) return false;

    // The row's own link, not activeElement: a browser that leaves links
    // unfocused on click would otherwise hand focus back to the document rather
    // than to the picture the operator just opened.
    shotOpener = anchor;

    // The caption says the facts that identify the row, read off that row. The
    // experimental chip is read out of the row too rather than rebuilt here, so the
    // larger picture carries exactly what the row claimed and cannot claim more.
    const row = anchor.closest(".event");
    const camera = row ? row.querySelector(".event__camera") : null;
    const time = row ? row.querySelector(".event__time") : null;
    const fight = row ? row.querySelector(".event__fight") : null;
    const label = camera ? camera.textContent : "Snapshot";
    const at = time ? time.textContent : "";
    const flagged = fight && !fight.hidden ? " · " + fight.textContent : "";
    el.shotCaption.textContent = at
      ? label + flagged + " · " + at
      : label + flagged;

    const token = ++shotToken;
    el.shotStage.dataset.state = "loading";
    el.shotState.textContent = "Loading the snapshot…";
    setShown(el.shotImage, false);
    // Assigned rather than added, so opening a second snapshot cannot leave a
    // listener behind that fires against the one after it.
    el.shotImage.onload = () => {
      if (token === shotToken) shotReady();
    };
    el.shotImage.onerror = () => {
      if (token === shotToken) shotFailed();
    };
    // The row's own thumbnail alt, which was built from this event's detected
    // label. A word typed in here would say "Person" over a dog, and the dialog is
    // the same picture at a larger size: the alt has to be the row's, not a second
    // answer about it. Nothing is claimed here when the row carried no alt.
    const thumb = row ? row.querySelector(".event__img") : null;
    el.shotImage.alt = thumb ? textOr(thumb.getAttribute("alt"), "") : "";
    el.shotImage.src = src;

    el.shotDialog.showModal();
    lockScroll();
    el.shotClose.focus();
    // A picture already in the browser's cache can be complete before the load
    // event is ever queued, and one that never arrives would leave the loading
    // line up over a picture that is there.
    if (el.shotImage.complete && el.shotImage.naturalWidth > 0) shotReady();
    return true;
  }

  /* An ordinary click reads the picture here. A modified or middle click is left
   * alone, so the anchor is still a link: the file itself opens in a tab exactly
   * as it did, and the dialog is only ever a viewer, never a gate. */
  function onRowShot(event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target.closest(".event__shot");
    if (!anchor || !el.events.contains(anchor)) return;
    if (openSnapshot(anchor)) event.preventDefault();
  }

  el.events.addEventListener("click", onRowShot);

  el.shotClose.addEventListener("click", () => {
    el.shotDialog.close();
  });

  // Escape is the browser's own. The backdrop, and the dialog's own padding,
  // are the dialog element: there is nothing behind a modal one to hit.
  el.shotDialog.addEventListener("click", (event) => {
    if (event.target === el.shotDialog) el.shotDialog.close();
  });

  el.shotDialog.addEventListener("close", () => {
    ++shotToken;
    el.shotImage.onload = null;
    el.shotImage.onerror = null;
    el.shotImage.removeAttribute("src");
    releaseScroll();
    const opener = shotOpener;
    shotOpener = null;
    // The archive is rebuilt under the operator's hands, so a row anchor that
    // has since gone is passed over rather than resurrected.
    if (opener && opener.isConnected) opener.focus();
  });

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
    lockScroll();
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
      (kept > 0 ? ", including " + kept + " kept for review" : "") +
      ". Nothing else is deleted."
    );
  }

  /* The archive-wide delete never follows the filters on screen. It is counted from
   * the worker's own whole-archive read rather than from the page being looked at,
   * so the sentence says all four cameras, every page, and explicitly that the
   * camera and review filters above do not narrow it. Without that last clause a
   * kept-only view makes an archive-wide delete read as a kept-only one, which is
   * the one scope that would be irreversible by surprise. */
  function archiveScope(summary) {
    if (summary.count === 0) {
      return "There are no saved snapshots to delete, so there is nothing to confirm.";
    }
    return (
      "All " +
      plural(summary.count, "saved snapshot") +
      " on all four cameras, including those kept for review, on every page. " +
      "This does not follow the camera or review filter on this page."
    );
  }

  /* -------------------------------------------------------------- entry -- */

  /* What a status reply has to be before this page acts on one of its booleans.
   * Every field is checked, because a reply it cannot read is a status it does not
   * have: shown as unavailable rather than filled in with a guess. armed and
   * armed_since have to agree, so an "armed with no moment" or a "disarmed with one"
   * shape is refused - except on the always-armed camera, which is exactly armed
   * with no moment of its own. The mode has to agree with the three flags it is
   * derived from, so a reply that contradicts itself is not half-believed either. */
  function usableEntry(payload) {
    if (!isObject(payload)) return null;
    if (!Array.isArray(payload.cameras)) return null;
    for (const camera of CAMERAS) {
      if (!payload.cameras.includes(camera)) return null;
    }
    if (payload.always_armed !== ALWAYS_CAMERA) return null;
    if (payload.mode !== "home" && payload.mode !== "away" && payload.mode !== "custom") return null;
    if (!isObject(payload.state)) return null;
    const state = {};
    for (const camera of CAMERAS) {
      const row = payload.state[camera];
      if (!isObject(row)) return null;
      const ready = row.ready;
      const armed = row.armed;
      if (ready !== true && ready !== false) return null;
      if (armed !== true && armed !== false) return null;
      const since = row.armed_since;
      if (camera === ALWAYS_CAMERA) {
        if (armed !== true || since !== null) return null;
        state[camera] = { ready: ready, armed: true, armedSince: null };
        continue;
      }
      if (armed) {
        if (numberOrNull(since) === null || since <= 0) return null;
      } else if (since !== null) {
        return null;
      }
      state[camera] = { ready: ready, armed: armed, armedSince: since };
    }
    const armedCount = ENTRY_CAMERAS.filter((camera) => state[camera].armed).length;
    const derived = armedCount === 0 ? "home" : armedCount === ENTRY_CAMERAS.length ? "away" : "custom";
    if (derived !== payload.mode) return null;
    return { state: state, notice: textOr(payload.notice, ""), mode: payload.mode };
  }

  function applyEntry(parsed) {
    state.entry.state = parsed.state;
    state.entry.notice = parsed.notice;
    state.entry.mode = parsed.mode;
    state.entry.read = true;
    state.entry.error = null;
  }

  async function readEntry(allowed) {
    /* A reply is only believed when it is the newest word on the subject: a read
     * that started before the newest write is dropped, and so is any read landing
     * while a write is in flight, so a slow GET can never undo an acknowledged
     * arm. allowed is only ever set by the one caller that is reconciling its own
     * write and is holding the controls shut itself.
     *
     * A read that would be discarded on return is not sent at all. While a write
     * is in flight an ordinary GET cannot change anything - its answer is thrown
     * away - so the request is not made: the service is not asked to answer a
     * question whose answer cannot be used, and the arming POST is left the only
     * thing in flight. The epoch guard stays for the read already on the wire
     * when the write starts, which nothing here can call back. */
    if (!allowed && state.entry.busy !== null) return { ok: false, stale: true };
    const epoch = state.entry.epoch;
    const outdated = () =>
      (!allowed && state.entry.busy !== null) || epoch !== state.entry.epoch;
    try {
      const parsed = usableEntry(await fetchJSON(ENTRY_PATH, ENTRY_TIMEOUT_MS));
      if (!parsed) throw new Error("unexpected response shape");
      if (outdated()) return { ok: false, stale: true };
      applyEntry(parsed);
      return { ok: true, parsed };
    } catch (err) {
      if (outdated()) return { ok: false, stale: true };
      // A status that could not be read is not a status this page keeps: the last
      // answer is dropped rather than shown as though it were current.
      state.entry.read = false;
      state.entry.error = { message: describe(err) };
      state.entry.state = {};
      return { ok: false, error: describe(err) };
    }
  }

  async function postEntry(body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ENTRY_TIMEOUT_MS);
    try {
      const res = await fetch(ENTRY_PATH, {
        method: "POST",
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-Surveillance-Action": ENTRY_ACTION,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const parsed = usableEntry(await res.json());
      if (!parsed) throw new Error("unexpected response shape");
      return parsed;
    } finally {
      clearTimeout(timer);
    }
  }

  /* One change, and never sent twice: a POST whose answer is unknown is settled by
   * reading the state back, not by asking again. The epoch moves at the start of the
   * write, so every status read already in flight is discarded rather than allowed to
   * land on top of the answer this is waiting for. */
  async function sendEntry(body) {
    /* Five things must already be true before a POST is built: the status has been
     * read (read is the flag, so a page that has never had an answer cannot write from
     * a guess), this is not the always-armed camera, the change is one the server's own
     * last answer supports, and no other write is in flight. The buttons are disabled
     * in every one of those states as well; this is the same refusal in the code path,
     * not only in the rendering. */
    if (!state.entry.read || state.entry.busy !== null) return;
    if (body.camera === ALWAYS_CAMERA) return;
    if (body.camera) {
      const known = state.entry.state[body.camera];
      if (!known) return;
      // Only turning something on needs its area confirmed. Turning it off has to work
      // without it, or a camera that stopped being confirmed could never be disarmed.
      if (body.armed && !known.ready) return;
      if (known.armed === body.armed) return;
    } else if (body.mode === "away") {
      // Away arms all three at once, so it needs all three areas confirmed; Home keeps
      // no person snapshot and needs nothing.
      if (!ENTRY_CAMERAS.every((camera) => state.entry.state[camera].ready)) return;
    } else if (body.mode !== "home") {
      return;
    }
    state.entry.epoch += 1;
    state.entry.busy = body.camera || body.mode;
    renderEntry();
    let failure = null;
    let applied = null;
    try {
      applied = await postEntry(body);
    } catch (err) {
      failure = describe(err);
      // The change may or may not have been saved. Only the service's own next answer
      // says which, so that is what this waits for. The controls stay shut for the
      // whole reconciliation: an uncertain answer must not be answered from here.
      const reread = await readEntry(true);
      if (reread.ok) applied = reread.parsed;
    }
    state.entry.busy = null;
    if (applied !== null) applyEntry(applied);
    const said = entryOutcomeText(body);
    if (failure === null) {
      showEntryOutcome("ok", said + " Snapshots already kept are untouched, and what keeps " +
        "running did not change: " + ALWAYS_CAMERA + "'s area_4, animals on Cameras 1–3, " +
        "and the possible-fighting watch on Cameras 2 and 3.");
    } else if (applied !== null) {
      showEntryOutcome(
        "warn",
        "The request did not finish (" + failure + "). The state beside the rows was read " +
          "back from the service, so it is what is actually in force."
      );
    } else {
      showEntryOutcome(
        "bad",
        "The request did not finish (" + failure + ") and the status could not be read back " +
          "either, so this page cannot say what is in force. Nothing is claimed here: check " +
          "the service before relying on either state."
      );
    }
    renderEntry();
  }

  /* What the operator asked for, in words. Named before the request is sent and used
   * only once the service has confirmed it, so it can never stand in for an answer.
   * It says what is being kept - a person inside a watched area - and never anything
   * about who, which way, or what it meant. "Person review" here always means the
   * armed person-presence rule and nothing else: the words below name what keeps
   * running, because "disarmed" is not a total stop and must never read as one. */
  function entryOutcomeText(body) {
    if (!body.camera) {
      return body.mode === "away"
        ? "Armed person review on Cameras 1–3: Camera 1 its monitored area 1, Cameras 2 and 3 anyone in view."
        : "Armed person review off on Cameras 1–3.";
    }
    const what = body.camera === "camera_1" ? "its monitored area 1" : "anyone in view";
    return cameraLabel(body.camera) +
      (body.armed ? " armed · keeps " + what : " disarmed · keeps no person snapshot");
  }

  function showEntryOutcome(tone, text) {
    el.entryResult.dataset.tone = tone;
    el.entryResult.textContent = text;
  }

  /* Each row says what it is: not yet read, not available, its area not confirmed by the
   * camera service, or armed or not with the moment it was armed. The always-armed
   * camera is a line with no button, because there is no action to offer. Nothing here
   * is optimistic: every button offers what the server's own last answer supports, and
   * Disarm stays available on an armed camera whose area cannot be confirmed, because a
   * camera that stopped reporting must never be stuck armed. */
  function renderEntry() {
    renderEntryMode();
    for (const camera of CAMERAS) {
      const row = $("entry-" + camera);
      if (!row) continue;
      const known = state.entry.state[camera];
      const busy = state.entry.busy === camera;
      const blocked = state.entry.busy !== null;
      const place = ENTRY_PLACE[camera];
      let tone = "pending";
      let text = cameraLabel(camera) + " · " + place + " · reading status";
      let action = "Arm";
      let enabled = false;
      if (!known) {
        tone = state.entry.read ? "bad" : "pending";
        text = cameraLabel(camera) + " · " + place + " · status unavailable";
      } else if (camera === ALWAYS_CAMERA) {
        tone = known.ready ? "ok" : "warn";
        text =
          cameraLabel(camera) +
          " · " +
          place +
          (known.ready ? " · always armed" : " · always armed, area not confirmed");
      } else if (known.armed) {
        tone = known.ready ? "ok" : "warn";
        text =
          cameraLabel(camera) +
          " · " +
          place +
          (known.ready
            ? " · armed since " + clockTime(known.armedSince * 1000)
            : " · armed, area not confirmed");
        action = "Disarm";
        enabled = true;
      } else {
        tone = known.ready ? "pending" : "warn";
        text =
          cameraLabel(camera) +
          " · " +
          place +
          (known.ready ? " · not armed" : " · not armed, area not confirmed");
        enabled = known.ready;
      }
      setTick(row.querySelector(".tick"), tone);
      row.querySelector("[data-entry-state]").textContent = text;
      const button = row.querySelector("[data-entry-action]");
      if (!button) continue;
      button.textContent = busy ? "Working…" : action;
      button.disabled = !enabled || blocked;
      // The state is the button's description, so its accessible name never
      // promises more than the row beside it is showing.
      button.setAttribute("aria-describedby", "entry-state-" + camera);
    }
  }

  /* The mode line: the three flags added up, and how many of them are actually taking
   * effect. Away with an area unconfirmed is not Away in force, and saying so here is
   * what keeps an armed camera from reading as keeping snapshots. */
  function renderEntryMode() {
    const node = $("entry-state-mode");
    const notice = $("entry-notice");
    if (notice) notice.textContent = state.entry.notice;
    if (!node) return;
    const label = $("entry-mode");
    if (label) setTick(label.querySelector(".tick"), modeTone());
    if (!state.entry.read) {
      node.textContent = state.entry.error
        ? "Entry monitoring · status unavailable (" + state.entry.error.message + ")"
        : "Entry monitoring · reading status";
    } else {
      const armed = ENTRY_CAMERAS.filter((camera) => state.entry.state[camera].armed);
      const unconfirmed = armed.filter((camera) => !state.entry.state[camera].ready);
      const mode = state.entry.mode.charAt(0).toUpperCase() + state.entry.mode.slice(1);
      node.textContent =
        "Entry monitoring · " + mode + " · " + armed.length + " of " + ENTRY_CAMERAS.length +
        " armed" +
        (unconfirmed.length ? " · " + unconfirmed.length + " area not confirmed" : "");
    }
    const blocked = state.entry.busy !== null;
    // read is checked first: before the first answer there is no state to ask, and a
    // status that never arrived must not be a crash on the first render.
    const allReady =
      state.entry.read && ENTRY_CAMERAS.every((camera) => state.entry.state[camera].ready);
    const home = el.entry.querySelector('[data-entry-preset="home"]');
    const away = el.entry.querySelector('[data-entry-preset="away"]');
    if (home) {
      home.textContent = state.entry.busy === "home" ? "Working…" : "Home";
      home.disabled = !state.entry.read || blocked;
    }
    if (away) {
      away.textContent = state.entry.busy === "away" ? "Working…" : "Away";
      away.disabled = !state.entry.read || blocked || !allReady;
    }
  }

  function modeTone() {
    if (!state.entry.read) return state.entry.error ? "bad" : "pending";
    const armed = ENTRY_CAMERAS.filter((camera) => state.entry.state[camera].armed);
    if (armed.some((camera) => !state.entry.state[camera].ready)) return "warn";
    return "ok";
  }

  el.entry.addEventListener("click", (event) => {
    const preset = event.target.closest("button[data-entry-preset]");
    if (preset && el.entry.contains(preset)) {
      askEntry({ mode: preset.dataset.entryPreset });
      return;
    }
    const button = event.target.closest("button[data-entry-action]");
    if (!button || !el.entry.contains(button)) return;
    const known = state.entry.state[button.dataset.entryAction];
    if (!known) return;
    askEntry({ camera: button.dataset.entryAction, armed: !known.armed });
  });

  /* The one thing this page asks before it stops keeping people. Only an acknowledged
   * Away asks, and only for a change that turns keeping people off: Home, or one camera
   * that is armed right now being disarmed. Arming a camera, a repeat of the mode
   * already in force, and a camera that is not armed all go straight out. The mode and
   * the flags read here are the acknowledged ones from the server, never a guess. */
  function entryNeedsConfirmation(body) {
    if (!state.entry.read || state.entry.busy !== null) return false;
    if (state.entry.mode !== "away") return false;
    if (!body.camera) return body.mode === "home";
    const known = state.entry.state[body.camera];
    return Boolean(known && known.armed && body.armed === false);
  }

  /* What the confirmation would send, captured whole when it opens: a poll, a filter
   * change or another click while the dialog is open cannot change it.

   * The scope sentence names exactly what stops and, just as importantly, what does
   * not. Home turns off armed person-presence review on cameras 1-3; it does not turn
   * off camera_4, the animal rule, or the experimental fighting watch, and a dialog
   * that implied otherwise would be the most expensive kind of wrong on this page. */
  let entryIntent = null;

  /* Stated once and used in both scopes, so the two can never drift apart and the
   * confirmations cannot disagree with each other. */
  const STILL_RUNNING =
    "Camera 4 stays armed, animals stay monitored on Cameras 1–3, and the " +
    "possible-fighting watch keeps running on Cameras 2 and 3.";

  function openEntryConfirm(body) {
    // Never stacked on an open one, and never re-entered from its own Confirm.
    if (entryIntent !== null || el.entryConfirm.open) return false;
    entryIntent = body;
    const whole = !body.camera;
    el.entryConfirmTitle.textContent = whole
      ? "Switch to Home?"
      : "Disarm " + cameraLabel(body.camera) + "?";
    el.entryConfirmScope.textContent = whole
      ? "Away is active. Home stops armed person-presence review on Cameras 1–3. " +
        STILL_RUNNING
      : "Away is active. Disarming " +
        cameraLabel(body.camera) +
        " (" +
        ENTRY_PLACE[body.camera] +
        ") stops that camera's armed person-presence review and the mode becomes " +
        "Custom. " +
        STILL_RUNNING;
    el.entryConfirmLabel.textContent = whole ? "Switch to Home" : "Disarm";
    el.entryConfirmButton.disabled = false;
    el.entryConfirmCancel.disabled = false;
    el.entryConfirm.showModal();
    lockScroll();
    // Cancel is the default focus, and it is one of only two ways out besides Confirm:
    // Escape and Cancel both close this dialog through the form, which sends no request
    // and leaves every setting exactly as the server last reported it.
    el.entryConfirmCancel.focus();
    return true;
  }

  /* The refusals the disabled buttons already express, in the code path: nothing is
   * built from a body this page does not recognise, from a status it has not read, or
   * while another change is in flight. */
  function askEntry(body) {
    if (!state.entry.read || state.entry.busy !== null) return;
    if (body.camera === ALWAYS_CAMERA) return;
    if (body.camera) {
      if (!ENTRY_CAMERAS.includes(body.camera)) return;
      if (body.armed !== true && body.armed !== false) return;
      const known = state.entry.state[body.camera];
      if (!known || known.armed === body.armed) return;
      // Only turning something on needs its area confirmed. Turning it off has to work
      // without it, or a camera that stopped being confirmed could never be disarmed.
      if (body.armed && !known.ready) return;
    } else if (body.mode === "away") {
      // Away arms all three at once, so it needs all three areas confirmed; Home keeps
      // no person snapshot and needs nothing.
      if (!ENTRY_CAMERAS.every((camera) => state.entry.state[camera].ready)) return;
    } else if (body.mode !== "home") {
      return;
    }
    if (entryNeedsConfirmation(body)) {
      openEntryConfirm(body);
      return;
    }
    sendEntry(body);
  }

  /* Confirm sends the captured body through the guarded path - never back through
   * askEntry, so a confirmed change cannot open another confirmation - and never
   * twice: the intent is dropped before the request is made. */
  function confirmEntry() {
    const body = entryIntent;
    if (body === null) return;
    entryIntent = null;
    el.entryConfirm.close();
    sendEntry(body);
  }

  /* Cancel, Escape and Confirm all land here, so a closed dialog never leaves an
   * intent behind for the next click to pick up. */
  function closeEntryConfirm() {
    entryIntent = null;
    releaseScroll();
  }

  el.entryConfirmButton.addEventListener("click", confirmEntry);
  el.entryConfirm.addEventListener("close", closeEntryConfirm);

  /* ------------------------------------------------------------ fighting -- */

  /* What the experimental watch is doing, read over one safe GET.

   * It is the only part of this page that is not about a camera's live picture or
   * the archive, and it is deliberately the least assertive thing here: two words
   * per camera, a tick, and one line saying why when it is not running. There is no
   * control, because there is nothing this page is allowed to do to it.
   *
   * Monitoring is earned and it is narrow. All of these must hold at once: the model
   * is ready, that camera's state is analysing, and its last capture and last score
   * are both recent by this browser's own clock. A model still loading its weights,
   * a model that refused, a camera with no frames arriving, a camera the sidecar
   * calls unavailable, and an answer that has simply stopped arriving all fail
   * closed - none of them can be talked into Monitoring, and a stale timestamp is
   * treated as no timestamp rather than as a current one.
   *
   * This must never cost anything else on the page. The four live players, the
   * archive and the arming status are the ones that matter, so this read is fired
   * and never awaited inside the cycle below: a sidecar that is loading a several
   * hundred megabyte model, slow, or absent cannot delay any of them by a cycle. */

  /* A moment the sidecar reported, or null for none. Not "recent": how old it is has
   * to be judged against this browser's clock, because a sidecar's own clock is a
   * different process's claim and not this page's evidence. */
  const isMoment = (value) =>
    value === null ||
    (typeof value === "number" && Number.isFinite(value) && value > 0);
  const asMoment = (value) => (typeof value === "number" ? value : null);

  /* A two-class score in [0, 1], or null. Checked rather than displayed: the point of
   * the check is that a number this page could not read cannot end up on a row. */
  const isScore = (value) =>
    value === null ||
    (typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1);
  const asScore = (value) => (typeof value === "number" ? value : null);

  /* What this page needs out of a status, or null.

   * Every core field is required and typed, and anything that fails is a status this
   * page does not have: shown as unavailable rather than filled in with a guess. New
   * keys are ignored, because a sidecar may carry more metadata than the page needs
   * and refusing a whole answer over an extra field would be its own kind of wrong.
   * What is not tolerated is a missing or malformed core field, a camera set that is
   * not exactly the two watched, or a state word this page does not know - an
   * unrecognised state must fail closed rather than fall through to Monitoring.
   *
   * The threshold, the window length, the frame rate, the vote count and the cooldown
   * are therefore checked and then deliberately **not** carried into anything this page
   * renders. A 0.9 printed beside a camera would read as a confidence or an accuracy,
   * and this page has neither; what the operator is shown is words, in the collapsed
   * explanation beside the rows.
   */
  function usableFighting(payload) {
    if (!isObject(payload)) return null;
    if (payload.experimental !== true) return null;
    if (payload.label !== FIGHT_SUB_LABEL) return null;
    if (!FIGHT_MODEL_STATES.includes(payload.model_state)) return null;
    const threshold = numberOrNull(payload.threshold);
    const fps = numberOrNull(payload.capture_fps);
    const cooldown = numberOrNull(payload.cooldown_seconds);
    const windowFrames = wholeCount(payload.window_frames);
    const votes = wholeCount(payload.votes_required);
    const pending = wholeCount(payload.pending);
    if (threshold === null || threshold <= 0 || threshold > 1) return null;
    if (fps === null || fps <= 0) return null;
    if (cooldown === null || cooldown < 0) return null;
    if (windowFrames === null || windowFrames < 1) return null;
    if (votes === null || votes < 1) return null;
    if (pending === null) return null;
    const cameras = payload.cameras;
    if (!isObject(cameras) || Object.keys(cameras).length !== FIGHT_CAMERAS.length) return null;
    const rows = {};
    for (const camera of FIGHT_CAMERAS) {
      const row = cameras[camera];
      if (!isObject(row)) return null;
      if (!FIGHT_CAMERA_STATES.includes(row.state)) return null;
      if (!isMoment(row.last_capture) || !isMoment(row.last_scored)) return null;
      if (!isScore(row.score)) return null;
      rows[camera] = {
        state: row.state,
        lastCapture: asMoment(row.last_capture),
        lastScored: asMoment(row.last_scored),
        // Carried, and never printed. A camera that says it is analysing has to have
        // something to have analysed: a null score here is a row claiming to be watched
        // on no evidence at all, and fightingNow below refuses it.
        score: asScore(row.score),
      };
    }
    return {
      modelState: payload.model_state,
      pending: pending,
      cameras: rows,
    };
  }

  /* The age of a moment the sidecar reported, in seconds, or null when it is not one
   * this page will act on.

   * Two failures share that null, and both have to. A moment older than the window is
   * stale, and a moment **ahead** of this browser's clock is not evidence of anything
   * having just happened either: this browser and the sidecar do not share a clock,
   * and a number that has not occurred yet must never be counted as recent. Both are
   * treated as no moment at all rather than as a current one.
   */
  function momentAge(moment) {
    if (moment === null || typeof moment !== "number" || !Number.isFinite(moment)) {
      return null;
    }
    const age = Date.now() / 1000 - moment;
    return Number.isFinite(age) && age >= 0 && age <= FIGHT_STALE_MS / 1000 ? age : null;
  }

  /* True only when this camera is demonstrably being watched right now.

   * Four things have to hold at once, and the score is one of them. A ready model, an
   * analysing camera and recent frames and a recent score all describe the same thing
   * from three sides; a **null score** describes none of it, and a row that claims to
   * be analysing while carrying no score has nothing behind the claim. So a null or
   * unreadable score is refused here even though it is a legal value to receive.
   */
  function fightingNow(row, modelState) {
    if (modelState !== "ready" || row.state !== "analyzing") return false;
    if (typeof row.score !== "number" || !Number.isFinite(row.score) ||
        row.score < 0 || row.score > 1) {
      return false;
    }
    return momentAge(row.lastCapture) !== null && momentAge(row.lastScored) !== null;
  }

  /* One camera's row: a tick and one of three words an operator can act on.

   * Monitoring is the earned one. Starting means frames are genuinely arriving and the
   * model is working through its first window - so a warm row whose frames have gone
   * stale is Unavailable, not Starting, because Starting promises something is on its
   * way and nothing says it is. Unavailable is everything else, including a model that
   * will not load: the reason belongs in the collapsed explanation, not beside a camera,
   * and no word here is a claim about anything having happened in the room. */
  function fightingCameraRow(camera) {
    const where = cameraLabel(camera) + " · " + ENTRY_PLACE[camera];
    const status = state.fighting.status;
    if (!status) {
      // Before the first answer that is Starting, and after a failed read it is
      // Unavailable: one is still worth waiting out, the other has stopped arriving.
      return state.fighting.failed
        ? { tone: "bad", text: where + " · unavailable" }
        : { tone: "pending", text: where + " · starting" };
    }
    const row = status.cameras[camera];
    if (fightingNow(row, status.modelState)) {
      return { tone: "ok", text: where + " · " + FIGHT_MONITORING };
    }
    if (status.modelState === "ready" && row.state === "warming" &&
        momentAge(row.lastCapture) !== null) {
      return { tone: "pending", text: where + " · starting" };
    }
    return { tone: "bad", text: where + " · unavailable" };
  }

  function fightingTone() {
    const status = state.fighting.status;
    if (!status) return state.fighting.failed ? "bad" : "pending";
    if (status.modelState === "refused") return "bad";
    if (status.modelState !== "ready") return "pending";
    const rows = FIGHT_CAMERAS.map((camera) => fightingCameraRow(camera));
    if (rows.some((row) => row.tone === "ok")) return "ok";
    if (rows.some((row) => row.tone === "bad")) return "bad";
    return "pending";
  }

  /* One read, settled either way and never thrown. A status this page does not have is
   * a status it does not keep: the last answer is dropped rather than left on screen,
   * so a sidecar that has just died reads as unavailable and not as the last thing it
   * said. The failure flag is set from the same answer, so a sidecar that comes back
   * stops being announced on its own.
   *
   * The renderer is guarded separately, so a surprise in the DOM cannot reject this
   * and leave the in-flight flag set for good. */
  async function readFighting() {
    let parsed = null;
    try {
      parsed = usableFighting(await fetchJSON(FIGHT_PATH, TIMEOUT_MS));
    } catch (err) {
      parsed = null;
    }
    state.fighting.status = parsed;
    state.fighting.read = parsed !== null;
    state.fighting.failed = parsed === null;
    try {
      renderFighting();
    } catch (err) {
      // Nothing on this page is worth breaking a live view over.
    }
  }

  function showFightingOutcome(tone, text) {
    if (!el.fightingResult) return;
    el.fightingResult.dataset.tone = tone;
    el.fightingResult.textContent = text;
  }

  /* One line above the two rows, and only two things on it: what the watch is for,
   * which is fixed, and how many rooms are earning the word Monitoring, which is
   * counted here from the same test the rows use. A ready model alone never counts a
   * room, so this cannot claim two while both are stale. */
  function fightingHeadline() {
    const status = state.fighting.status;
    if (!status) {
      return state.fighting.failed
        ? "Possible fighting · status unavailable"
        : "Possible fighting · starting";
    }
    const watching = FIGHT_CAMERAS.filter((camera) =>
      fightingNow(status.cameras[camera], status.modelState)
    ).length;
    return (
      "Possible fighting · continuous on Cameras 2 and 3 · " +
      watching +
      " of " +
      FIGHT_CAMERAS.length +
      " monitoring"
    );
  }

  function renderFighting() {
    const node = $("fighting-state");
    if (node) node.textContent = fightingHeadline();
    const label = $("fighting-mode");
    if (label) setTick(label.querySelector(".tick"), fightingTone());
    for (const camera of FIGHT_CAMERAS) {
      const row = $("fighting-" + camera);
      if (!row) continue;
      const view = fightingCameraRow(camera);
      setTick(row.querySelector(".tick"), view.tone);
      const text = row.querySelector("[data-fighting-state]");
      if (text) text.textContent = view.text;
    }
    // One sentence, and no more: what failed is this one read, not the cameras and not
    // the archive, and a status nobody can read is not worth a paragraph.
    showFightingOutcome(
      state.fighting.failed ? "warn" : "pending",
      state.fighting.failed
        ? "Fighting monitoring status is unavailable."
        : ""
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
    // so it can fail without anything having been sent a second time - the POST
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

  /* The camera filter, the review scope and the page number are the worker's
   * business, so changing any of them is a new read rather than a re-slice of what
   * is already on screen.
   *
   * One generation counter covers every read: a page walk is slow enough to still
   * be in flight when the operator has moved on, and a reply that arrives late is
   * dropped rather than filed under the page, camera and scope that were asked for
   * afterwards. It also means only the newest read owns the loading and error
   * state, so a superseded one cannot clear a list that is still correct.
   *
   * A transition is a move to a different page, camera or scope, not a re-read of
   * the page already on screen: the old rows leave immediately, because leaving
   * them under a new page number or a new filter is the one thing this must never
   * do.
   *
   * Reports whether the list it left behind is a list that was actually read, so
   * a caller can say so rather than assume it. A failed read is never presented as
   * a refreshed list. */
  async function readEvents(transition) {
    const generation = ++state.generation;
    const page = state.page;
    const camera = state.filter;
    const review = state.review;

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
        EVENTS_PATH +
          "?page=" +
          page +
          "&camera=" +
          encodeURIComponent(camera) +
          "&review=" +
          encodeURIComponent(review),
        ARCHIVE_READ_MS
      );
      if (generation !== state.generation) return stale;
      const parsed = usablePage(payload);
      if (!parsed) throw new Error("unexpected response shape");

      state.rows = parsed.events.filter((event) => listedEvent(event));
      /* The worker's own page number is adopted, which is how a page that has just
       * been deleted out from under the operator is clamped to the last one, and
       * how a deep link past the end of a shorter archive lands on a real page.
       * The URL is corrected to the page that was actually read - and by
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
    renderEntry();
    renderFighting();
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

  /* The experimental watch rides the same cycle and is deliberately not awaited
   * into it. One read is ever in flight, it settles its own rendering when it lands,
   * and its completion is never on the critical path of anything else: four live
   * players, the archive and the arming status do not wait on a sidecar that may be
   * loading a model, slow, or not deployed at all. */
  let fightingReading = false;

  function pollFighting() {
    if (fightingReading) return;
    fightingReading = true;
    const done = () => {
      fightingReading = false;
    };
    // Both handlers, so the in-flight flag is released however this settles. It never
    // rejects, and the second is here so that could not become a silent freeze.
    readFighting().then(done, done);
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
       * the live pictures are unaffected either way.
       *
       * The entry status rides the same loop and the same Promise.all: no second
       * timer, and a tab that is hidden stops reading both. The possible-fighting
       * status is fired beside them and is not awaited, so it cannot hold this
       * cycle open for anything else. */
      pollFighting();
      await Promise.all([
        readStats(),
        readEntry(),
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

  /* Page, camera and review scope are the three things an operator navigates by, so
   * all three live in the query string: a deep link has to survive a reload, and
   * the back button has to step back through the pages somebody actually read.
   * Nothing else is written there - the archive is read-only apart from the one
   * delete above, and a URL is not a place to keep anything else.
   *
   * This is the initial parse, and it deliberately does not clamp to the page
   * count this page starts out with: state.pages is 1 until the worker's first
   * answer lands, so clamping here would turn a deep link to page 5 into a read of
   * page 1 and the operator would never see the archive they asked for. An unusable
   * value falls back to the first page, to every camera and to every detection. */
  const PAGE_PARAM = /^[1-9][0-9]*$/;

  function readUrlState() {
    const params = new URLSearchParams(window.location.search);
    const rawPage = params.get("page");
    const rawCamera = params.get("camera");
    const rawReview = params.get("review");
    const page = Number(rawPage);
    return {
      page: rawPage !== null && PAGE_PARAM.test(rawPage) && Number.isSafeInteger(page) ? page : 1,
      filter: knownCamera(rawCamera) ? rawCamera : "all",
      review: knownReview(rawReview) ? rawReview : "all",
    };
  }

  /* Canonical: the page is written only when it is not the first, and the camera
   * and the review scope only when they are not all, so the common case is a bare
   * path and no two navigations ever produce two spellings of the same view. Every
   * other parameter and the fragment are left exactly as they were found - this
   * page did not put them there and does not get to clean them up. A push is a move
   * the operator asked for; a replace is the same view being described more
   * accurately. */
  function writeUrlState(push) {
    const url = new URL(window.location.href);
    if (state.page > 1) url.searchParams.set("page", String(state.page));
    else url.searchParams.delete("page");
    if (state.filter === "all") url.searchParams.delete("camera");
    else url.searchParams.set("camera", state.filter);
    if (state.review === "all") url.searchParams.delete("review");
    else url.searchParams.set("review", state.review);
    const entry = url.pathname + url.search + url.hash;
    if (push) window.history.pushState(null, "", entry);
    else window.history.replaceState(null, "", entry);
  }

  /* The pressed state of the camera tabs follows state.filter instead of being
   * written at the click, so a view restored from a link or from the back button
   * shows the filter it is rather than leaving the old tab lit. The review scope
   * is marked the same way, for the same reason. */
  function markFilter(camera) {
    for (const tab of el.filters.querySelectorAll("button[data-camera]")) {
      const on = tab.dataset.camera === camera;
      tab.classList.toggle("is-on", on);
      tab.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function markReview(review) {
    for (const tab of el.reviewFilters.querySelectorAll("button[data-review]")) {
      const on = tab.dataset.review === review;
      tab.classList.toggle("is-on", on);
      tab.setAttribute("aria-pressed", on ? "true" : "false");
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

  /* The review scope is a move exactly like a camera filter: a different set of
   * rows with its own paging, so it returns to page 1, and it leaves the selection
   * alone for the same reason. A tick made while reading kept rows is still a tick
   * after going back to every detection, and vice versa. */
  el.reviewFilters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-review]");
    if (!button) return;
    const chosen = button.dataset.review;
    if (!knownReview(chosen)) return;
    // Already the scope on screen, already on its first page: nothing to change, and
    // nothing to add to the stack Back walks.
    if (chosen === state.review && state.page === 1) return;
    state.review = chosen;
    state.page = 1;
    writeUrlState(true);
    markReview(chosen);
    readEvents(true);
  });

  /* Three steps, each a move to a page that exists. goToPage() refuses the move
   * that changes nothing and clamps to the page count, so no step can name a
   * page the archive does not have. */
  el.pagePrev.addEventListener("click", () => {
    goToPage(state.page - 1);
  });

  el.pageNext.addEventListener("click", () => {
    goToPage(state.page + 1);
  });

  // The newest page, asked for rather than waited for.
  el.pageLatest.addEventListener("click", () => {
    goToPage(1);
  });

  /* Back and Forward are navigation, not a poll: they restore the page, the
   * camera and the review scope the URL names and read that page. Nothing is
   * pushed - the entry being restored is already on the stack - and a read that was
   * already in flight is dropped by the generation counter in readEvents rather
   * than answering a page the operator has already walked away from.
   *
   * While a delete is in flight the restore is still applied to state, so the URL
   * and the page on screen agree, but its read is left to the re-read that follows
   * the POST: the captured body cannot be widened by any of this, and one read at
   * a time is the rule the whole file follows. */
  window.addEventListener("popstate", () => {
    const next = readUrlState();
    state.page = next.page;
    state.filter = next.filter;
    state.review = next.review;
    markFilter(next.filter);
    markReview(next.review);
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

  // Every way out of the confirmation reaches this: the cancel button, Escape,
  // or the delete's own close() once the answer has landed.
  el.dialog.addEventListener("close", () => {
    releaseScroll();
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

  /* The page, the camera and the review scope are taken from the URL before
   * anything is drawn and before the first archive read, so a reload straight onto
   * a deep link arrives on the page it names rather than on page 1 of every camera
   * and every detection. */
  const initial = readUrlState();
  state.page = initial.page;
  state.filter = initial.filter;
  state.review = initial.review;
  markFilter(state.filter);
  markReview(state.review);
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