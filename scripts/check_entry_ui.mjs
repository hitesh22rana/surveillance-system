#!/usr/bin/env node
/* Behavioural check for the entry confirmation and the fighting status in
 * dashboard/app.js (no dependencies).
 *
 * Two browser decisions are checked here, both by driving the page's own functions
 * rather than a copy of them: they are lifted out of app.js by name, evaluated with
 * the page's state replaced by mocks, and asked the questions that matter.
 *
 * The confirmation is the only thing between an acknowledged Away and an unreviewed
 * disarm: a native <dialog>, the form that closes it, and one guarded request. So it
 * is asked which changes ask, that Cancel and Escape write nothing, that Confirm
 * writes exactly once, and that an unreadable status, a busy page or the always-armed
 * camera produce neither a dialog nor a request. Both its scopes must also say that
 * only armed person-presence review stops, and name what carries on.
 *
 * The fighting status is one read and three words per room. What matters is that
 * Monitoring is only ever the narrow set of facts that earn it, so this drives it
 * with a controlled status and a controlled clock: a ready model with fresh data
 * monitors, and a future moment, a stale moment, a loading or refused model, a warm
 * row whose frames stopped, and a malformed reply all read as Starting or
 * Unavailable instead. It also drives the real read, so a failure drops the last
 * answer and a recovery clears it.
 *
 * The mocks are the dialog, the dispatcher, the clock and the network, nothing else:
 * there is no fake of the logic under test, no browser automation, nothing exported
 * from app.js for this, and nothing added to app.js to make it possible. Where a
 * native dialog would fire `cancel` then `close`, this calls the same cleanup the
 * page's own `close` listener calls.
 *
 *     node scripts/check_entry_ui.mjs
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = readFileSync(join(HERE, "..", "dashboard", "app.js"), "utf8");

const ENTRY_CAMERAS = ["camera_1", "camera_2", "camera_3"];
const ALWAYS_CAMERA = "camera_4";
// The page's own place names and label helper, read out of the file rather than
// restated: the dialog copy names these rooms, and a check agreeing with a copy of
// itself would prove nothing.
const ENTRY_PLACE = Object.fromEntries(
  [...APP.slice(APP.indexOf("const ENTRY_PLACE")).split("\n  });")[0]
    .matchAll(/(camera_\d): "([^"]+)"/g)]
    .map(([, camera, place]) => [camera, place])
);
const cameraLabel = (camera) => "Camera " + String(camera).slice(-1);

/* One top-level function, from its declaration to the brace that ends it. A nested
 * block closes deeper, so the first two-space brace after it is the function's own,
 * and the slice keeps that brace: without it the text is not a function at all. A
 * leading `async` is kept too, because a function that awaits inside it will not
 * parse without it. */
function lift(name) {
  const at = APP.indexOf("function " + name + "(");
  assert.notEqual(at, -1, `${name} is not in dashboard/app.js`);
  const lineStart = APP.lastIndexOf("\n", at) + 1;
  const start = APP.slice(lineStart, at).trim() === "async" ? lineStart : at;
  const end = APP.indexOf("\n  }\n", start);
  assert.notEqual(end, -1, `${name} has no closing brace at the top level`);
  return APP.slice(start, end + 4);
}

/* The one standing sentence about what keeps running, lifted whole out of app.js
 * rather than restated here. Both confirmation scopes use it, and the point of
 * checking those scopes is that neither of them reads as a total stop, so a copy
 * written here would agree with itself and prove nothing. */
function declaration(name) {
  const start = APP.indexOf("const " + name);
  assert.notEqual(start, -1, `${name} is not in dashboard/app.js`);
  const end = APP.indexOf(";\n", start);
  assert.notEqual(end, -1, `${name} has no terminating semicolon`);
  return APP.slice(start, end + 2);
}

const LIFTED = [declaration("STILL_RUNNING"), ["entryNeedsConfirmation", "openEntryConfirm",
  "closeEntryConfirm", "askEntry", "confirmEntry"].map(lift).join("\n\n")].join("\n\n");

