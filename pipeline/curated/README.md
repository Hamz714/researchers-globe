# Curated tables (Person A)

Every row needs a real `source_url`. No source, no row.

- `ports.csv`: major iron ore export ports for the top producing countries.
  `port_id` must start with `port_` (e.g. `port_hedland`).
- `trade.csv`: top destination countries per exporter. `share` is the fraction of the
  exporter's iron ore exports going to that importer (0–1); it becomes the EXPORTS_TO edge weight.
- `countries.csv`: ISO2 code, name and an approximate centroid for drawing arcs.

`data/mock/*_mock.csv` show the same columns filled with illustrative values.

## How the numbers were made (8 Nov 2026)

- `trade.csv`: UN Comtrade via WITS, 2023, exports of HS 260111 (non-agglomerated iron ore)
  by partner, share = partner value / World value. Rows below ~5% dropped. Two exceptions:
  Canada uses 260111 + 260112 (pellets) combined because pellets are a large part of its
  exports, and the US row uses 260112 (US iron ore exports are almost all pellets).
- `countries.csv`: centroids from Google's canonical countries table.
- `ports.csv`: approximate terminal coordinates; source is the port's (or operator's) Wikipedia page.
- `companies.csv`: maps GEM parent names to our company ids, listing country, Wikipedia title
  (exact article title, used for the attention signal) and news search terms.
