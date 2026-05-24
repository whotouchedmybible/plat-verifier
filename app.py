"""
Alabama Plat Verifier - POC
3-tab workflow:
  Tab 1 - Step 1: Upload DXF
  Tab 2 - Step 2: Input Deed
  Tab 3 - Step 3: Verification Report

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

BEARING_SYMBOLIC = re.compile(
    r"([NS])\s*(\d{1,3})[degrees°\s]+(\d{1,2})[minutes'\s]+(\d{1,2}(?:\.\d+)?)[seconds\"']?\s*([EW])",
    re.IGNORECASE
)

BEARING_SPELLED = re.compile(
    r"(north|south)\s+(\d{1,3})\s+degrees?\s+(\d{1,2})\s+minutes?\s+(\d{1,2}(?:\.\d+)?)\s+seconds?\s+(east|west)",
    re.IGNORECASE
)

DISTANCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:feet|foot|ft\.?)", re.IGNORECASE)
CHAIN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:chains?|chs?\.?)", re.IGNORECASE)
ROD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:rods?|rd\.?)", re.IGNORECASE)

THENCE_BLOCK = re.compile(
    r"thence\s+(.+?)(?=thence|point\s+of\s+beginning|beginning\.|$)",
    re.IGNORECASE | re.DOTALL
)

_NS_MAP = {"north": "N", "south": "S"}
_EW_MAP = {"east": "E", "west": "W"}
_CARDINAL_AZ = {"north": 0.0, "south": 180.0, "east": 90.0, "west": 270.0}


def parse_bearing_from_block(text: str):
    m = BEARING_SYMBOLIC.search(text)
    if m:
        ns, d, mn, s, ew = m.groups()
        dd = float(d) + float(mn) / 60.0 + float(s) / 3600.0
        quadrant = ns.upper() + ew.upper()
        return quadrant, dd, m.group(0).strip()

    m = BEARING_SPELLED.search(text)
    if m:
        ns_w, d, mn, s, ew_w = m.groups()
        ns = _NS_MAP[ns_w.lower()]
        ew = _EW_MAP[ew_w.lower()]
        dd = float(d) + float(mn) / 60.0 + float(s) / 3600.0
        raw = f"{ns} {int(float(d)):02d}d{int(float(mn)):02d}m{float(s):05.2f}s {ew}"
        return ns + ew, dd, raw

    return None


def parse_distance_from_block(text: str):
    m = DISTANCE_RE.search(text)
    if m:
        return float(m.group(1))
    m = CHAIN_RE.search(text)
    if m:
        return float(m.group(1)) * 66.0
    m = ROD_RE.search(text)
    if m:
        return float(m.group(1)) * 16.5
    nums = re.findall(r"\b(\d+(?:\.\d+)?)\b", text)
    for n in reversed(nums):
        val = float(n)
        if 5.0 < val < 10000.0:
            return val
    return None


def bearing_to_azimuth(quadrant: str, angle_deg: float) -> float:
    if quadrant == "NE": return angle_deg
    elif quadrant == "SE": return 180.0 - angle_deg
    elif quadrant == "SW": return 180.0 + angle_deg
    elif quadrant == "NW": return 360.0 - angle_deg
    raise ValueError(f"Unknown quadrant: {quadrant}")


def make_course(bearing_raw, quadrant, angle_deg, azimuth, distance_ft) -> Course:
    az_rad = math.radians(azimuth)
    return Course(
        bearing_raw=bearing_raw, distance_ft=distance_ft,
        quadrant=quadrant, degrees=angle_deg, azimuth=azimuth,
        departure=distance_ft * math.sin(az_rad),
        latitude=distance_ft * math.cos(az_rad),
    )


# ---------------------------------------------------------------------------
# Deed text parser
# ---------------------------------------------------------------------------

SKIP_PHRASES = [
    "point of beginning", "iron pin", "iron rod", "concrete monument",
    "corner", "right-of-way", "containing", "acres", "more or less",
    "said ", "along ", "to the "
]


def parse_deed_text(deed_text: str):
    courses = []
    warnings = []
    text = re.sub(r"\s+", " ", deed_text.replace("\n", " ").replace("\r", " "))
    blocks = THENCE_BLOCK.findall(text)

    if not blocks:
        warnings.append("No 'thence' calls found. Trying full-text extraction.")
        blocks = [text]

    for block in blocks:
        block = block.strip().rstrip(";").strip()
        if not block:
            continue

        parsed = parse_bearing_from_block(block)
        if parsed:
            quadrant, angle_deg, bearing_raw = parsed
            azimuth = bearing_to_azimuth(quadrant, angle_deg)
            dist = parse_distance_from_block(block)
            if dist:
                courses.append(make_course(bearing_raw, quadrant, angle_deg, azimuth, dist))
            else:
                warnings.append(f"No distance for bearing '{bearing_raw}': '{block[:60]}'")
            continue

        card_m = re.match(r"^\s*(north|south|east|west)\b(.+)?", block, re.IGNORECASE)
        if card_m:
            cardinal = card_m.group(1).lower()
            azimuth = _CARDINAL_AZ[cardinal]
            dist = parse_distance_from_block(block)
            if dist:
                courses.append(make_course(cardinal.capitalize(), cardinal[0].upper(), 0.0, azimuth, dist))
            else:
                warnings.append(f"No distance for cardinal '{cardinal}': '{block[:60]}'")
            continue

        if any(p in block.lower() for p in SKIP_PHRASES):
            continue

        warnings.append(f"Could not parse block: '{block[:80]}'")

    if not courses:
        warnings.append(
            "No courses extracted. Ensure deed uses standard format: "
            "'thence North 12 degrees 00 minutes 23 seconds West 302.60 feet'"
        )

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
        courses=courses, total_departure=total_dep, total_latitude=total_lat,
        error_of_closure=eoc, total_perimeter=perimeter, precision_ratio=precision,
        closes=eoc <= tolerance_ft, tolerance_ft=tolerance_ft
    )


# ---------------------------------------------------------------------------
# Divergence analysis
# ---------------------------------------------------------------------------

def traverse_vertices(courses):
    x, y = 0.0, 0.0
    pts = [(x, y)]
    for c in courses:
        x += c.departure
        y += c.latitude
        pts.append((x, y))
    return pts


def compute_divergence(dxf_courses, deed_courses):
    dxf_pts = traverse_vertices(dxf_courses)
    deed_pts = traverse_vertices(deed_courses)
    n = min(len(dxf_pts), len(deed_pts))
    distances = [
        math.sqrt((dxf_pts[i][0] - deed_pts[i][0])**2 +
                  (dxf_pts[i][1] - deed_pts[i][1])**2)
        for i in range(n)
    ]
    return {
        "max": max(distances),
        "avg": sum(distances) / len(distances),
        "first_course": distances[1] if len(distances) > 1 else distances[0],
        "per_vertex": list(zip(dxf_pts[:n], deed_pts[:n], distances)),
        "n": n,
    }


def divergence_verdict(max_div: float):
    if max_div < 1.0:
        return "MATCH — traverses agree within 1 ft", "success"
    elif max_div < 10.0:
        return "CLOSE — minor discrepancy, review flagged courses", "warning"
    elif max_div < 100.0:
        return "MISMATCH — significant divergence, field verification required", "error"
    else:
        return "MAJOR MISMATCH — deed and drawing do not describe the same parcel", "error"


# ---------------------------------------------------------------------------
# Traverse plot
# ---------------------------------------------------------------------------

def plot_traverse(courses_list, labels, colors, title) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.set_aspect("equal")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.set_title(title, fontsize=12, fontweight="bold")
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
        ax.plot(xs[0], ys[0], color=color, marker="*", markersize=14)
        for i, c in enumerate(courses):
            mx = (xs[i] + xs[i+1]) / 2
            my = (ys[i] + ys[i+1]) / 2
            ax.annotate(
                f"{c.bearing_raw}\n{c.distance_ft:.1f}'",
                (mx, my), fontsize=6, ha="center", color=color, alpha=0.85
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
        "lines": [], "bearings": [], "distances": [], "annotations": [],
        "warnings": [], "raw_texts": [], "layers_found": set(), "entity_counts": {}
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
                result["lines"].append({"layer": layer, "start": p1, "end": p2, "length": math.dist(p1, p2)})
            except Exception:
                pass

        elif etype == "LWPOLYLINE":
            try:
                pts = list(entity.get_points())
                for i in range(len(pts) - 1):
                    p1 = (pts[i][0], pts[i][1])
                    p2 = (pts[i+1][0], pts[i+1][1])
                    result["lines"].append({"layer": layer, "start": p1, "end": p2, "length": math.dist(p1, p2)})
                if entity.is_closed and len(pts) > 1:
                    p1 = (pts[-1][0], pts[-1][1])
                    p2 = (pts[0][0], pts[0][1])
                    result["lines"].append({"layer": layer, "start": p1, "end": p2, "length": math.dist(p1, p2)})
            except Exception:
                pass

        elif etype in ("TEXT", "MTEXT"):
            try:
                text = entity.dxf.text.strip() if etype == "TEXT" else entity.plain_mtext().strip()
                insert = (entity.dxf.insert.x, entity.dxf.insert.y)
                result["raw_texts"].append({"text": text, "layer": layer, "insert": insert})
                if layer == "BEARINGS" or BEARING_SYMBOLIC.search(text):
                    result["bearings"].append({"text": text, "layer": layer, "insert": insert})
                elif layer == "DISTANCES" or re.search(r"\d+\.\d+\s*'", text):
                    result["distances"].append({"text": text, "layer": layer, "insert": insert})
                elif layer == "ANNOTATION":
                    result["annotations"].append(text)
            except Exception:
                pass

    result["entity_counts"] = entity_counts
    result["layers_found"] = sorted(result["layers_found"])
    return result


def pair_and_compute(bearings, distances):
    courses = []
    warnings = []
    used = set()

    for b in bearings:
        bx, by = b["insert"]
        best_idx, best_d = None, float("inf")
        for i, d in enumerate(distances):
            if i in used:
                continue
            dist = math.sqrt((bx - d["insert"][0])**2 + (by - d["insert"][1])**2)
            if dist < best_d:
                best_d = dist
                best_idx = i

        if best_idx is None:
            warnings.append(f"No distance for bearing: {b['text']}")
            continue
        if best_d > 50.0:
            warnings.append(f"Bearing '{b['text']}' paired with '{distances[best_idx]['text']}' — {best_d:.1f} units apart.")

        dist_m = re.search(r"(\d+(?:\.\d+)?)", distances[best_idx]["text"])
        if not dist_m:
            warnings.append(f"Could not parse distance: {distances[best_idx]['text']}")
            continue

        used.add(best_idx)
        parsed = parse_bearing_from_block(b["text"])
        if not parsed:
            warnings.append(f"Could not parse bearing: {b['text']}")
            continue

        quadrant, angle_deg, bearing_raw = parsed
        azimuth = bearing_to_azimuth(quadrant, angle_deg)
        courses.append(make_course(bearing_raw, quadrant, angle_deg, azimuth, float(dist_m.group(1))))

    for i, d in enumerate(distances):
        if i not in used:
            warnings.append(f"Unpaired distance: {d['text']}")

    return courses, warnings


# ---------------------------------------------------------------------------
# File text extraction
# ---------------------------------------------------------------------------

def extract_text_from_file(uploaded_file):
    fname = uploaded_file.name.lower()
    file_bytes = uploaded_file.read()

    if fname.endswith(".txt"):
        return file_bytes.decode("utf-8", errors="ignore"), "plain text", None

    elif fname.endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs), "DOCX extract", None
        except Exception as e:
            return None, None, f"DOCX error: {e}"

    elif fname.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if text.strip():
                return text, "PDF text extract", None
        except Exception:
            pass
        return ocr_pdf(file_bytes)

    elif fname.endswith((".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp")):
        return ocr_image(file_bytes), "image OCR", None

    return None, None, f"Unsupported file type: {uploaded_file.name}"


def ocr_image(image_bytes: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        return pytesseract.image_to_string(img, config="--psm 6")
    except Exception as e:
        return f"OCR error: {e}"


def ocr_pdf(pdf_bytes: bytes):
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
        pages = convert_from_bytes(pdf_bytes, dpi=300)
        text = "\n".join(
            pytesseract.image_to_string(p.convert("L"), config="--psm 6")
            for p in pages
        )
        if text.strip():
            return text, f"PDF OCR ({len(pages)} pages)", None
        return None, None, "OCR produced no text — check scan quality."
    except ImportError:
        return None, None, "pdf2image or pytesseract not available."
    except Exception as e:
        return None, None, f"PDF OCR error: {e}"


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Alabama Plat Verifier — POC",
    page_icon="📐",
    layout="wide"
)

st.title("Alabama Plat Verifier")
st.caption("POC — DXF Closure + Deed Parsing + Verification Report")

# Session state
for key in ["dxf_courses", "dxf_closure", "dxf_filename", "dxf_annotations",
            "deed_courses", "deed_closure", "deed_text_preview", "deed_warnings",
            "active_tab"]:
    if key not in st.session_state:
        st.session_state[key] = None if key != "dxf_courses" and key != "deed_courses" else []

if "active_tab" not in st.session_state or st.session_state.active_tab is None:
    st.session_state.active_tab = 0

# Tab labels with status indicators
def tab_label(step, name, done):
    return f"{'✓ ' if done else ''}{step}: {name}"

dxf_done = len(st.session_state.dxf_courses) > 0
deed_done = len(st.session_state.deed_courses) > 0

tab1, tab2, tab3 = st.tabs([
    tab_label("Step 1", "DXF Boundary Closure", dxf_done),
    tab_label("Step 2", "Deed Description Input", deed_done),
    tab_label("Step 3", "Verification Report", dxf_done and deed_done),
])

SAMPLES = {
    "Todd v. Owens (1991) — Section 16, T16S R12E": (
        "Commence at the southwest corner of the Northeast Quarter of the Northwest "
        "Quarter of Section 16, Township 16 South, Range 12 East and run North 89 "
        "degrees 59 minutes 56 seconds East 209.49 feet to an iron pin which is the "
        "point of beginning; thence North 12 degrees 00 minutes 23 seconds West 302.60 "
        "feet to a point; thence North 12 degrees 40 minutes 13 seconds West 365.00 "
        "feet to a point; thence South 89 degrees 59 minutes 56 seconds West 209.49 "
        "feet to a point; thence South 0 degrees 00 minutes 04 seconds East 667.60 "
        "feet to the point of beginning."
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


# ===========================================================================
# TAB 1 — DXF Closure
# ===========================================================================

with tab1:
    st.subheader("Step 1: Upload DXF and Verify Boundary Closure")
    st.markdown(
        "Upload the CAD drawing in DXF format. The tool will extract boundary lines "
        "and bearing/distance labels, compute the traverse closure, and store the "
        "result for comparison in Step 3."
    )
    st.info(
        "**Expected layer conventions:** "
        "`BOUNDARY` — line entities | "
        "`BEARINGS` — bearing text | "
        "`DISTANCES` — distance text"
    )

    with st.sidebar:
        st.header("Settings")
        tolerance = st.number_input(
            "DXF closure tolerance (ft)",
            min_value=0.001, max_value=1.0, value=0.05, step=0.001, format="%.3f"
        )
        show_raw = st.checkbox("Show raw DXF entities", value=False)
        st.markdown("---")
        st.caption("POC only. Production tool will be F#.")

    uploaded_dxf = st.file_uploader("Upload DXF file", type=["dxf"], key="dxf_upload")

    if uploaded_dxf is not None:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp.write(uploaded_dxf.read())
            tmp_path = tmp.name

        try:
            with st.spinner("Parsing DXF..."):
                extracted = extract_from_dxf(tmp_path)
        finally:
            os.unlink(tmp_path)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total entities", sum(extracted["entity_counts"].values()))
        c2.metric("Boundary lines", len(extracted["lines"]))
        c3.metric("Bearing labels", len(extracted["bearings"]))
        c4.metric("Distance labels", len(extracted["distances"]))

        with st.expander("Layers found"):
            st.write(", ".join(extracted["layers_found"]) or "none")

        if extracted["annotations"]:
            with st.expander("Annotations"):
                for a in extracted["annotations"]:
                    st.text(a)

        for w in extracted["warnings"]:
            st.warning(w)

        if extracted["bearings"] and extracted["distances"]:
            courses, pw = pair_and_compute(extracted["bearings"], extracted["distances"])
            for w in pw:
                st.warning(w)

            if courses:
                result = compute_closure(courses, tolerance_ft=tolerance)

                if result.closes:
                    st.success(f"CLOSES within tolerance ({tolerance} ft)")
                else:
                    st.error(f"DOES NOT CLOSE — error: {result.error_of_closure:.4f} ft")

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Error of Closure", f"{result.error_of_closure:.4f} ft")
                c2.metric("Precision Ratio",
                    f"1:{result.precision_ratio:,.0f}" if result.precision_ratio != float("inf") else "1:∞")
                c3.metric("Perimeter", f"{result.total_perimeter:.2f} ft")
                c4.metric("Courses", len(courses))

                import pandas as pd
                with st.expander("Course detail"):
                    st.dataframe(pd.DataFrame([{
                        "#": i+1, "Bearing": c.bearing_raw,
                        "Distance (ft)": f"{c.distance_ft:.2f}",
                        "Azimuth": f"{c.azimuth:.4f}",
                        "Departure": f"{c.departure:.4f}",
                        "Latitude": f"{c.latitude:.4f}",
                    } for i, c in enumerate(courses)]), use_container_width=True)

                # Store in session state
                st.session_state.dxf_courses = courses
                st.session_state.dxf_closure = result
                st.session_state.dxf_filename = uploaded_dxf.name
                st.session_state.dxf_annotations = extracted["annotations"]

                st.success("DXF stored. Proceed to Step 2 to input the deed description.")

        if show_raw:
            with st.expander("Entity counts"):
                st.json(extracted["entity_counts"])
            with st.expander("All text entities"):
                for t in extracted["raw_texts"]:
                    st.text(f"[{t['layer']}] @ ({t['insert'][0]:.1f},{t['insert'][1]:.1f}) -> {t['text']}")
    else:
        st.markdown("Upload a DXF file above, or use `generate_sample.py` to create a test file.")
        if dxf_done:
            st.info(f"DXF already loaded from previous upload: {st.session_state.dxf_filename}")


# ===========================================================================
# TAB 2 — Deed Input
# ===========================================================================

with tab2:
    st.subheader("Step 2: Input Deed Description")
    st.markdown(
        "Paste or upload the legal description from the deed. "
        "The tool will extract boundary courses and compute the deed traverse. "
        "Supports PDF (text-based and scanned), DOCX, TXT, and images."
    )

    if not dxf_done:
        st.warning("Complete Step 1 first — upload and parse a DXF before proceeding.")

    input_method = st.radio(
        "Input method",
        ["Paste text", "Upload file (PDF, DOCX, TXT, image)"],
        horizontal=True
    )

    deed_text = ""

    if input_method == "Paste text":
        deed_text = st.text_area(
            "Paste deed legal description here", height=200,
            placeholder=(
                "Example:\n"
                "thence North 12 degrees 00 minutes 23 seconds West 302.60 feet; "
                "thence South 89 degrees 59 minutes 56 seconds West 209.49 feet..."
            )
        )
    else:
        deed_file = st.file_uploader(
            "Upload deed file",
            type=["pdf", "docx", "txt", "png", "jpg", "jpeg", "tiff", "tif", "bmp"],
            key="deed_upload"
        )
        if deed_file:
            with st.spinner("Extracting text..."):
                deed_text, method, err = extract_text_from_file(deed_file)
            if err:
                st.error(err)
                deed_text = ""
            elif deed_text:
                st.success(f"Extracted via {method} — {len(deed_text)} characters")
                with st.expander("Preview extracted text"):
                    st.text(deed_text[:3000] + ("..." if len(deed_text) > 3000 else ""))

    with st.expander("Load a sample Alabama deed (from public court records)"):
        sample = st.selectbox("Select sample", ["— select —"] + list(SAMPLES.keys()))
        if sample != "— select —":
            deed_text = SAMPLES[sample]
            st.text_area("Loaded deed text", value=deed_text, height=150, disabled=True)

    st.markdown("---")

    parse_clicked = st.button(
        "Parse Deed and Generate Report",
        disabled=not deed_text.strip(),
        type="primary"
    )

    if parse_clicked:
        with st.spinner("Parsing deed..."):
            deed_courses, deed_warnings = parse_deed_text(deed_text)

        for w in deed_warnings:
            st.warning(w)

        if not deed_courses:
            st.error("No courses extracted. Check deed format.")
        else:
            deed_closure = compute_closure(deed_courses, tolerance_ft=0.5)
            st.session_state.deed_courses = deed_courses
            st.session_state.deed_closure = deed_closure
            st.session_state.deed_text_preview = deed_text[:500]
            st.session_state.deed_warnings = deed_warnings
            st.success(
                f"Extracted {len(deed_courses)} courses. "
                "Report is ready — proceed to Step 3."
            )


# ===========================================================================
# TAB 3 — Verification Report
# ===========================================================================

with tab3:
    st.subheader("Step 3: Verification Report")

    dxf_courses = st.session_state.dxf_courses or []
    deed_courses = st.session_state.deed_courses or []
    dxf_closure = st.session_state.dxf_closure
    deed_closure = st.session_state.deed_closure

    if not dxf_courses and not deed_courses:
        st.info("Complete Steps 1 and 2 to generate a report.")
    else:
        import pandas as pd

        # --- Input summary ---
        st.markdown("### Inputs")
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("**DXF Drawing**")
            if dxf_courses and dxf_closure:
                fname = st.session_state.dxf_filename or "uploaded file"
                st.markdown(f"File: `{fname}`")
                if st.session_state.dxf_annotations:
                    for a in st.session_state.dxf_annotations:
                        st.caption(a)
                if dxf_closure.closes:
                    st.success(f"Closure: PASS ({dxf_closure.error_of_closure:.4f} ft error)")
                else:
                    st.error(f"Closure: FAIL ({dxf_closure.error_of_closure:.4f} ft error)")
                st.markdown(f"Courses: **{len(dxf_courses)}** | Perimeter: **{dxf_closure.total_perimeter:.2f} ft**")
            else:
                st.warning("No DXF data — complete Step 1.")

        with c2:
            st.markdown("**Deed Description**")
            if deed_courses and deed_closure:
                if st.session_state.deed_text_preview:
                    st.caption(st.session_state.deed_text_preview[:200] + "...")
                if deed_closure.closes:
                    st.success(f"Closure: PASS ({deed_closure.error_of_closure:.4f} ft error)")
                else:
                    st.warning(f"Closure: {deed_closure.error_of_closure:.2f} ft error (expected for approximate descriptions)")
                st.markdown(f"Courses: **{len(deed_courses)}** | Perimeter: **{deed_closure.total_perimeter:.2f} ft**")
            else:
                st.warning("No deed data — complete Step 2.")

        st.markdown("---")

        # --- Closure comparison ---
        st.markdown("### Closure Comparison")
        if dxf_closure and deed_closure:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("DXF Error of Closure", f"{dxf_closure.error_of_closure:.4f} ft")
            c2.metric("Deed Error of Closure", f"{deed_closure.error_of_closure:.4f} ft")
            perim_diff = abs(dxf_closure.total_perimeter - deed_closure.total_perimeter)
            c3.metric("DXF Perimeter", f"{dxf_closure.total_perimeter:.2f} ft")
            c4.metric("Deed Perimeter", f"{deed_closure.total_perimeter:.2f} ft",
                delta=f"{perim_diff:.2f} ft difference",
                delta_color="inverse" if perim_diff > 1.0 else "off")

        st.markdown("---")

        # --- Divergence analysis ---
        st.markdown("### Divergence Analysis")
        if dxf_courses and deed_courses:
            div = compute_divergence(dxf_courses, deed_courses)
            verdict_text, verdict_type = divergence_verdict(div["max"])

            if verdict_type == "success":
                st.success(f"VERDICT: {verdict_text}")
            elif verdict_type == "warning":
                st.warning(f"VERDICT: {verdict_text}")
            else:
                st.error(f"VERDICT: {verdict_text}")

            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Max Vertex Divergence", f"{div['max']:.2f} ft",
                help="Largest separation between corresponding vertices"
            )
            c2.metric(
                "Avg Vertex Divergence", f"{div['avg']:.2f} ft",
                help="Average separation across all compared vertices"
            )
            c3.metric(
                "First Course Divergence", f"{div['first_course']:.2f} ft",
                help="Separation after first course — if large, problem starts at the beginning"
            )

            with st.expander("Per-vertex divergence detail"):
                rows = []
                for i, (dpt, dept, d) in enumerate(div["per_vertex"]):
                    rows.append({
                        "Vertex": i,
                        "DXF X": f"{dpt[0]:.2f}",
                        "DXF Y": f"{dpt[1]:.2f}",
                        "Deed X": f"{dept[0]:.2f}",
                        "Deed Y": f"{dept[1]:.2f}",
                        "Divergence (ft)": f"{d:.2f}",
                        "Flag": "REVIEW" if d > 1.0 else "OK"
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.info("Both DXF and deed data required for divergence analysis.")

        st.markdown("---")

        # --- Course tables side by side ---
        st.markdown("### Course Detail")
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("**DXF Courses**")
            if dxf_courses:
                st.dataframe(pd.DataFrame([{
                    "#": i+1, "Bearing": c.bearing_raw,
                    "Dist (ft)": f"{c.distance_ft:.2f}",
                    "Dep": f"{c.departure:.2f}",
                    "Lat": f"{c.latitude:.2f}",
                } for i, c in enumerate(dxf_courses)]), use_container_width=True)

        with c2:
            st.markdown("**Deed Courses**")
            if deed_courses:
                st.dataframe(pd.DataFrame([{
                    "#": i+1, "Bearing": c.bearing_raw,
                    "Dist (ft)": f"{c.distance_ft:.2f}",
                    "Dep": f"{c.departure:.2f}",
                    "Lat": f"{c.latitude:.2f}",
                } for i, c in enumerate(deed_courses)]), use_container_width=True)

        # --- Warnings ---
        if st.session_state.deed_warnings:
            with st.expander("Deed parsing warnings"):
                for w in st.session_state.deed_warnings:
                    st.warning(w)

        st.markdown("---")

        # --- Traverse plot ---
        st.markdown("### Traverse Overlay")
        if dxf_courses and deed_courses:
            div = compute_divergence(dxf_courses, deed_courses)
            fig = plot_traverse(
                [dxf_courses, deed_courses],
                ["DXF Traverse", "Deed Traverse"],
                ["#2196F3", "#FF5722"],
                f"Deed vs DXF Overlay — Max Divergence: {div['max']:.2f} ft"
            )
        elif dxf_courses:
            fig = plot_traverse([dxf_courses], ["DXF Traverse"], ["#2196F3"], "DXF Boundary Traverse")
        elif deed_courses:
            fig = plot_traverse([deed_courses], ["Deed Traverse"], ["#FF5722"], "Deed Boundary Traverse")
        else:
            fig = None

        if fig:
            st.pyplot(fig)
            plt.close(fig)
