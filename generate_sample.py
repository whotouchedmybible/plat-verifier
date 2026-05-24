"""
Synthetic DXF generator for plat verifier POC.
Creates a known closed traverse with bearing/distance labels.
All math is pre-verified to close exactly.
"""

import math
import ezdxf


def bearing_to_azimuth(quadrant: str, deg: int, min: int, sec: int) -> float:
    """Convert a quadrant bearing (e.g. N 45°30'00" E) to decimal azimuth degrees."""
    dd = deg + min / 60.0 + sec / 3600.0
    q = quadrant.upper()
    if q == "NE":
        return dd
    elif q == "SE":
        return 180.0 - dd
    elif q == "SW":
        return 180.0 + dd
    elif q == "NW":
        return 360.0 - dd
    raise ValueError(f"Unknown quadrant: {quadrant}")


def azimuth_to_vector(azimuth_deg: float, distance: float):
    """Return (departure, latitude) for a course."""
    az = math.radians(azimuth_deg)
    departure = distance * math.sin(az)
    latitude = distance * math.cos(az)
    return departure, latitude


def format_bearing(quadrant: str, deg: int, min: int, sec: int) -> str:
    ns = quadrant[0].upper()
    ew = quadrant[1].upper()
    return f"{ns} {deg:02d}°{min:02d}'{sec:02d}\" {ew}"


def generate_sample_dxf(output_path: str):
    """
    5-course closed traverse.
    Courses are defined such that sum of departures and latitudes = 0.
    Starting point: (1000.00, 1000.00)
    Layer conventions:
      BOUNDARY  - LINE entities for boundary courses
      BEARINGS  - TEXT entities for bearing labels
      DISTANCES - TEXT entities for distance labels
      ANNOTATION - TEXT entities for parcel info
    """

    # Define courses: (quadrant, deg, min, sec, distance_ft)
    # These are pre-computed to close exactly
    courses_raw = [
        ("NE", 45, 30,  0, 200.00),
        ("SE", 12, 15,  0, 180.00),
        ("SE", 78, 45,  0, 150.00),
        ("SW", 30,  0,  0, 210.00),
        ("NW", 55, 10, 30, 999.0),  # placeholder — will be computed to force closure
    ]

    # Compute first 4 courses
    points = [(1000.0, 1000.0)]
    total_dep = 0.0
    total_lat = 0.0

    computed_courses = []
    for i, (quad, d, m, s, dist) in enumerate(courses_raw[:-1]):
        az = bearing_to_azimuth(quad, d, m, s)
        dep, lat = azimuth_to_vector(az, dist)
        total_dep += dep
        total_lat += lat
        x = points[-1][0] + dep
        y = points[-1][1] + lat
        points.append((x, y))
        computed_courses.append((quad, d, m, s, dist, az, dep, lat))

    # Compute closing course to return to start
    close_dep = -total_dep
    close_lat = -total_lat
    close_dist = math.sqrt(close_dep**2 + close_lat**2)
    close_az = math.degrees(math.atan2(close_dep, close_lat)) % 360.0

    # Convert azimuth back to quadrant bearing for label
    if 0 <= close_az < 90:
        close_quad = "NE"
        close_angle = close_az
    elif 90 <= close_az < 180:
        close_quad = "SE"
        close_angle = 180.0 - close_az
    elif 180 <= close_az < 270:
        close_quad = "SW"
        close_angle = close_az - 180.0
    else:
        close_quad = "NW"
        close_angle = 360.0 - close_az

    close_deg = int(close_angle)
    close_min = int((close_angle - close_deg) * 60)
    close_sec = round(((close_angle - close_deg) * 60 - close_min) * 60)

    computed_courses.append((
        close_quad, close_deg, close_min, close_sec,
        close_dist, close_az, close_dep, close_lat
    ))
    points.append((1000.0, 1000.0))  # closes back to start

    # Build DXF
    doc = ezdxf.new(dxfversion="R2010")
    msp = doc.modelspace()

    # Define layers
    doc.layers.add("BOUNDARY", color=2)   # yellow
    doc.layers.add("BEARINGS", color=3)   # green
    doc.layers.add("DISTANCES", color=4)  # cyan
    doc.layers.add("ANNOTATION", color=7) # white

    # Add parcel annotation
    msp.add_text(
        "PARCEL: LOT 1, SEC 12, T4S R2W\nHUNTSVILLE MERIDIAN, LIMESTONE CO. AL",
        dxfattribs={
            "layer": "ANNOTATION",
            "height": 5.0,
            "insert": (1000.0, 1200.0),
        }
    )

    msp.add_text(
        "POB: IRON PIN SET",
        dxfattribs={
            "layer": "ANNOTATION",
            "height": 3.0,
            "insert": (990.0, 992.0),
        }
    )

    # Add boundary lines and labels
    for i, course in enumerate(computed_courses):
        quad, d, m, s, dist, az, dep, lat = course
        p1 = points[i]
        p2 = points[i + 1]

        # Boundary line
        msp.add_line(
            p1, p2,
            dxfattribs={"layer": "BOUNDARY"}
        )

        # Midpoint for label placement
        mid_x = (p1[0] + p2[0]) / 2.0
        mid_y = (p1[1] + p2[1]) / 2.0

        # Bearing label
        bearing_str = format_bearing(quad, d, m, s)
        msp.add_text(
            bearing_str,
            dxfattribs={
                "layer": "BEARINGS",
                "height": 3.0,
                "insert": (mid_x + 5.0, mid_y + 3.0),
            }
        )

        # Distance label
        dist_str = f"{dist:.2f}'"
        msp.add_text(
            dist_str,
            dxfattribs={
                "layer": "DISTANCES",
                "height": 3.0,
                "insert": (mid_x + 5.0, mid_y - 3.0),
            }
        )

    doc.saveas(output_path)
    print(f"Sample DXF written to {output_path}")
    print(f"Traverse has {len(computed_courses)} courses, closes exactly to POB.")
    return computed_courses, points


if __name__ == "__main__":
    courses, pts = generate_sample_dxf("sample_plat.dxf")
    print("\nCourses:")
    for i, c in enumerate(courses):
        quad, d, m, s, dist, az, dep, lat = c
        print(f"  {i+1}: {format_bearing(quad,d,m,s)}  {dist:.2f}ft  "
              f"dep={dep:.4f}  lat={lat:.4f}")
    print(f"\nFinal point: {pts[-1]}")
    print(f"POB: {pts[0]}")
