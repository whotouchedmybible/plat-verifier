"""
Alabama Plat Verifier - POC
Streamlit app for DXF boundary closure verification.
Python/Streamlit POC only - production will be F#.

Layer conventions expected in DXF:
  BOUNDARY  - LINE or LWPOLYLINE entities
  BEARINGS  - TEXT entities with bearing strings
  DISTANCES - TEXT entities with distance strings

Bearing format: N 45°30'00" E  or  S 12°15'00" W  etc.
Distance format: 210.00'  or  210.00 ft  etc.
"""

import math
import re
import io
import streamlit as st
import ezdxf
from ezdxf import recover
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass
class Course:
    bearing_raw: str
    distance_ft: float
    quadrant: str      # NE, SE, SW, NW
    degrees: float     # decimal degrees of angle from N or S
    azimuth: float     # 0-360 decimal degrees
    departure: float   # easting component
    latitude: float    # northing component


@dataclass
class ClosureResult:
    courses: list
    total_departure: float
    total_latitude: float
    error_of_closure: float
    total_perimeter: float
    precision_ratio: float
    closes: bool
    tolerance_ft: float


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

BEARING_PATTERN = re.compile(
    r"([NS])\s*(\d{1,3})[°\s]+(\d{1,2})['\s]+(\d{1,2})[\"']?\s*([EW])",
    re.IGNORECASE
)

DISTANCE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:ft|'|feet)?",
    re.IGNORECASE
)


def parse_bearing(text: str) -> Optional[tuple]:
    """
    Parse bearing string into (quadrant, decimal_degrees).
    Returns None if unparseable.
    """
    m = BEARING_PATTERN.search(text)
    if not m:
        return None
    ns, d, mn, s, ew = m.groups()
    dd = int(d) + int(mn) / 60.0 + int(s) / 3600.0
    quadrant = (ns + ew).upper()
    return quadrant, dd


def bearing_to_azimuth(quadrant: str, angle_deg: float) -> float:
    if quadrant == "NE":
        return angle_deg
    elif quadrant == "SE":
        return 180.0 - angle_deg
    elif quadrant == "SW":
        return 180.0 + angle_deg
    elif quadrant == "NW":
        return 360.0 - angle_deg
    raise ValueError(f"Unknown quadrant: {quadrant}")


def parse_distance(text: str) -> Optional[float]:
    m = DISTANCE_PATTERN.search(text)
    if not m:
        return None
    return float(m.group(1))


def compute_course(bearing_raw: str, distance_ft: float) -> Optional[Course]:
    parsed = parse_bearing(bearing_raw)
    if not parsed:
        return None
    quadrant, angle_deg = parsed
    az = bearing_to_azimuth(quadrant, angle_deg)
    az_rad = math.radians(az)
    dep = distance_ft * math.sin(az_rad)
    lat = distance_ft * math.cos(az_rad)
    return Course(
        bearing_raw=bearing_raw,
        distance_ft=distance_ft,
        quadrant=quadrant,
        degrees=angle_deg,
        azimuth=az,
        departure=dep,
        latitude=lat
    )


# ---------------------------------------------------------------------------
# DXF extraction
# ---------------------------------------------------------------------------

