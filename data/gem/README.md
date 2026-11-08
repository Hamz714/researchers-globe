# Global Iron Ore Mines Tracker (GEM), September 2026 (V1) release

`gem_iron_ore_2026-09.xlsx` is the standard copy downloaded from
https://globalenergymonitor.org/projects/global-iron-ore-mines-tracker on 8 Nov 2026.
Licence: CC BY 4.0. Credit in the app and slides: "Global Iron Ore Mines Tracker,
Global Energy Monitor, September 2026 (V1) release".

Sheet "Main Data", 980 mines, 701 operating. Column names are in `pipeline/graph.py`
(`GEM_COLUMN_MAP`). Rebuild the graph with:

    python -m pipeline.graph --gem data/gem/gem_iron_ore_2026-09.xlsx
