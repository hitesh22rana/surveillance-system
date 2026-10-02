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

The dashboard is read-only by construction. Keep it that way: the proxy pins the
upstream stats key allowlist server-side, no credential, log, config, or restart
path is reachable from the browser, and both published ports stay on loopback.
