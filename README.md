# Alabama Plat Verifier — POC

Boundary closure verification tool for Alabama land survey plats.

**POC only — production will be F#/Giraffe.**

## What it does

- Accepts a DXF file upload
- Extracts boundary LINE/LWPOLYLINE entities
- Extracts TEXT/MTEXT bearing and distance labels
- Pairs labels to courses by spatial proximity
- Computes traverse closure (sum of departures and latitudes)
- Reports error of closure and precision ratio

## DXF layer conventions required

| Layer | Contents |
|-------|----------|
| `BOUNDARY` | LINE or LWPOLYLINE boundary entities |
| `BEARINGS` | TEXT entities with bearing labels |
| `DISTANCES` | TEXT entities with distance labels |
| `ANNOTATION` | TEXT entities for parcel info (optional) |

## Bearing format

```
N 45°30'00" E
S 12°15'00" W
```

## Distance format

```
210.00'
210.00 ft
```

## Generate a sample DXF

```bash
python generate_sample.py
```

Creates `sample_plat.dxf` — a 5-course closed traverse with correct layer conventions.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy

Push to a public GitHub repo, then deploy via [Streamlit Community Cloud](https://share.streamlit.io).
