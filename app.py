"""
Alabama Plat Verifier - POC
Streamlit app with two tabs:
  1. DXF Closure Verification
  2. Deed Parser (PDF/DOCX/text -> traverse -> plot -> overlay)

Python/Streamlit POC only - production will be F#.
"""

import math
import re
import io
import os
import tempfile
import streamlit as st
import ezdxf
from ezdxf import recover
from dataclasses import dataclass
from typing import Optional
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass
class Course:
    bearing_raw: str
    distance_ft: float
    quadrant: str
    degrees: float
    azimuth: float
    departure: float
    latitude: float


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
# Bearing / distance parsing
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
# Traverse plot
# ---------------------------------------------------------------------------

def plot_traverse(courses_list: list, labels: list, colors: list, title: str) -> plt.Figure:
    """
    Plot one or more traverses on the same axes.
    courses_list: list of Course lists
    labels: list of legend labels
    colors: list of colors
    """
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_aspect("equal")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.set_title(title, fontsize=12)
    ax.set_xlabel("Departure (ft)")
    ax.set_ylabel("Latitude (ft)")

    patches = []
    for courses, label, color in zip(courses_list, labels, colors):
        if not courses:
            continue
        x, y = 0.0, 0.0
        xs, ys = [x], [y]
        for c in courses:
            x += c.departure
            y += c.latitude
            xs.append(x)
            ys.append(y)

        ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=4)
        ax.plot(xs[0], ys[0], color=color, marker="*", markersize=12)

        # Label midpoints of each course
        for i, c in enumerate(courses):
            mx = (xs[i] + xs[i+1]) / 2
            my = (ys[i] + ys[i+1]) / 2
            ax.annotate(
                f"{c.bearing_raw}\n{c.distance_ft:.1f}'",
                (mx, my),
                fontsize=6,
                ha="center",
                color=color,
                alpha=0.8
            )

        patches.append(mpatches.Patch(color=color, label=label))

    if patches:
        ax.legend(handles=patches, loc="best", fontsize=9)

    plt.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# DXF extraction
# ---------------------------------------------------------------------------

def extract_from_dxf(filepath: str) -> dict:
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
        doc, auditor = recover.readfile(filepath)
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
                    "layer": layer, "start": p1, "end": p2,
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
                        "layer": layer, "start": p1, "end": p2,
                        "length": math.dist(p1, p2)
                    })
                if entity.is_closed and len(pts) > 1:
                    p1 = (pts[-1][0], pts[-1][1])
                    p2 = (pts[0][0], pts[0][1])
                    result["lines"].append({
                        "layer": layer, "start": p1, "end": p2,
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
                    "text": text, "layer": layer, "insert": insert
                })

                if layer == "BEARINGS" or BEARING_PATTERN.search(text):
                    result["bearings"].append({
                        "text": text, "layer": layer, "insert": insert
                    })
                elif layer == "DISTANCES" or re.search(r"\d+\.\d+\s*'", text):
                    result["distances"].append({
                        "text": text, "layer": layer, "insert": insert
                    })
                elif layer == "ANNOTATION":
                    result["annotations"].append(text)
            except Exception:
                pass

    result["entity_counts"] = entity_counts
    result["layers_found"] = sorted(result["layers_found"])
    return result


def pair_and_compute(bearings: list, distances: list) -> tuple:
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
                f"Bearing '{b['text']}' paired with distance "
                f"'{distances[best_idx]['text']}' — {best_dist:.1f} units apart, verify pairing."
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
# Text extraction from deed files
# ---------------------------------------------------------------------------

def extract_text_from_file(uploaded_file) -> tuple:
    """
    Returns (text, error_message).
    Handles PDF, DOCX, and plain text.
    """
    fname = uploaded_file.name.lower()

    if fname.endswith(".txt"):
        return uploaded_file.read().decode("utf-8", errors="ignore"), None

    elif fname.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(uploaded_file.read()))
            text = "\n".join(
                page.extract_text() or "" for page in reader.pages
            )
            if not text.strip():
                return None, (
                    "PDF appears to be a scanned image — no selectable text found. "
                    "Use the paste option below instead, or upload a text-based PDF."
                )
            return text, None
        except Exception as e:
            return None, f"PDF read error: {e}"

    elif fname.endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(uploaded_file.read()))
            text = "\n".join(p.text for p in doc.paragraphs)
            return text, None
        except Exception as e:
            return None, f"DOCX read error: {e}"

    else:
        return None, f"Unsupported file type: {uploaded_file.name}"


# ---------------------------------------------------------------------------
# Claude API deed parser
# ---------------------------------------------------------------------------

