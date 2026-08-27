# Wildfire map

A single HTML file that plots live US wildfire data from public federal feeds. No build step, no
dependencies to install, no API keys, no account. Open the file and it works.

Every layer answers one question the usual fire map does not: **how old is this record?** Incident
markers are colored by the age of the record itself, each popup names the exact field the age came
from, and every layer carries its own "last update" stamp. A fire map that looks current but is
running on six-hour-old agency records is worse than one that admits it.

## Quick start

Open `index.html` in a browser. That is the whole install.

To host it, drop the repo on GitHub Pages (Settings, Pages, deploy from branch). `index.html` at the
repo root is served automatically. Everything it calls is HTTPS and CORS-enabled, so it works
identically from `file://`, from Pages, or from any static host.

## What it shows

| Layer | Source | Marker | Notes |
|---|---|---|---|
| Active incidents | NIFC / IRWIN interagency records | Dot, colored by record age | Default on |
| Fire perimeters | NIFC current perimeters | Orange outline | Default on |
| VIIRS hotspots | NASA FIRMS, 375 m | Yellow diamond | 3 to 4 looks a day |
| GOES fire detections | NOAA HMS, GOES-East and GOES-West | Green triangle | Last 24 h, newest first |
| MODIS hotspots | NASA FIRMS, 1 km | Violet square | Last 48 h |

Fifteen preset areas (eleven western states plus Texas, Alaska, Western US, Continental US), a table
view of incidents sorted oldest record first, and a median record age for whatever is in view.

Note on GOES: a geostationary sensor re-detects the same fire every few minutes, and each 2 km pixel
becomes its own record. A dense green cluster is one fire seen many times, not many fires. A
state-sized view can return thousands of GOES points, which is why the layer is off by default and
capped.

## Data sources

All feeds are public, anonymous, and free. Each was queried without credentials to confirm that.

- **Incidents and perimeters**: `services9.arcgis.com/RHVPKKiFTONKtxq3/.../USA_Wildfires_v1`, Esri
  Living Atlas republication of NIFC and IRWIN interagency records.
- **VIIRS**: `.../Satellite_VIIRS_Thermal_Hotspots_and_Fire_Activity`, NASA FIRMS 375 m detections.
- **MODIS**: `.../MODIS_Thermal_v1`, NASA FIRMS 1 km detections, last 48 hours.
- **GOES**: `services2.arcgis.com/C8EMgrsFcRFL6LrL/.../NOAA_Satellite_Fire_Detections_(v1)`, NOAA
  NESDIS Hazard Mapping System, filtered to `Satellite LIKE 'GOES%'`.

The two org ids in those URLs belong to the publishers, Esri's public live-feeds org and NOAA
NESDIS. They are the documented endpoints for these feeds, not anyone's private instance.

The underlying observations are US federal work and in the public domain. The hosted services are
Esri's and NOAA's redistribution of them, free to query without an account. If you are building
something commercial on top, read Esri's terms for their hosted services rather than assuming the
public-domain status of the data carries over to the hosting.

## Basemaps

Six options, none requiring an account or key:

| Basemap | Source | Terms |
|---|---|---|
| USGS Imagery Topo (default) | The National Map | US federal, public domain |
| USGS Topo | The National Map | US federal, public domain |
| USGS Shaded Relief | The National Map | US federal, public domain |
| OpenStreetMap | OSM Foundation | Attribution, see tile usage policy |
| OpenTopoMap | OpenTopoMap | CC-BY-SA, attribution |
| NASA GIBS Blue Marble | NASA EOSDIS | Public domain, zoom 8 max |

The USGS layers are the default deliberately. **If you fork this and expect real traffic, do not
point users at the OpenStreetMap or OpenTopoMap community tile servers.** Their usage policies ask
distributed applications to run their own tile infrastructure. The USGS and NASA endpoints have no
such restriction.

## Refresh behavior

Auto refresh is on by default and paced to how fast each source actually changes:

| Layer | Interval |
|---|---|
| Incidents, GOES | 5 min |
| Perimeters, VIIRS | 10 min |
| MODIS | 15 min |

A single one-minute tick decides what is due. Guards, in order of how much they matter:

- Only layers that are switched on are polled.
- Nothing runs while the browser tab is hidden or the browser is offline.
- One request per layer at a time, and requests within a tick are staggered 1.5 s apart.
- HTTP 429 and 503 are treated as failures and trigger exponential backoff, doubling up to 8x,
  reset on the first success.
- Each response gets a signature (count, newest timestamp, first and last record id). An unchanged
  result skips the redraw entirely, so the map does not rebuild markers for nothing.
- Results are cached per layer for 90 s and fetched 25% beyond the visible edge, so small pans and
  zooms cost no requests at all.
- Toggling a layer off only hides it. Data stays in memory, the stamp is untouched, and switching it
  back on is instant.
- A failed refresh keeps the previous markers on the map and labels the stamp "refresh failed, still
  showing this set". Stale and labeled beats blank.

With every layer on and the tab in front, that is roughly 15 requests an hour spread across four
services.

## Privacy

No API keys, tokens, accounts, cookies, localStorage, sessionStorage, geolocation, or analytics.
Nothing is sent anywhere except the tile and feature queries listed above. The only third-party
script is Leaflet 1.9.4 from cdnjs, BSD-2 licensed; vendor it locally if you would rather not depend
on a CDN.

## Limitations

- Each layer is capped at 1000 records per view. When the cap is hit the page says so rather than
  truncating silently.
- Coverage is US only. All four feeds are US federal.
- Perimeter records vary in freshness far more than incident records; some carry no usable timestamp
  at all, in which case the marker is gray and the popup says so.
- This is a viewer, not a warning system. Do not make evacuation decisions from it. Follow your local
  emergency management agency.

## License

MIT, see `LICENSE`

## ToDo/Roadmap

- GOES dedup by pixel
- URL parameters for sharing a view
- vendoring Leaflet