def extract_from_dxf(fileobj) -> dict:
    """
    Extract boundary lines and text labels from DXF.
    Returns dict with lines, bearings, distances, warnings, raw_texts.
    """
    result = {
        "lines": [],
        "bearings": [],
        "distances": [],
        "annotations": [],
        "warnings": [],
        "raw_texts": [],
        "layers_found": set(),
        "entity_counts": {}
    }

    try:
        doc, auditor = recover.readfile(fileobj)
    except Exception as e:
        result["warnings"].append(f"DXF read error: {e}")
        return result

    msp = doc.modelspace()

    entity_counts = {}
    for entity in msp:
        etype = entity.dxftype()
        entity_counts[etype] = entity_counts.get(etype, 0) + 1
        layer = entity.dxf.layer.upper() if hasattr(entity.dxf, "layer") else "0"
        result["layers_found"].add(layer)

        if etype == "LINE":
            try:
                p1 = (entity.dxf.start.x, entity.dxf.start.y)
                p2 = (entity.dxf.end.x, entity.dxf.end.y)
                result["lines"].append({
                    "layer": layer,
                    "start": p1,
                    "end": p2,
                    "length": math.dist(p1, p2)
                })
            except Exception:
                pass

        elif etype == "LWPOLYLINE":
            try:
                pts = list(entity.get_points())
                for i in range(len(pts) - 1):
                    p1 = (pts[i][0], pts[i][1])
                    p2 = (pts[i+1][0], pts[i+1][1])
                    result["lines"].append({
                        "layer": layer,
                        "start": p1,
                        "end": p2,
                        "length": math.dist(p1, p2)
                    })
                if entity.is_closed and len(pts) > 1:
                    p1 = (pts[-1][0], pts[-1][1])
                    p2 = (pts[0][0], pts[0][1])
                    result["lines"].append({
                        "layer": layer,
                        "start": p1,
                        "end": p2,
                        "length": math.dist(p1, p2)
                    })
            except Exception:
                pass

        elif etype in ("TEXT", "MTEXT"):
            try:
                if etype == "TEXT":
                    text = entity.dxf.text.strip()
                    insert = (entity.dxf.insert.x, entity.dxf.insert.y)
                else:
                    text = entity.plain_mtext().strip()
                    insert = (entity.dxf.insert.x, entity.dxf.insert.y)

                result["raw_texts"].append({
                    "text": text,
                    "layer": layer,
                    "insert": insert
                })

                if layer == "BEARINGS" or BEARING_PATTERN.search(text):
                    result["bearings"].append({
                        "text": text,
                        "layer": layer,
                        "insert": insert
                    })
                elif layer == "DISTANCES" or re.search(r"\d+\.\d+\s*'", text):
                    result["distances"].append({
                        "text": text,
                        "layer": layer,
                        "insert": insert
                    })
                elif layer == "ANNOTATION":
                    result["annotations"].append(text)

            except Exception:
                pass

    result["entity_counts"] = entity_counts
    result["layers_found"] = sorted(result["layers_found"])
    return result


# ---------------------------------------------------------------------------
# Pair bearings with distances and compute courses
# ---------------------------------------------------------------------------

def pair_and_compute(bearings: list, distances: list) -> tuple:
    """
    Pair bearings with distances by proximity of insertion points.
    Returns (courses, unpaired_warnings).
    """
    courses = []
    warnings = []
    used_distances = set()

    for b in bearings:
        bx, by = b["insert"]
        best_idx = None
        best_dist = float("inf")

        for i, d in enumerate(distances):
            if i in used_distances:
                continue
            dx, dy = d["insert"]
            dist_between = math.sqrt((bx - dx)**2 + (by - dy)**2)
            if dist_between < best_dist:
                best_dist = dist_between
                best_idx = i

        if best_idx is None:
            warnings.append(f"No distance found for bearing: {b['text']}")
            continue

        if best_dist > 50.0:
            warnings.append(
                f"Bearing '{b['text']}' paired with distance '{distances[best_idx]['text']}' "
                f"but they are {best_dist:.1f} units apart — verify pairing."
            )

        dist_val = parse_distance(distances[best_idx]["text"])
        if dist_val is None:
            warnings.append(f"Could not parse distance: {distances[best_idx]['text']}")
            continue

        used_distances.add(best_idx)
        course = compute_course(b["text"], dist_val)
        if course:
            courses.append(course)
        else:
            warnings.append(f"Could not parse bearing: {b['text']}")

    for i, d in enumerate(distances):
        if i not in used_distances:
            warnings.append(f"Unpaired distance label: {d['text']}")

    return courses, warnings


# ---------------------------------------------------------------------------
# Closure math
# ---------------------------------------------------------------------------

def compute_closure(courses: list, tolerance_ft: float = 0.05) -> ClosureResult:
    total_dep = sum(c.departure for c in courses)
    total_lat = sum(c.latitude for c in courses)
    eoc = math.sqrt(total_dep**2 + total_lat**2)
    perimeter = sum(c.distance_ft for c in courses)
    precision = (1.0 / (eoc / perimeter)) if eoc > 0 else float("inf")
    return ClosureResult(
        courses=courses,
        total_departure=total_dep,
        total_latitude=total_lat,
        error_of_closure=eoc,
        total_perimeter=perimeter,
        precision_ratio=precision,
        closes=eoc <= tolerance_ft,
        tolerance_ft=tolerance_ft
    )


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Alabama Plat Verifier — POC",
    page_icon="📐",
    layout="wide"
)

st.title("Alabama Plat Verifier")
st.caption("POC — Boundary Closure Verification from DXF")

st.info(
    "**Expected DXF layer conventions:** "
    "`BOUNDARY` for line entities, "
    "`BEARINGS` for bearing text labels, "
    "`DISTANCES` for distance text labels. "
    "Bearing format: `N 45°30'00\" E`  |  Distance format: `210.00'`"
)

