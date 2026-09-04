# RiverBank Agent Runtime

`riverbank.agent/v1` is the provider-neutral execution boundary for RiverBank
Edge OS. Voice, Chat, background tasks, the Workshop and the daily report must
request model work through this contract instead of importing or invoking an
agent framework directly.

The first adapter is `hermes-cli`. Hermes remains the installed backend and
keeps its private profile, model credentials and tool implementation. Product
services only know the stable Unix socket protocol at
`/run/riverbank-agent/runtime.sock`.

The broker accepts bounded `run`, `cancel` and `health` operations. Callers may
select only named workspaces and allow-listed toolsets configured by the
service. Image paths must resolve beneath configured trusted roots. Autonomous
mode is restricted to trusted background-task purposes. The socket accepts the
service user and root only; the adapter never accepts an arbitrary command,
working directory, environment variable or executable from a caller.

This boundary permits a future adapter to replace Hermes without changing the
display, ASR/TTS, accounts, Workshop packages, task database or clients.
