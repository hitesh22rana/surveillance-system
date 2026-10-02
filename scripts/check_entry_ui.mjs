#!/usr/bin/env node
/* Behavioural check for the entry confirmation in dashboard/app.js (no dependencies).

The confirmation is the only thing between an acknowledged Away and an unreviewed
disarm, and it is browser code: a native <dialog>, the form that closes it, and one
guarded request. So this drives the page's own functions rather than a copy of them -
they are lifted out of app.js by name, evaluated with the page's state replaced by
mocks, and asked the questions that matter: which changes ask, that Cancel and Escape
write nothing, that Confirm writes exactly once, and that an unreadable status, a busy
page or the always-armed camera produce neither a dialog nor a request.

The mocks are the dialog and the dispatcher, nothing else: there is no fake of the
logic under test, no browser automation, and nothing added to app.js to make this
possible. Where a native dialog would fire `cancel` then `close`, this calls the same
cleanup the page's own `close` listener calls.

    node scripts/check_entry_ui.mjs
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
 * and the slice keeps that brace: without it the text is not a function at all. */
function lift(name) {
  const start = APP.indexOf("function " + name + "(");
  assert.notEqual(start, -1, `${name} is not in dashboard/app.js`);
  const end = APP.indexOf("\n  }\n", start);
  assert.notEqual(end, -1, `${name} has no closing brace at the top level`);
  return APP.slice(start, end + 4);
}

const LIFTED = ["entryNeedsConfirmation", "openEntryConfirm", "closeEntryConfirm", "askEntry",
  "confirmEntry"].map(lift).join("\n\n");

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
  assert.match(scope, /stops person review on Cameras 1/, "the dialog does not say what Home stops: " + scope);
  assert.match(scope, /Camera 4 remains armed/, "the dialog does not keep camera_4 in view: " + scope);
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
  assert.match(scope, /becomes Custom/, "the dialog does not say the mode changes: " + scope);
  assert.match(scope, /Camera 4 remains armed/, "the dialog does not keep camera_4 in view: " + scope);
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

console.log("ok  arming from Home goes straight out; a repeated arm, Away or unconfirmed area never asks");
console.log("ok  Home and a single disarm from Away ask, saying what stops and that Camera 4 stays armed");
console.log("ok  Cancel and Escape send nothing and leave the dialog able to ask again");
console.log("ok  Confirm sends the captured request exactly once, names the room, and closes");
console.log("ok  an unread status, a busy page, an unrecognised body and camera_4 produce no dialog and no request");
console.log("\nPASS  every assertion held");