/* The fighting status, lifted the same way. The constants and the small predicates
 * come along because the functions call them, and the clock is a parameter rather
 * than anything in app.js: `Date` is shadowed inside the evaluated body, which is how
 * a stale moment and a future moment can both be asked about without waiting ten
 * seconds or editing production code to make the question answerable.
 *
 * The text is assembled in fightingHarness below rather than here, because one part of
 * it has to be evaluated by the async constructor and the rest not. */

/* Any fixed past second. The fake clock is set to exactly this, so "now" is a number
 * this check controls rather than one it has to wait for. */
const NOW = 1700000000;

/* A status the page would accept, with every field the cases below move. Written out
 * from the documented contract rather than from any helper the page uses, so a check
 * that agreed with the page's own builder would prove nothing. The default is both
 * rooms ready, fresh and analysing, which is the only shape that may earn Monitoring. */
function room(over) {
  return Object.assign({
    state: "analyzing",
    last_capture: NOW,
    last_scored: NOW,
    score: 0.9,
    last_candidate: null,
    last_matched: null,
  }, over);
}

function status(modelState, room2, room3, over) {
  return Object.assign({
    experimental: true,
    label: "possible_fighting",
    model_state: modelState,
    threshold: 0.9,
    window_frames: 16,
    capture_fps: 3,
    votes_required: 2,
    cooldown_seconds: 60,
    pending: 0,
    cameras: { camera_2: room(room2), camera_3: room(room3) },
  }, over);
}

/* The fighting functions, evaluated over a state and a clock this check owns. `reply`
 * is what the read will be given next, and each call to `read()` consumes it, so a
 * failure and then a recovery can be driven in one run. The body has to be built by
 * the async constructor rather than by `new Function`, because the page's read is an
 * async function and this evaluates the real one rather than a restatement of it. */
function fightingHarness() {
  const state = { fighting: { read: false, failed: false, status: null } };
  const replies = [];
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  // Two builders over the same lifted text: the page's read is an async function, so it
  // has to be evaluated as one, while the predicates around it are plain functions and
  // are nicer to assert on directly. `usableFighting` appears in both because the read
  // calls it.
  const pureNames = ["usableFighting", "momentAge", "fightingNow", "fightingCameraRow",
    "fightingTone", "fightingHeadline"];
  const pureText = ["isObject", "numberOrNull", "wholeCount", "isMoment", "asMoment",
    "asScore", "isScore", "FIGHT_SUB_LABEL", "FIGHT_CAMERAS", "FIGHT_MONITORING",
    "FIGHT_MODEL_STATES", "FIGHT_CAMERA_STATES", "FIGHT_STALE_MS"]
    .map(declaration).join("\n\n");
  const fakeDate = { now: () => NOW * 1000 };
  const pure = new Function(
    "state", "ENTRY_PLACE", "cameraLabel", "Date",
    pureText + "\n" + pureNames.map(lift).join("\n\n") +
      "\nreturn { " + pureNames.join(", ") + " };",
  )(state, ENTRY_PLACE, cameraLabel, fakeDate);
  const fetchJSON = () => {
    const next = replies.length ? replies.shift() : Promise.reject(new Error("nothing queued"));
    return typeof next === "function" ? next() : next;
  };
  const readFactory = new AsyncFunction(
    "state", "ENTRY_PLACE", "cameraLabel", "Date", "fetchJSON", "FIGHT_PATH", "TIMEOUT_MS",
    "renderFighting",
    pureText + "\n" + lift("usableFighting") + "\n" + lift("readFighting") +
      "\nreturn readFighting();",
  );
  return {
    ...pure,
    state,
    reply: (value) => replies.push(value),
    // The page's own read, with only the network and the renderer replaced. The factory
    // invokes it, so this is the page's read rather than a promise of one.
    read: () => readFactory(state, ENTRY_PLACE, cameraLabel, fakeDate, fetchJSON,
      "/api/activity/fighting", 4000, () => {}),
    // The last word a row prints, which is the only part an operator acts on.
    word: (camera) => pure.fightingCameraRow(camera).text.split(" · ").pop(),
    tone: (camera) => pure.fightingCameraRow(camera).tone,
    headline: () => pure.fightingHeadline(),
    tone3: () => pure.fightingTone(),
  };
}

