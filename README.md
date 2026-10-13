# RFD WET · river data

Public river data for the RFD WET app (Reno Fire Department Water Entry Team).
A GitHub Action (`.github/workflows/data.yml`) runs `update_data.py` every 15 minutes and
commits `data/*.json` when anything changed:

- `cdec.json` reservoir storage, inflow, outflow (CDEC; browsers can't fetch CDEC directly)
- `normals.json` USGS daily flow percentiles
- `daily.json` daily mean and peak flow per gauge
- `travel.json` learned travel times between gauges
- `lake.json` Lake Tahoe daily elevation

All of it is public data from USGS and the California Data Exchange Center. The app itself lives
in a private repository.
