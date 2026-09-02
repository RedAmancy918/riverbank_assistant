# 具身智讯 / Paper Radar

Daily embodied-intelligence research digest for Hermes Agent.

## Runtime

- `scripts/collect.py`: arXiv collection, prefiltering, deduplication and candidate manifest.
- `AGENTS.md`: the durable editorial and deep-reading contract used by Hermes cron.
- `scripts/render.py`: report validation, Markdown archive and atomic static-site rendering.
- `scripts/web_server.py`: static-site server plus the one-shot special-focus and
  per-paper Q&A APIs.
- `scripts/paper_qa.py`: current-report reading buffer builder. It atomically
  replaces `data/paper-qa/current.json` and never creates dated full-text caches.
- `scripts/paper_chat_proxy.py`: same-origin bridge to the persistent Hermes Chat
  worker; browser clients never receive the model key or internal pairing token.
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

Industry monitoring uses the `company_sources` list in `config/topics.json` as
its canonical source registry. In addition to frontier-model teams and the
existing embodied-intelligence companies, it covers Boston Dynamics, Agility,
Apptronik, Sanctuary AI, FieldAI, Intrinsic, Amazon Robotics, RAI Institute and
Toyota Research Institute. Hugging Face, Pollen Robotics and LeRobot remain
explicitly covered. New open robot platforms such as Microduck or Reachy, and
substantive releases covering reinforcement learning, sim-to-real, robot
policies, datasets, simulation, evaluation and
deployment tooling, are eligible industry updates rather than generic product
marketing. Undated evergreen product pages require a separate dated official
announcement before they can be treated as new; financing, personnel, event
and investor-relations posts are excluded unless they contain independently
useful technical or deployment information.

The latest page can accept one special-focus description for the next 08:00 run.
The request is stored under `data/special-focus/`, adds at most five items outside
the normal candidate/selected limits, and is consumed only after a successful render.

## Article Q&A

Every selected paper, potential method and one-shot focus item has an inquiry panel
inside “展开方法、实验与判断”. The panel lazily loads only when expanded and asks the
existing Hermes Daily worker to answer from that article's current reading buffer.
Full-text HTML is reused from `/tmp/paperradar/<arxiv-id>.json` when available and
otherwise fetched during finalization; if arXiv is unavailable, the structured daily
reading notes remain usable and the page labels that fallback.

Retention deliberately separates evidence from conversation history:

- `data/paper-qa/current.json` contains only the current successfully rendered daily
  report and is replaced atomically on the next run; no historical full-text corpus
  is accumulated.
- `data/paper-qa/conversations.json` maps stable paper IDs to the persistent Chat
  database at `/var/lib/riverbank-tasks/chat.db`. These conversations survive daily
  cache replacement and service restarts.
- An archived article can still display its saved conversation. New questions are
  accepted only while that article exists in the current daily buffer, preventing an
  old chat from silently answering with stale or unrelated evidence.
- Internal article chats use source `paper-radar-internal` and are hidden from the
  ordinary desktop/mobile Chat conversation list.
