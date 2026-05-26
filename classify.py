#!/usr/bin/env python3
"""Interactive helper to hand-classify segment disciplines (road/gravel/mtb).

Usage:
    uv run classify.py
"""

import db
import update_segments


def main() -> None:
    conn = db.connect()
    db.init(conn)

    rows = conn.execute(
        "SELECT id, name, display_location, distance_m, avg_grade "
        "FROM segments "
        "WHERE in_shutesbury=1 AND lower(activity_type)='ride' AND discipline IS NULL "
        "ORDER BY name"
    ).fetchall()

    if not rows:
        print("All in-town rides are classified.")
        conn.close()
        return

    print(f"{len(rows)} in-town ride segment(s) need classification.\n")

    classified = 0
    try:
        for row in rows:
            seg_id = row["id"]
            name = row["name"]
            location = row["display_location"]
            distance_mi = (row["distance_m"] or 0) / 1609.34
            avg_grade = row["avg_grade"]
            url = f"https://www.strava.com/segments/{seg_id}"

            print(f"\n{name}")
            print(f"  Location : {location}")
            print(f"  Distance : {distance_mi:.2f} mi")
            print(f"  Avg grade: {avg_grade}%")
            print(f"  URL      : {url}")

            while True:
                try:
                    choice = input("[r]oad / [g]ravel / [m]tb / [s]kip / [q]uit: ").strip().lower()
                except EOFError:
                    print()
                    raise KeyboardInterrupt

                if choice == "r":
                    conn.execute("UPDATE segments SET discipline=? WHERE id=?", ("road", seg_id))
                    conn.commit()
                    print(f"  -> classified as road")
                    classified += 1
                    break
                elif choice == "g":
                    conn.execute("UPDATE segments SET discipline=? WHERE id=?", ("gravel", seg_id))
                    conn.commit()
                    print(f"  -> classified as gravel")
                    classified += 1
                    break
                elif choice == "m":
                    conn.execute("UPDATE segments SET discipline=? WHERE id=?", ("mtb", seg_id))
                    conn.commit()
                    print(f"  -> classified as mtb")
                    classified += 1
                    break
                elif choice == "s":
                    break
                elif choice == "q":
                    raise KeyboardInterrupt
                # unrecognized input: re-prompt

    except KeyboardInterrupt:
        pass

    print(f"\n{classified} classified this session.")
    update_segments.export_data_json(conn)
    conn.close()


if __name__ == "__main__":
    main()
