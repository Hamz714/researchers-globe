# Curated tables (Person A)

Every row needs a real `source_url`. No source, no row.

- `ports.csv`: major iron ore export ports for the top producing countries.
  `port_id` must start with `port_` (e.g. `port_hedland`).
- `trade.csv`: top destination countries per exporter. `share` is the fraction of the
  exporter's iron ore exports going to that importer (0–1); it becomes the EXPORTS_TO edge weight.
- `countries.csv`: ISO2 code, name and an approximate centroid for drawing arcs.

`data/mock/*_mock.csv` show the same columns filled with illustrative values.
