# 具身智讯 / Paper Radar

Daily embodied-intelligence research digest for Hermes Agent.

## Runtime

- `scripts/collect.py`: arXiv collection, prefiltering, deduplication and candidate manifest.
- `AGENTS.md`: the durable editorial and deep-reading contract used by Hermes cron.
- `scripts/render.py`: report validation, Markdown archive and atomic static-site rendering.
- `scripts/web_server.py`: static-site server plus the one-shot special-focus API.
- `scripts/special_focus.py`: atomic queue for next-day special-focus descriptions.
- `public/`: latest report at `/`, title-and-abstract candidate list at `/candidates/`,
  and historical reports under `/archive/`.
- `../../config/systemd/paper-radar-web.service`: boot-persistent web service template.

The default timezone is Asia/Shanghai. The web app listens on `0.0.0.0:19732`,
so it can be reached through a LAN or Tailscale address. Access control remains
the responsibility of the LAN, firewall, reverse proxy or tailnet policy; do not
expose the port directly to the public Internet without authentication.

Current editorial limits are 30 paper or industry-research candidates at 10/100 or
higher, 10 selected papers or potential methods at 40/100 or higher, and at most 9
industry updates within a rolling 183-day window. Work from the latest 72 hours is
prioritized; unused slots are backfilled with meaningful, never-before-listed work
from the preceding 183 days. Retrieval combines embodied-intelligence, upstream-
method, robotics-crossover, category-latest, dedicated VLM, VGGT-like foundation-
vision / 3D-geometry / spatial-intelligence, and JEPA / predictive-world-model
streams. All counts are ceilings, never quotas.

The latest page can accept one special-focus description for the next 08:00 run.
The request is stored under `data/special-focus/`, adds at most five items outside
the normal candidate/selected limits, and is consumed only after a successful render.