/* The mocks: a status, a dialog whose text is read straight off the nodes the page
 * writes to, a dispatcher recording every request, and the scroll pair the page keeps
 * as one. */
function harness() {
  const node = () => ({ textContent: "", disabled: false, focus() {} });
  const dialog = node();
  dialog.open = false;
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; built.closeEntryConfirm(); };
  const el = {
    entryConfirm: dialog,
    entryConfirmTitle: node(),
    entryConfirmScope: node(),
    entryConfirmLabel: node(),
    entryConfirmButton: node(),
    entryConfirmCancel: node(),
  };
  const state = {
    entry: {
      read: true,
      busy: null,
      mode: "home",
      state: Object.fromEntries(
        ENTRY_CAMERAS.map((camera) => [camera, { ready: true, armed: false, armedSince: null }])
      ),
    },
  };
  const sent = [];
  const locks = [];
  const build = new Function(
    "state", "el", "ENTRY_CAMERAS", "ENTRY_PLACE", "ALWAYS_CAMERA", "cameraLabel",
    "lockScroll", "releaseScroll", "sendEntry",
    "let entryIntent = null;\n" + LIFTED + "\nreturn {" +
    "entryNeedsConfirmation, openEntryConfirm, closeEntryConfirm, askEntry, confirmEntry };"
  );
  const built = build(state, el, ENTRY_CAMERAS, ENTRY_PLACE, ALWAYS_CAMERA, cameraLabel,
    () => locks.push("lock"), () => locks.push("unlock"), (body) => sent.push(body));
  const arm = (mode, armed) => {
    state.entry.mode = mode;
    for (const camera of ENTRY_CAMERAS) {
      state.entry.state[camera].armed = armed;
      state.entry.state[camera].armedSince = armed ? 1790910000.0 : null;
    }
  };
  return {
    ...built,
    state,
    dialog,
    sent,
    locks,
    arm,
    title: () => el.entryConfirmTitle.textContent,
    label: () => el.entryConfirmLabel.textContent,
    scope: () => el.entryConfirmScope.textContent,
  };
}

// Home: arming one camera asks nothing and goes straight out.
{
  const ui = harness();
  ui.arm("home", false);
  assert.equal(ui.entryNeedsConfirmation({ camera: "camera_2", armed: true }), false);
  ui.askEntry({ camera: "camera_2", armed: true });
  assert.equal(ui.dialog.open, false, "arming asked for confirmation");
  assert.deepEqual(ui.sent, [{ camera: "camera_2", armed: true }], "arming sent something else");
  ui.state.entry.state.camera_2.armed = true;
  ui.askEntry({ camera: "camera_2", armed: true });
  assert.equal(ui.sent.length, 1, "a repeated arm was sent again");
  ui.askEntry({ camera: "camera_1", armed: false });
  assert.equal(ui.sent.length, 1, "a disarm of a camera that was not armed was sent");
}

// Away: a repeated Away never asks, and it reaches the dispatcher.
{
  const ui = harness();
  ui.arm("away", true);
  ui.askEntry({ mode: "away" });
  assert.equal(ui.dialog.open, false, "a repeated Away asked for confirmation");
  ui.askEntry({ mode: "away" });
  assert.equal(ui.dialog.open, false, "a second repeated Away asked for confirmation");
  assert.equal(ui.sent.length, 2, "a repeated Away did not go straight out");
  assert.equal(ui.entryNeedsConfirmation({ mode: "away" }), false);
  assert.equal(ui.entryNeedsConfirmation({ mode: "home" }), true);
}