# Sidebar controls
with st.sidebar:
    st.header("Settings")
    tolerance = st.number_input(
        "Closure tolerance (ft)",
        min_value=0.001,
        max_value=1.0,
        value=0.05,
        step=0.001,
        format="%.3f"
    )
    show_raw = st.checkbox("Show raw DXF entities", value=False)
    show_courses = st.checkbox("Show course detail", value=True)

    st.markdown("---")
    st.caption("POC only. Production tool will be F#.")

# File upload
uploaded = st.file_uploader("Upload DXF file", type=["dxf"])

if uploaded is not None:
    # Write to temp path for ezdxf recover
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(uploaded.read())
        tmp_path = tmp.name

    try:
        with st.spinner("Parsing DXF..."):
            extracted = extract_from_dxf(tmp_path)
    finally:
        os.unlink(tmp_path)

    # --- DXF Summary ---
    st.subheader("DXF Summary")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total entities", sum(extracted["entity_counts"].values()))
    col2.metric("Boundary lines", len(extracted["lines"]))
    col3.metric("Bearing labels", len(extracted["bearings"]))
    col4.metric("Distance labels", len(extracted["distances"]))

    with st.expander("Layers found in DXF"):
        st.write(", ".join(extracted["layers_found"]) or "none")

    if extracted["annotations"]:
        with st.expander("Annotations"):
            for a in extracted["annotations"]:
                st.text(a)

    if extracted["warnings"]:
        for w in extracted["warnings"]:
            st.warning(w)

    # --- Pairing and computation ---
    if not extracted["bearings"]:
        st.error(
            "No bearing labels found. Check that bearing text is on the BEARINGS layer "
            "or matches format: N 45°30'00\" E"
        )
    elif not extracted["distances"]:
        st.error(
            "No distance labels found. Check that distance text is on the DISTANCES layer "
            "or matches format: 210.00'"
        )
    else:
        courses, pair_warnings = pair_and_compute(
            extracted["bearings"],
            extracted["distances"]
        )

        for w in pair_warnings:
            st.warning(w)

        if not courses:
            st.error("No courses could be computed. Check label formats.")
        else:
            result = compute_closure(courses, tolerance_ft=tolerance)

            # --- Closure result ---
            st.subheader("Closure Result")

            if result.closes:
                st.success(f"✓ CLOSES within tolerance ({tolerance} ft)")
            else:
                st.error(f"✗ DOES NOT CLOSE — error exceeds tolerance ({tolerance} ft)")

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Error of Closure", f"{result.error_of_closure:.4f} ft")
            col2.metric(
                "Precision Ratio",
                f"1:{result.precision_ratio:,.0f}" if result.precision_ratio != float("inf") else "1:∞"
            )
            col3.metric("Perimeter", f"{result.total_perimeter:.2f} ft")
            col4.metric("Courses", len(result.courses))

            col1, col2 = st.columns(2)
            col1.metric("Sum Departures", f"{result.total_departure:.4f} ft")
            col2.metric("Sum Latitudes", f"{result.total_latitude:.4f} ft")

            # --- Course table ---
            if show_courses:
                st.subheader("Course Detail")
                import pandas as pd
                rows = []
                for i, c in enumerate(result.courses):
                    rows.append({
                        "#": i + 1,
                        "Bearing": c.bearing_raw,
                        "Distance (ft)": f"{c.distance_ft:.2f}",
                        "Azimuth (°)": f"{c.azimuth:.4f}",
                        "Departure (ft)": f"{c.departure:.4f}",
                        "Latitude (ft)": f"{c.latitude:.4f}",
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True)

    # --- Raw entity dump ---
    if show_raw:
        st.subheader("Raw Entities")
        with st.expander("Entity counts by type"):
            st.json(extracted["entity_counts"])
        with st.expander("All text entities"):
            for t in extracted["raw_texts"]:
                st.text(f"[{t['layer']}] @ ({t['insert'][0]:.1f}, {t['insert'][1]:.1f})  →  {t['text']}")
        with st.expander("Boundary lines"):
            for i, l in enumerate(extracted["lines"]):
                st.text(
                    f"{i+1}: [{l['layer']}] "
                    f"({l['start'][0]:.2f},{l['start'][1]:.2f}) → "
                    f"({l['end'][0]:.2f},{l['end'][1]:.2f})  "
                    f"len={l['length']:.2f}"
                )

else:
    st.markdown("---")
    st.markdown("### No file uploaded yet.")
    st.markdown(
        "Upload a DXF file above, or use `generate_sample.py` to create a synthetic test file. "
        "The sample file creates a 5-course closed traverse with correct layer conventions."
    )
