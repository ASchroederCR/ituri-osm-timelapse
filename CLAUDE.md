# Task: OSM change time-lapse for Ebola-affected areas of DRC

Goal: weekly time-lapse (GIF/MP4) and per-health-zone statistics showing how OpenStreetMap
buildings changed in the 2026 Bundibugyo Ebola outbreak area (Ituri epicentre: Mongbwalu,
Bunia, Rwampara), plus a ranking of zones by net increase.

Everything is implemented in `ebola_osm_timelapse.py`. Your job is to run it, verify it, and fix
what breaks. Do not rewrite it unless a verification step fails.

## Facts to rely on (checked against docs.ohsome.org, Nov 2026)
- ohsome API **v1 shuts down 30 Nov 2026**. Use **v2** only: `https://api.heigit.org/ohsome-api/v2`.
  If this URL 404s (the docs say `-rc` may be dropped or retained), try `.../v2-rc` via `OHSOME_API_URL`.
- v2 needs an API key in the `Authorization` header (free signup: account.heigit.org/signup). Env var: `OHSOME_API_KEY`.
- `ohsome-py` does not support v2. Use raw HTTP (httpx). No `groupBy/boundary`; we tile instead.
- `/extraction/features.parquet` returns every version overlapping the time range with `edit_timestamp`
  and `valid_to_timestamp`. A snapshot at T = rows with `edit_timestamp <= T < valid_to_timestamp`.
- The docs say `valid_to_timestamp` is 2222-01-01 for versions that are current **or deleted**. The script
  closes deleted versions using `DELETION` rows from `/extraction/contributions.parquet`. **Verify this.**
- Contributions include `changeset_tags` (hashtags such as `#eboladrc2026`). Extraction timeout is 4 min,
  stats timeout 3 min.
- Outbreak/mapping context: HOT ESA Hub + OSM RDC + MSF TM projects since 21 May 2026, hashtag
  `#eboladrc2026`, priority area ~22,000 km2 around Mongbwalu/Bunia (Missing Maps blog, 7 Jul 2026).
  Validators delete obsolete buildings, so net counts understate mapping effort (script reports removals).

## Inputs you must obtain
1. `OHSOME_API_KEY` in the environment.
2. Health zone/area polygons for Ituri (GRID3 COD Ituri release on the GRID3 Data Hub / HDX, superseding v8.0,
   or OCHA/fieldmaps.io admin boundaries). Save as `data/zones.gpkg` (or .geojson). Run
   `python ebola_osm_timelapse.py inspect-zones data/zones.gpkg` and pick the name column (`--zone-col`).
   Restrict to the outbreak-affected zones if the file covers the whole province.
   If no polygon file is available, use `--bbox` (approximate Mongbwalu-Bunia box, replace when you can:
   `29.7 1.2 30.9 2.5`) and say so in the final report.

## Steps (stop and report if any verification fails)
0. `pip install -r requirements.txt`
1. Dry run offline: `python tests/make_synthetic_cache.py out_test` then
   `python ebola_osm_timelapse.py build --out out_test --zones out_test/zones.geojson --start 2026-04-01 --end 2026-10-31 --step-days 14`.
   Expect zone_gain.csv: Bunia 200->486, Rwampara 300->1432.
2. Inspect one real response before the full run: fetch a single small tile, then
   `pq.read_schema(file)`. Confirm column names/types match the schema above, and that `geom` is WKB bytes.
   If pyarrow returns a geospatial type, adapt `read_tile` only.
3. Fetch (resumable; cached per tile):
   `python ebola_osm_timelapse.py fetch --zones data/zones.gpkg --zone-col <col> --layer buildings --start 2026-04-01 --end <today>`
   On 504/timeouts lower `--tile-deg` (0.25 default; try 0.15) and delete the cache for that layer.
4. Validate: `python ebola_osm_timelapse.py validate --zones data/zones.gpkg --layer buildings --start 2026-04-01 --end <today> --n-tiles 3`
   Reconstructed counts must be within ~2% of `/stats/features/count` per tile and date.
   If larger: (a) check deletion handling in `adjust_deletions`, (b) confirm the stats endpoint
   counts the same geometry types (it also includes multipolygon relations), (c) report the gap.
   Do not proceed to publish results with an unexplained gap >2%.
5. Build: `python ebola_osm_timelapse.py build --zones data/zones.gpkg --zone-col <col> --layer buildings --start 2026-04-01 --end <today> --campaign eboladrc2026`
   Outputs in `out/buildings/`: `timelapse.gif`, `timelapse.mp4`, `frames/`, `zone_timeseries.csv`,
   `zone_gain.csv`, `zone_gain.png`.
6. Cross-check against independent sources and report differences:
   - HDX weekly export "DRC & Uganda - Ebola Outbreak - May 2026 - OpenStreetMap Data" (building counts at a date).
   - GRID3 gap analysis: ~1,200 settlement points in the outbreak zone had no OSM settlement within 1 km (June 2026).
   - ohsomeNow dashboard for `#eboladrc2026` (campaign totals vs `new_campaign` column).
7. Optional layers: `--layer residential`, `--layer places`. Roads are NOT supported by the density renderer
   (centroids misrepresent lines); if wanted, add a length-based layer using `/stats/features/length` per zone.

## Interpretation rules for the final report
- Rank zones by `net_gain` AND `growth_pct` (a small baseline inflates percentages).
- `edit_timestamp` is when mapping entered OSM, not when buildings were built. State this.
- Frame-to-frame "new" is capped by Tasking Manager task order, not outbreak intensity; compare against INSP case counts
  by health zone before claiming mapping followed the epidemic.
- Report the exact zone file/version, date range, API version, and any validation deviation.
