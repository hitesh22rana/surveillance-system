# Project instructions

This repository is an experiment. Commit and push changes directly to `main`;
do not create feature branches or pull requests unless the user asks for one.
This project-specific preference supersedes the user's global branch/PR workflow.

Always sign every Git commit and verify the signature before pushing.
Do not monitor CI after pushing unless explicitly requested.

Write no em dashes in anything you add to this repository. Not in prose or
Markdown, not in UI copy, labels, placeholders or empty-value glyphs, not in
code comments, docstrings or printed check output, and not in config comments.
Use a plain ASCII hyphen with the surrounding spacing preserved, or restructure
the sentence. En dashes are fine where a range or a numeric span is meant
(`camera_1` through `camera_3` reads as `1–3`), as are minus operators and
structural delimiters. The whole tree was swept clean of every representation of
the em dash on 2026-10-02: the character itself, the named and decimal and hex
HTML entities, and the backslash-u escape used in JavaScript and Python string
literals. A new one is a regression rather than a style choice, and
`git ls-files` plus a byte scan of those representations is the check.

Delegate all implementation work to the locally installed OpenCode running
`opencode/space-bunny-free` (Space Bunny Free) at `--variant max`, which is the
preferred effort level for every delegated run. Codex does not write the code
itself: it plans the change, delegates the implementation, then reviews and
validates the result against live containers, real hardware, and screenshots.
If the local OpenCode database reports `no such column: project_id`, use an
isolated `XDG_DATA_HOME` rather than modifying existing data.

Keep camera credentials, runtime configuration, and footage out of Git and agent
prompts. Never read `.env`, `config/config.yml`, or anything under `storage/` into
a prompt. Use Frigate's existing zones, tracking, and snapshots before custom ML
or notification services. Record live validation separately from static checks,
and never report a pass that was not actually observed.

The dashboard is read-only by construction, with two authorised exceptions the
owner asked for: controlled manual deletion of reviewed snapshots, and manual
arming of person monitoring on `camera_1`, `camera_2` and `camera_3`. Keep
everything else that way. The proxy still pins the upstream stats key allowlist
server-side, and no credential, log, config or restart path is reachable from the
browser. The only writes from the browser are in `scripts/retain.py --serve`, both
behind their own refusals.

An automated sidecar, `scripts/fighting.py`, is separately authorised and is not one
of those two writes because the browser cannot reach it at all. The owner authorised
it to do exactly two things to events that already exist: write the native
`possible_fighting` sub-label on a matching `person` event on `camera_2` or
`camera_3`, and set the retain flag so that snapshot is kept for manual review. It
must never delete, never un-pin, never edit configuration and never create an event,
and it publishes no port. Its model weights are provisioned into a mounted directory
from outside this repository and are never downloaded at runtime.

`GET /api/activity/fighting` is the whole of what the browser may see of it: one
exact nginx location onto the sidecar's own read-only `/status`, a fixed upstream
path, no forwarded client query, no CORS, and the server's 4 s timeouts. The upstream
is a variable so Docker's resolver, not nginx's start-time lookup, resolves it, so the
dashboard still starts and still serves everything else when the sidecar is absent.
Every other `/api/activity` path is 404. Treat the status as untrusted input: the
page checks it field by field and fails closed, and a loading model, a refused model,
a camera with no recent capture or score, and stale data must never read as
Monitoring.

`possible_fighting` is a **possibility a model reported, never a finding**, and its
accuracy has not been measured on these two rooms. Never present a score as a
confidence or an accuracy, and never let inference timings stand in for either. Two
lifetimes now exist and must not be conflated: an ordinary snapshot is kept six
hours, and a snapshot the watch labels is kept indefinitely until a person deletes
it.

Because three separate things keep snapshots, no copy anywhere may claim Home or a
disarm stops all of them. Home and a disarm stop **armed person-presence review and
only that**; `camera_4`'s area_4, the animal rule on `camera_1` to `camera_3` and
the fighting watch on `camera_2` and `camera_3` all continue.

`POST /api/snapshots/delete` is irreversible and answers the reviewed-deletion
call behind the dashboard's exact Host, an Origin equal to that Host, a JSON
content type, `X-Surveillance-Action: delete`, and a body validated before any
upstream request. It is the only path that can destroy anything, and it takes no
query string on any method: a variable `proxy_pass` drops the arguments silently
rather than forwarding them, so the guard is explicit at both the proxy and the
worker.

`POST /api/security/entry` is the arming write: exactly
`{"camera":"camera_1"|"camera_2"|"camera_3","armed":true|false}` or exactly
`{"mode":"home"|"away"}`, behind the same Host, Origin and content-type refusals
plus `X-Surveillance-Action: arm-entry`. It writes nothing upstream and edits no
configuration - it saves three flags and three timestamps in one small JSON file
and flips what a later poll pass may keep. It reads the camera service's
`/api/config` internally to learn whether the zone that camera needs exists, but
only booleans per camera ever leave that route; no config, URL, camera, file path
or raw error is forwarded to the browser. `camera_4` is armed internally from
startup: it is not in the saved file, so no request and no writable file can
disable it, and both Home and Away leave it alone. The 60-second full-frame person
rule is removed on every camera and records kept under it stay exactly as they
are.

Arming is a person's decision made on the dashboard and it is now durable, so the
older "every restart comes back disarmed" rule no longer holds for cameras 1–3;
what still holds is that a fresh installation is Home, that there is no
command-line route to arm anything, and that arming is never automatic. A file
this cannot read falls back to Home and says so rather than guessing Away, and a
camera whose area is not confirmed stops pinning without losing its setting or the
moment it was armed.

Both published ports stay on loopback, and the gateway publishes no port at all -
nginx reaches it on the internal network only. The fighting sidecar publishes none
either: nginx reaches it as `fighting:8091` on that same internal network. Deletion
is never automatic: the poll loop contains no `DELETE` and never will. Arming is
never automatic either: it is a person's decision, and camera_4's is fixed in this
process rather than in any file.

Destructive validation is allowed when the owner authorises it explicitly, which
he did on 2026-10-02 for one experimental snapshot; the earlier blanket
never-agent-deletes rule is withdrawn. The authorisation covers the act, not the
safeguards: never weaken the Host, Origin, content-type, action-header or body
validation to make a check easier, never widen the archive scope to prove it, and
never treat a disposable-fake pass as evidence about the live stack. Record a
destructive run as irreversible in the docs, with what was deleted and what was
confirmed afterwards.
