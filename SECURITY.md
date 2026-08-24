# Security

Do not open a public issue containing API keys, credentials, device identities,
private network addresses, photographs, recordings or logs with personal data.

Before publishing a vulnerability report, remove secrets and use a private
contact channel selected by the repository owner. No security contact address is
declared in this snapshot.

Runtime secrets belong in the external Hermes profile or a system secret store,
never in this repository. Camera Hub is intentionally bound to loopback by
default. Paper Radar should be protected by a LAN, tailnet ACL or authenticated
reverse proxy.
