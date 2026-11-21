# Ituri Mapping Time-Lapse

Weekly time-lapses of OpenStreetMap building footprints in the health zones of Ituri, DRC, affected by the 2026
Ebola (Bundibugyo virus) outbreak. Runs 1 April to 21 November 2026, alongside confirmed cases and deaths by health
zone.

**Live page:** https://aschroedercr.github.io/ituri-osm-timelapse/

The page is served from `docs/` and includes a light/dark toggle.

## What it shows
- An overview map of 26 health zones overlapping HOT's ~22,750 km² `#eboladrc2026` priority area. Grey shows buildings
  mapped before 1 April; orange shows buildings added since.
- Zoomed 5.5 km views of the seven zones with the most additions, drawn as real footprints week by week on a current
  OSM basemap.
- Per-zone charts of buildings in OSM against cumulative confirmed cases and deaths (INSP daily SitReps, WHO DON).
- A ranking of zones by net gain, growth, additions, removals, cases and deaths, with method notes and caveats.

The date a building appears is when it was **mapped**, not when it was built. Mapping order follows Tasking Manager
priorities, not transmission. Validators also delete and re-trace footprints, so read additions and removals together.

## Pipeline
| Step | Script |
|---|---|
| Fetch OSM history per tile (ohsome API v2, needs `OHSOME_API_KEY`) | `ebola_osm_timelapse.py fetch` |
| Extend the cached history to a later date (one request per tile) | `update_increment.py` |
| Refresh case data from INSP SitReps | `insp_list.py`, `insp_parse.py` |
| Check rebuilt counts against `/stats/features/count` | `ebola_osm_timelapse.py validate`, `validate_tiles.py` |
| Rebuild snapshots and write per-zone CSVs | `ebola_osm_timelapse.py build` |
| Light and dark footprint and overview time-lapses (Overpass basemap, Roboto) | `zone_timelapses.py` |
| Assemble the page | `make_page.py` + `page_template.html` + `data/page_meta.json` |

Inputs in `data/`:
- `zones.gpkg`: GRID3 COD Ituri health zones (Sept 2026 update), filtered to the priority area.
- `ituri_cases_by_zone.csv`: parsed from INSP SitReps; the matching source URLs are in `ituri_cases_by_zone_sources.csv`.

Offline test: run `python make_synthetic_cache.py out_test`, then the `build` command in `CLAUDE.md`.

## Sources and licences
- Map data © OpenStreetMap contributors, ODbL, via the HeiGIT ohsome API and the Overpass API.
- Health zones © GRID3.
- Case data: INSP (DRC Ministry of Health) and WHO.
- Roboto font: SIL Open Font License.
