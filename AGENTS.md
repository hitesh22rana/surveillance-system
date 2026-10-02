# Project instructions

This repository is an experiment. Commit and push changes directly to `main`;
do not create feature branches or pull requests unless the user asks for one.
This project-specific preference supersedes the user's global branch/PR workflow.

Always sign every Git commit and verify the signature before pushing.
Do not monitor CI after pushing unless explicitly requested.

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
arming of entry review on `camera_1` and `camera_4`. Keep everything else that
way. The proxy still pins the upstream stats key allowlist server-side, and no
credential, log, config or restart path is reachable from the browser. The only
writes are in `scripts/retain.py --serve`, both behind their own refusals.

`POST /api/snapshots/delete` is irreversible and answers the reviewed-deletion
call behind the dashboard's exact Host, an Origin equal to that Host, a JSON
content type, `X-Surveillance-Action: delete`, and a body validated before any
upstream request. It is the only path that can destroy anything.

`POST /api/security/entry` is the arming write: exactly
`{"camera":"camera_1"|"camera_4","armed":true|false}`, behind the same Host,
Origin and content-type refusals plus `X-Surveillance-Action: arm-entry`. It
writes nothing upstream and edits no configuration — it flips a flag in the
worker's memory that decides what a later poll pass may keep, on those two
cameras only, and it never unpins and never deletes. It reads Frigate's
`/api/config` internally to learn whether the expected zone exists, but only two
booleans per camera ever leave that route; no config, URL, camera or raw error is
forwarded to the browser. The 60-second full-frame person rule is removed on every
camera and records kept under it stay exactly as they are.

Both published ports stay on loopback, and the gateway publishes no port at all —
nginx reaches it on the internal network only. Deletion is never automatic: the
poll loop contains no `DELETE` and never will. Arming is never automatic either:
it is disarmed on every restart and there is no command-line route to arm it.

Destructive validation is allowed when the owner authorises it explicitly, which
he did on 2026-10-02 for one experimental snapshot; the earlier blanket
never-agent-deletes rule is withdrawn. The authorisation covers the act, not the
safeguards: never weaken the Host, Origin, content-type, action-header or body
validation to make a check easier, never widen the archive scope to prove it, and
never treat a disposable-fake pass as evidence about the live stack. Record a
destructive run as irreversible in the docs, with what was deleted and what was
confirmed afterwards.
