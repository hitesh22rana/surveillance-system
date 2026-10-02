# Project instructions

This repository is an experiment. Commit and push changes directly to `main`;
do not create feature branches or pull requests unless the user asks for one.
This project-specific preference supersedes the user's global branch/PR workflow.

Always sign every Git commit and verify the signature before pushing.
Do not monitor CI after pushing unless explicitly requested.

Delegate all implementation work to the locally installed OpenCode running
`opencode/space-bunny-free` (Space Bunny Free). Codex does not write the code
itself: it plans the change, delegates the implementation, then reviews and
validates the result against live containers, real hardware, and screenshots.
If the local OpenCode database reports `no such column: project_id`, use an
isolated `XDG_DATA_HOME` rather than modifying existing data.

Keep camera credentials, runtime configuration, and footage out of Git and agent
prompts. Never read `.env`, `config/config.yml`, or anything under `storage/` into
a prompt. Use Frigate's existing zones, tracking, and snapshots before custom ML
or notification services. Record live validation separately from static checks,
and never report a pass that was not actually observed.

The dashboard is read-only by construction, with one authorised exception the
owner asked for: controlled manual deletion of reviewed snapshots. Keep everything
else that way. The proxy still pins the upstream stats key allowlist server-side,
and no credential, log, config or restart path is reachable from the browser. The
only write path in this repository is `scripts/retain.py --serve` answering
`POST /api/snapshots/delete`, it is irreversible, and it stays behind its own
refusals: the dashboard's exact Host, an Origin equal to that Host, a JSON content
type, `X-Surveillance-Action: delete`, and a body validated before any upstream
request. Both published ports stay on loopback, and the gateway publishes no port
at all — nginx reaches it on the internal network only. Deletion is never
automatic: the poll loop contains no `DELETE` and never will.

Destructive validation is allowed when the owner authorises it explicitly, which
he did on 2026-10-02 for one experimental snapshot; the earlier blanket
never-agent-deletes rule is withdrawn. The authorisation covers the act, not the
safeguards: never weaken the Host, Origin, content-type, action-header or body
validation to make a check easier, never widen the archive scope to prove it, and
never treat a disposable-fake pass as evidence about the live stack. Record a
destructive run as irreversible in the docs, with what was deleted and what was
confirmed afterwards.