// Home from Away: asks, says what it does, and Cancel or Escape writes nothing.
{
  const ui = harness();
  ui.arm("away", true);
  ui.askEntry({ mode: "home" });
  assert.equal(ui.dialog.open, true, "Home from Away did not ask");
  assert.deepEqual(ui.sent, [], "asking for confirmation already sent something");
  assert.match(ui.title(), /Switch to Home\?/, "the title is not about Home");
  assert.match(ui.label(), /Switch to Home/, "the button is not about Home");
  const scope = ui.scope();
  assert.match(scope, /Away is active/, "the dialog does not say Away is active: " + scope);
  assert.match(scope, /stops armed person-presence review on Cameras 1/, "the dialog does not say exactly what Home stops: " + scope);
  assert.doesNotMatch(scope, /stops person review/, "the dialog claims all person review stops: " + scope);
  assert.match(scope, /Camera 4 stays armed/, "the dialog does not keep camera_4 in view: " + scope);
  assert.match(scope, /animals stay monitored on Cameras 1/, "the dialog does not say the animal rule continues: " + scope);
  assert.match(scope, /possible-fighting watch keeps running on Cameras 2 and 3/, "the dialog does not say the fighting watch continues: " + scope);
  assert.doesNotMatch(scope, /stay armed/, "the dialog claims the other two stay armed: " + scope);
  assert.deepEqual(ui.locks, ["lock"], "the dialog did not hold the scroll");
  // Escape and Cancel are both the dialog closing, and nothing else: a native dialog
  // fires `close` for either, which is the cleanup the page registered.
  ui.dialog.close();
  assert.deepEqual(ui.sent, [], "Escape sent a request");
  ui.askEntry({ mode: "home" });
  assert.equal(ui.dialog.open, true, "the closed dialog left an intent behind");
  ui.dialog.close();
  assert.deepEqual(ui.locks, ["lock", "unlock", "lock", "unlock"], "the scroll was not held and handed back");
  assert.deepEqual(ui.sent, [], "Cancel sent a request");
}

