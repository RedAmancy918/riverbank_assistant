# Configuration templates

Files in this directory describe the currently working deployment. Systemd and
health-monitor templates contain `@RIVERBANK_*@` placeholders; do not copy them
directly. Render them first with `scripts/render_config.py`.

- `systemd/`: core service units and drop-ins.
- `systemd/optional/proxy/`: generic local HTTP proxy drop-ins, disabled by default.
- `systemd/optional/viewturbo/`: proprietary ViewTurbo integration example only.
- `plymouth/`: RiverBank boot theme.
- `labwc/`: window and touch mapping reference for the round DSI screen.
- `lightdm/`: reuse Plymouth's VT to reduce display handoff flicker.
- `panel/`: reference panel configuration with startup notification sources removed.
- `udev/`: tested USB-camera power rule; update VID/PID for other cameras.
- `motd/`: optional SSH welcome page.

The installer does not automatically overwrite labwc, LightDM or panel user
configuration because those files are commonly shared with other desktop uses.
