# Expression assets

The production system uses a set of animated GIF expressions. Those files are
not included because their redistribution rights are not established.

The factory or owner asset pack lives outside the Git checkout at
`$RIVERBANK_HOME/.local/share/riverbank/assets/expressions`. Install files there
using the filenames referenced by `../../expressions.json`. Keeping the asset
pack outside the application source lets Edge OS upgrades replace code without
silently deleting locally licensed artwork. Recommended source size is 800×800
for the round DSI display; the renderer also accepts other sizes and scales
them to the configured output.