// One camera from Away: asks, names the room, and Confirm sends exactly one request.
{
  const ui = harness();
  ui.arm("away", true);
  ui.askEntry({ camera: "camera_2", armed: false });
  assert.equal(ui.dialog.open, true, "a disarm from Away did not ask");
  assert.match(ui.title(), /Disarm Camera 2\?/, "the title does not name the camera");
  assert.match(ui.label(), /Disarm/, "the button is not the action");
  const scope = ui.scope();
  assert.match(scope, new RegExp(ENTRY_PLACE.camera_2), "the dialog does not name the room: " + scope);
  assert.match(scope, /stops that camera's armed person-presence review/, "the dialog does not say exactly what this disarm stops: " + scope);
  assert.doesNotMatch(scope, /stops person review/, "the dialog claims all person review stops: " + scope);
  assert.match(scope, /becomes Custom/, "the dialog does not say the mode changes: " + scope);
  assert.match(scope, /Camera 4 stays armed/, "the dialog does not keep camera_4 in view: " + scope);
  assert.match(scope, /animals stay monitored on Cameras 1/, "the dialog does not say the animal rule continues: " + scope);
  assert.match(scope, /possible-fighting watch keeps running on Cameras 2 and 3/, "the dialog does not say the fighting watch continues: " + scope);
  assert.deepEqual(ui.sent, [], "asking already sent something");
  ui.confirmEntry();
  assert.deepEqual(ui.sent, [{ camera: "camera_2", armed: false }], "Confirm sent something else");
  assert.equal(ui.dialog.open, false, "Confirm left the dialog open");
  ui.confirmEntry();
  assert.equal(ui.sent.length, 1, "Confirm sent twice");
  const other = harness();
  other.arm("away", true);
  other.askEntry({ camera: "camera_3", armed: false });
  assert.match(other.scope(), new RegExp(ENTRY_PLACE.camera_3), "camera_3 names no room");
  assert.notEqual(ENTRY_PLACE.camera_2, ENTRY_PLACE.camera_3, "both house cameras share a room");
}

// Nothing is built from an unreadable status, a busy page, or the always-armed camera.
{
  const bodies = [{ mode: "home" }, { camera: "camera_2", armed: false },
                  { camera: "camera_2", armed: true }, { mode: "away" }];
  for (const body of bodies) {
    const unread = harness();
    unread.arm("away", true);
    unread.state.entry.read = false;
    unread.askEntry(body);
    assert.equal(unread.dialog.open, false, `an unread status asked for ${JSON.stringify(body)}`);
    assert.deepEqual(unread.sent, [], `an unread status sent for ${JSON.stringify(body)}`);

    const busy = harness();
    busy.arm("away", true);
    busy.state.entry.busy = "camera_1";
    busy.askEntry(body);
    assert.equal(busy.dialog.open, false, `a busy page asked for ${JSON.stringify(body)}`);
    assert.deepEqual(busy.sent, [], `a busy page sent for ${JSON.stringify(body)}`);
  }
  for (const armed of [true, false]) {
    const four = harness();
    four.arm("away", true);
    four.askEntry({ camera: ALWAYS_CAMERA, armed });
    assert.equal(four.dialog.open, false, `camera_4 armed=${armed} asked for confirmation`);
    assert.deepEqual(four.sent, [], `camera_4 armed=${armed} reached the dispatcher`);
  }
  const odd = harness();
  odd.arm("away", true);
  for (const body of [{ mode: "night" }, { camera: "camera_9", armed: false }, {},
                      { camera: "camera_1", armed: "true" }]) {
    odd.askEntry(body);
    assert.deepEqual(odd.sent, [], `an unrecognised body sent: ${JSON.stringify(body)}`);
    assert.equal(odd.dialog.open, false, `an unrecognised body asked: ${JSON.stringify(body)}`);
  }
  const blind = harness();
  blind.arm("custom", false);
  blind.state.entry.state.camera_3.ready = false;
  blind.askEntry({ mode: "away" });
  assert.deepEqual(blind.sent, [], "Away was sent with an area unconfirmed");
  blind.askEntry({ camera: "camera_3", armed: false });
  assert.deepEqual(blind.sent, [], "a disarm of a camera that was never armed was sent");
}

// The fighting status: Monitoring is earned, and this is the whole property. Two fresh
// ready rooms are the only shape that earns it.
{
  const ui = fightingHarness();
  assert.equal(ui.usableFighting(status("ready")) !== null, true,
    "the ready fresh status was refused outright");
  ui.reply(Promise.resolve(status("ready")));
  await ui.read();
  assert.equal(ui.state.fighting.failed, false, "a good reply was recorded as a failure");
  assert.equal(ui.state.fighting.read, true, "a good reply did not read");
  for (const camera of ["camera_2", "camera_3"]) {
    assert.equal(ui.word(camera).toLowerCase(), "monitoring", `${camera} did not monitor`);
    assert.equal(ui.tone(camera), "ok", `${camera} did not read as healthy`);
  }
  assert.equal(ui.tone3(), "ok", "two monitoring rooms did not read as healthy together");
  assert.match(ui.headline(), /2 of 2 monitoring/, "the headline did not count two rooms");
}

// A moment in the future is not evidence of anything having just happened, and a stale
// one is not either. Neither may read as Monitoring.
{
  for (const [label, over] of [
    ["a score from the future", { last_scored: NOW + 60 }],
    ["a capture from the future", { last_capture: NOW + 60 }],
    ["a score long past", { last_scored: NOW - 3600 }],
    ["a capture long past", { last_capture: NOW - 3600 }],
    ["no capture at all", { last_capture: null }],
    ["no score at all", { last_scored: null }],
    ["no score to go on", { score: null }],
  ]) {
    const ui = fightingHarness();
    ui.reply(Promise.resolve(status("ready", over)));
    await ui.read();
    assert.equal(ui.state.fighting.failed, false, `${label} was recorded as a failed read`);
    assert.notEqual(ui.word("camera_2").toLowerCase(), "monitoring", `${label} read as monitoring`);
    assert.equal(ui.word("camera_2").toLowerCase(), "unavailable", `${label} did not read unavailable`);
    // Only camera_2 is spoiled in these cases, so the headline must count the one good
    // room and never round a bad room up into two.
    assert.match(ui.headline(), /1 of 2 monitoring/, `${label} was still counted as monitoring`);
  }
  // Both rooms spoiled: nothing is being watched, and the headline says so.
  const both = fightingHarness();
  both.reply(Promise.resolve(status("ready", { last_scored: NOW - 3600 },
    { last_scored: NOW - 3600 })));
  await both.read();
  assert.match(both.headline(), /0 of 2 monitoring/, "two stale rooms were counted as watching");
  assert.equal(both.tone3(), "bad", "two stale rooms read as healthy together");
}

// A model that is loading or refused, and a camera that is simply not available.
{
  for (const modelState of ["loading", "refused"]) {
    const ui = fightingHarness();
    ui.reply(Promise.resolve(status(modelState)));
    await ui.read();
    assert.equal(ui.state.fighting.failed, false, `a ${modelState} model was recorded as a failed read`);
    for (const camera of ["camera_2", "camera_3"]) {
      assert.notEqual(ui.word(camera).toLowerCase(), "monitoring",
        `a ${modelState} model read as monitoring`);
    }
    assert.match(ui.headline(), /0 of 2 monitoring/, `a ${modelState} model was counted`);
  }
  const off = fightingHarness();
  off.reply(Promise.resolve(status("ready", { state: "unavailable" })));
  await off.read();
  assert.equal(off.word("camera_2").toLowerCase(), "unavailable", "an unavailable room read otherwise");
  assert.equal(off.word("camera_3").toLowerCase(), "monitoring", "the other room did not monitor");
  assert.equal(off.tone3(), "ok", "one good room and one bad one did not read as healthy together");
}

// Starting is only Starting while frames are genuinely arriving: a warm row whose
// capture has gone stale has stopped reporting, and must not promise otherwise.
{
  const fresh = fightingHarness();
  fresh.reply(Promise.resolve(status("ready", { state: "warming" })));
  await fresh.read();
  assert.equal(fresh.word("camera_2").toLowerCase(), "starting", "a warm room with frames is not starting");

  const stalled = fightingHarness();
  stalled.reply(Promise.resolve(status("ready", { state: "warming", last_capture: NOW - 3600 })));
  await stalled.read();
  assert.equal(stalled.word("camera_2").toLowerCase(), "unavailable",
    "a warm room with stale frames stayed Starting for ever");
  assert.match(stalled.headline(), /1 of 2 monitoring/,
    "a stalled warm room was counted as watching");
}

// Every shape the page cannot understand is refused before a word is printed, and one
// unreachable room is never rounded up into two.
{
  const bad = [
    ["not an object", null],
    ["an array", []],
    ["not experimental", status("ready", {}, {}, { experimental: false })],
    ["a string experimental", status("ready", {}, {}, { experimental: "true" })],
    ["the wrong label", status("ready", {}, {}, { label: "fighting" })],
    ["an unknown model state", status("watching")],
    ["a threshold of zero", status("ready", {}, {}, { threshold: 0 })],
    ["a threshold above one", status("ready", {}, {}, { threshold: 1.5 })],
    ["a string threshold", status("ready", {}, {}, { threshold: "0.9" })],
    ["no window length", status("ready", {}, {}, { window_frames: null })],
    ["no votes required", status("ready", {}, {}, { votes_required: 0 })],
    ["a negative pending count", status("ready", {}, {}, { pending: -1 })],
    ["a missing pending count", status("ready", {}, {}, { pending: undefined })],
    ["an unknown camera state", status("ready", { state: "watching" })],
    ["a camera state word this page invented", status("ready", { state: "Monitoring" })],
    ["a string timestamp", status("ready", { last_scored: String(NOW) })],
    ["a score above one", status("ready", { score: 1.4 })],
    ["a negative score", status("ready", { score: -0.1 })],
    ["three cameras", status("ready", {}, {}, { cameras: { camera_2: room(), camera_3: room(), camera_4: room() } })],
    ["one camera", status("ready", {}, {}, { cameras: { camera_2: room() } })],
    ["a foreign camera", status("ready", {}, {}, { cameras: { camera_2: room(), camera_9: room() } })],
  ];
  for (const [label, payload] of bad) {
    const ui = fightingHarness();
    ui.reply(Promise.resolve(payload));
    await ui.read();
    assert.equal(ui.state.fighting.status, null, `${label} was believed`);
    assert.equal(ui.state.fighting.failed, true, `${label} was not recorded as a failed read`);
    assert.equal(ui.state.fighting.read, false, `${label} was recorded as read`);
    assert.equal(ui.tone3(), "bad", `${label} did not read as unavailable`);
  }
  // Extra metadata is not a reason to refuse a whole answer: a sidecar may carry more
  // than this page needs, and dropping it would be its own kind of wrong.
  const extra = fightingHarness();
  extra.reply(Promise.resolve(status("ready", {}, {}, {
    model: "some/model", model_revision: "0".repeat(40), notice: "a notice",
    a_field_from_the_future: { anything: [1, 2, 3] },
  })));
  await extra.read();
  assert.equal(extra.state.fighting.failed, false, "extra metadata was refused");
  assert.equal(extra.word("camera_3").toLowerCase(), "monitoring", "extra metadata stopped a room monitoring");
}

// The read itself: a failure drops the last answer rather than leaving it on screen,
// and a recovery clears the failure without a reload.
{
  const ui = fightingHarness();
  ui.reply(Promise.resolve(status("ready")));
  await ui.read();
  assert.equal(ui.word("camera_2").toLowerCase(), "monitoring", "the first read did not monitor");

  ui.reply(Promise.reject(new Error("HTTP 502")));
  await ui.read();
  assert.equal(ui.state.fighting.status, null, "a failed read kept the last answer");
  assert.equal(ui.state.fighting.failed, true, "a failed read did not record the failure");
  assert.equal(ui.word("camera_2").toLowerCase(), "unavailable", "a failed read still read monitoring");
  assert.match(ui.headline(), /status unavailable/, "a failed read did not say so");

  ui.reply(Promise.resolve(status("ready")));
  await ui.read();
  assert.equal(ui.state.fighting.failed, false, "recovery did not clear the failure");
  assert.equal(ui.state.fighting.read, true, "recovery did not read");
  assert.equal(ui.word("camera_2").toLowerCase(), "monitoring", "recovery did not resume monitoring");

  // A malformed reply after a good one is a failure in the same sense as no reply.
  ui.reply(Promise.resolve(status("ready", { score: 4 })));
  await ui.read();
  assert.equal(ui.state.fighting.failed, true, "a malformed reply was not a failure");
  assert.equal(ui.state.fighting.status, null, "a malformed reply kept the last answer");
}

console.log("ok  arming from Home goes straight out; a repeated arm, Away or unconfirmed area never asks");
console.log("ok  Home and a single disarm from Away ask, saying only armed person-presence review stops, and naming Camera 4, the animal rule and the fighting watch as continuing");
console.log("ok  Cancel and Escape send nothing and leave the dialog able to ask again");
console.log("ok  Confirm sends the captured request exactly once, names the room, and closes");
console.log("ok  an unread status, a busy page, an unrecognised body and camera_4 produce no dialog and no request");
console.log("ok  two fresh ready rooms monitor, and the headline counts the rooms that earned it");
console.log("ok  a future or stale moment, no moment, a loading or refused model and an unavailable room all read unavailable, never monitoring");
console.log("ok  a warm room is starting only while its frames are arriving, and a stalled warm room reads unavailable");
console.log("ok  21 malformed statuses are refused before a word is printed, while extra metadata is not");
console.log("ok  a failed read drops the last answer and a recovery clears the failure");
console.log("\nPASS  every assertion held");