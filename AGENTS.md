# Project instructions

This repository is an experiment. Commit and push changes directly to `main`;
do not create feature branches or pull requests unless the user asks for one.
This project-specific preference supersedes the user's global branch/PR workflow.

Always sign every Git commit and verify the signature before pushing.
Do not monitor CI after pushing unless explicitly requested.

For coding work, prefer delegation to locally installed OpenCode with
`opencode/space-bunny-free` (Space Bunny Free); Codex orchestrates, reviews, and
validates the result. If the local OpenCode database reports `no such column:
project_id`, use an isolated `XDG_DATA_HOME` rather than modifying existing data.

Keep camera credentials, runtime configuration, and footage out of Git and agent
prompts. Use Frigate's existing zones, tracking, and snapshots before custom ML
or notification services. Record live validation separately from static checks.