def parse_deed_with_claude(deed_text: str, api_key: str) -> tuple:
    """
    Send deed text to Claude, get back structured courses.
    Returns (courses list, raw_response, error).
    """
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        system_prompt = """You are an expert Alabama land surveyor assistant.
Your job is to extract boundary courses from deed legal descriptions.

Extract every bearing+distance course from the deed text provided.
Return ONLY a JSON array, no other text, no markdown, no explanation.

Each element must have exactly these fields:
  "bearing": bearing string exactly as written in the deed (e.g. "N 45°30'00\" E")
  "distance_ft": distance as a float in feet (convert chains: 1 chain = 66 ft, 1 rod = 16.5 ft)
  "monument": any monument or adjoint call for this course endpoint, or null
  "notes": any ambiguity or issue with this course, or null

Example output:
[
  {"bearing": "N 89°59'56\" E", "distance_ft": 209.49, "monument": "iron pin", "notes": null},
  {"bearing": "N 12°00'23\" W", "distance_ft": 302.60, "monument": null, "notes": null}
]

If a course has a direction but no explicit bearing (e.g. "thence West 420 feet"),
convert it: West = S 90°00'00" W is wrong, use N 90°00'00" W. 
Actually represent cardinal directions as:
  North = N 00°00'00" E
  South = S 00°00'00" E  
  East = N 90°00'00" E (use S 89°59'59\" E as approximation is wrong — use "EAST" in bearing field and flag in notes)
  West = similar

For cardinal-only directions, set bearing to the cardinal word and note the ambiguity.

Do not include the POC-to-POB lead-in course unless it is a boundary course.
Only return the JSON array."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=system_prompt,
            messages=[
                {"role": "user", "content": f"Extract boundary courses from this deed:\n\n{deed_text}"}
            ]
        )

        raw = message.content[0].text.strip()

        import json
        # Strip markdown fences if present
        clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        parsed = json.loads(clean)

        courses = []
        warnings = []
        for item in parsed:
            bearing = item.get("bearing", "")
            dist = item.get("distance_ft")
            monument = item.get("monument")
            notes = item.get("notes")

            if notes:
                warnings.append(f"Course '{bearing}': {notes}")
            if monument:
                warnings.append(f"Monument at end of '{bearing}': {monument}")

            if dist is None:
                warnings.append(f"No distance for course: {bearing}")
                continue

            course = compute_course(bearing, float(dist))
            if course:
                courses.append(course)
            else:
                warnings.append(f"Could not compute course from bearing: '{bearing}'")

        return courses, warnings, raw, None

    except Exception as e:
        return [], [], "", f"Claude API error: {e}"


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Alabama Plat Verifier — POC",
    page_icon="📐",
    layout="wide"
)

st.title("Alabama Plat Verifier")
st.caption("POC — Boundary Closure Verification + Deed Parser")

tab1, tab2 = st.tabs(["DXF Closure Verification", "Deed Parser"])


# ===========================================================================
# TAB 1: DXF Closure
# ===========================================================================

with tab1:
    st.info(
        "**Expected DXF layer conventions:** "
        "`BOUNDARY` for line entities, "
        "`BEARINGS` for bearing text labels, "
        "`DISTANCES` for distance text labels."
    )

    with st.sidebar:
        st.header("Settings")
        tolerance = st.number_input(
            "Closure tolerance (ft)",
            min_value=0.001, max_value=1.0,
            value=0.05, step=0.001, format="%.3f"
        )
        show_raw = st.checkbox("Show raw DXF entities", value=False)
        show_courses = st.checkbox("Show course detail", value=True)
        st.markdown("---")
        st.caption("POC only. Production tool will be F#.")

    uploaded_dxf = st.file_uploader("Upload DXF file", type=["dxf"], key="dxf_upload")

    dxf_courses = []  # used for overlay in tab 2

    if uploaded_dxf is not None:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp.write(uploaded_dxf.read())
            tmp_path = tmp.name

        try:
            with st.spinner("Parsing DXF..."):
                extracted = extract_from_dxf(tmp_path)
        finally:
            os.unlink(tmp_path)

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

        for w in extracted["warnings"]:
            st.warning(w)

        if not extracted["bearings"]:
            st.error("No bearing labels found. Check BEARINGS layer or label format.")
        elif not extracted["distances"]:
            st.error("No distance labels found. Check DISTANCES layer or label format.")
        else:
            courses, pair_warnings = pair_and_compute(
                extracted["bearings"], extracted["distances"]
            )
            for w in pair_warnings:
                st.warning(w)

            if not courses:
                st.error("No courses could be computed. Check label formats.")
            else:
                dxf_courses = courses
                result = compute_closure(courses, tolerance_ft=tolerance)

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

                st.subheader("Traverse Plot")
                fig = plot_traverse(
                    [courses], ["DXF Traverse"], ["#2196F3"],
                    "DXF Boundary Traverse"
                )
                st.pyplot(fig)
                plt.close(fig)

        if show_raw:
            st.subheader("Raw Entities")
            with st.expander("Entity counts by type"):
                st.json(extracted["entity_counts"])
            with st.expander("All text entities"):
                for t in extracted["raw_texts"]:
                    st.text(
                        f"[{t['layer']}] @ ({t['insert'][0]:.1f}, {t['insert'][1]:.1f})"
                        f"  →  {t['text']}"
                    )
            with st.expander("Boundary lines"):
                for i, l in enumerate(extracted["lines"]):
                    st.text(
                        f"{i+1}: [{l['layer']}] "
                        f"({l['start'][0]:.2f},{l['start'][1]:.2f}) → "
                        f"({l['end'][0]:.2f},{l['end'][1]:.2f})  "
                        f"len={l['length']:.2f}"
                    )
    else:
        st.markdown("Upload a DXF file above, or use `generate_sample.py` to create a test file.")


# ===========================================================================
# TAB 2: Deed Parser
# ===========================================================================

with tab2:
    st.subheader("Deed Description Parser")
    st.markdown(
        "Upload a deed document or paste the legal description. "
        "Claude will extract the boundary courses, plot the traverse, "
        "and optionally overlay it against a DXF traverse from Tab 1."
    )

    # API key input
    api_key = st.text_input(
        "Anthropic API Key",
        type="password",
        help="Your Anthropic API key. Not stored anywhere."
    )

    st.markdown("---")

    # Input method
    input_method = st.radio(
        "Deed input method",
        ["Paste text", "Upload file (PDF, DOCX, TXT)"],
        horizontal=True
    )

    deed_text = ""

    if input_method == "Paste text":
        deed_text = st.text_area(
            "Paste deed legal description here",
            height=200,
            placeholder=(
                "Example:\n"
                "Commence at the southwest corner of the Northeast Quarter of the "
                "Northwest Quarter of Section 16, Township 16 South, Range 12 East "
                "and run North 89 degrees 59 minutes 56 seconds East 209.49 feet to "
                "an iron pin which is the point of beginning; thence North 12 degrees "
                "00 minutes 23 seconds West 302.60 feet to a point..."
            )
        )

    else:
        deed_file = st.file_uploader(
            "Upload deed file",
            type=["pdf", "docx", "txt"],
            key="deed_upload"
        )
        if deed_file:
            with st.spinner("Extracting text..."):
                deed_text, err = extract_text_from_file(deed_file)
            if err:
                st.error(err)
                deed_text = ""
            elif deed_text:
                st.success(f"Text extracted — {len(deed_text)} characters")
                with st.expander("Preview extracted text"):
                    st.text(deed_text[:2000] + ("..." if len(deed_text) > 2000 else ""))

    # Sample deeds
    with st.expander("Load a sample Alabama deed (from public court records)"):
        sample = st.selectbox("Select sample", [
            "— select —",
            "Todd v. Owens (1991) — Section 16, T16S R12E",
            "Jefferson County v. McClinton (1974) — Sec 13, T19S R3W",
            "Jim Walter Homes v. Phifer (1983) — Sec 9, T14N R16E",
            "Sandlin v. Sanders (1978) — Sec 31, T13S R3W",
        ])

        samples = {
            "Todd v. Owens (1991) — Section 16, T16S R12E": (
                "Commence at the southwest corner of the Northeast Quarter of the Northwest "
                "Quarter of Section 16, Township 16 South, Range 12 East and run North 89 "
                "degrees 59 minutes 56 seconds East 209.49 feet to an iron pin which is the "
                "point of beginning of this property line; thence North 12 degrees 00 minutes "
                "23 seconds West 302.60 feet to a point; thence North 12 degrees 40 minutes "
                "13 seconds West 365.00 feet to a point; thence South 89 degrees 59 minutes "
                "56 seconds West 209.49 feet to a point; thence South 0 degrees 00 minutes "
                "04 seconds East 667.60 feet to the point of beginning."
            ),
            "Jefferson County v. McClinton (1974) — Sec 13, T19S R3W": (
                "Beginning at the N.E. corner of the S.W. 1/4 of the S.E. 1/4 of Sec. 13, "
                "Tp. 19 S., R. 3 W. thence West 420 feet, thence South 51 degrees 00 minutes "
                "West 610 feet, thence North 39 degrees West 480 feet to the north boundary "
                "line of said S.W. 1/4 of S.E. 1/4, thence West 98 feet to the N.W. corner "
                "of said S.W. 1/4 of S.E. 1/4, thence South 510 feet more or less to corner "
                "of Fred Greer one acre tract, thence North 51 degrees 00 minutes East along "
                "the north boundary line of said one acre lot 320 feet, thence South 135 feet "
                "more or less to the north boundary line of highway right-of-way, thence "
                "North 51 degrees East along said right-of-way line to point of beginning, "
                "containing 2.8 acres more or less, situated in Jefferson County, Alabama."
            ),
            "Jim Walter Homes v. Phifer (1983) — Sec 9, T14N R16E": (
                "Begin at a point 792 ft. south of the northwest corner of the SE/4 of the "
                "NE/4 of Sec. 9, Twp. 14N, Rge. 16E, Lowndes County, Alabama, thence South "
                "298 ft., thence East 660 ft., thence North 298 ft., thence West 660 ft. to "
                "the point of beginning and containing three acres, more or less."
            ),
            "Sandlin v. Sanders (1978) — Sec 31, T13S R3W": (
                "A part of the NW 1/4 of the SE 1/4 of Section 31, Township 13 South, Range "
                "3 West, Blount County, Alabama, more particularly described as follows: "
                "Begin at the NW corner of said NW 1/4 of the SE 1/4 thence South 0 degrees "
                "48 minutes 49 seconds East 350 feet; thence North 89 degrees 33 minutes 11 "
                "seconds East 140 feet; thence North 0 degrees 48 minutes 49 seconds West "
                "140 feet to the beginning. Containing 1.12 acres, more or less."
            ),
        }

        if sample != "— select —":
            deed_text = samples[sample]
            st.text_area("Loaded deed text", value=deed_text, height=150, disabled=True)

    st.markdown("---")

    # Parse button
    parse_clicked = st.button(
        "Parse Deed with Claude",
        disabled=(not deed_text.strip() or not api_key.strip()),
        type="primary"
    )

    if parse_clicked:
        with st.spinner("Sending to Claude API..."):
            deed_courses, deed_warnings, raw_response, error = parse_deed_with_claude(
                deed_text, api_key
            )

        if error:
            st.error(error)
        else:
            for w in deed_warnings:
                st.warning(w)

            if not deed_courses:
                st.error("No courses extracted. Check deed text or API response below.")
            else:
                st.success(f"Extracted {len(deed_courses)} courses from deed.")

                # Closure result
                result = compute_closure(deed_courses, tolerance_ft=0.5)
                st.subheader("Deed Traverse Closure")

                if result.closes:
                    st.success(f"✓ CLOSES within 0.5 ft tolerance")
                else:
                    st.warning(
                        f"Does not close to 0.5 ft — error: {result.error_of_closure:.2f} ft. "
                        "This may be expected for cardinal-direction or approximate deed descriptions."
                    )

                col1, col2, col3 = st.columns(3)
                col1.metric("Error of Closure", f"{result.error_of_closure:.4f} ft")
                col2.metric(
                    "Precision Ratio",
                    f"1:{result.precision_ratio:,.0f}" if result.precision_ratio != float("inf") else "1:∞"
                )
                col3.metric("Perimeter", f"{result.total_perimeter:.2f} ft")

                # Course table
                import pandas as pd
                st.subheader("Extracted Courses")
                rows = []
                for i, c in enumerate(deed_courses):
                    rows.append({
                        "#": i + 1,
                        "Bearing": c.bearing_raw,
                        "Distance (ft)": f"{c.distance_ft:.2f}",
                        "Departure (ft)": f"{c.departure:.4f}",
                        "Latitude (ft)": f"{c.latitude:.4f}",
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True)

                # Plot
                st.subheader("Traverse Plot")

                # Check if DXF courses available for overlay
                # Re-read from session state workaround — store in session
                if "dxf_courses" not in st.session_state:
                    st.session_state.dxf_courses = []

                overlay_available = len(st.session_state.dxf_courses) > 0

                if overlay_available:
                    fig = plot_traverse(
                        [st.session_state.dxf_courses, deed_courses],
                        ["DXF Traverse", "Deed Traverse"],
                        ["#2196F3", "#FF5722"],
                        "Deed vs DXF Traverse Overlay"
                    )
                else:
                    fig = plot_traverse(
                        [deed_courses],
                        ["Deed Traverse"],
                        ["#FF5722"],
                        "Deed Boundary Traverse"
                    )
                    st.info(
                        "Upload and parse a DXF in Tab 1 to enable overlay comparison."
                    )

                st.pyplot(fig)
                plt.close(fig)

            with st.expander("Raw Claude API response"):
                st.code(raw_response, language="json")

    # Store DXF courses in session state when computed in tab 1
    # This is a Streamlit limitation workaround
    if uploaded_dxf is not None and dxf_courses:
        st.session_state.dxf_courses = dxf_courses
