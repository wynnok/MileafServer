"""Current charging evidence shared by vehicle list and overview."""

from mileaf.probes import CAPABILITIES

CAPABILITIES["current_charging"] = {
    "drives": {"car_id": ("int2",), "start_date": ("timestamp",)},
}


def current_charging_join(as_of: str) -> str:
    """Trusted SQL parameter; c is the outer vehicle alias.

    Missing end_date alone does not prove current charging. Select the latest
    session first, then reject it if completed or superseded by driving or an
    asleep/offline transition. Do not invent an end time or a freshness cutoff.
    """
    return f"""
        LEFT JOIN LATERAL (
            SELECT latest.id, latest.start_date FROM (
                SELECT id, start_date, end_date FROM charging_processes
                WHERE car_id=c.id AND start_date <= {as_of}::timestamptz AT TIME ZONE 'UTC'
                ORDER BY start_date DESC, id DESC LIMIT 1
            ) latest
            WHERE latest.end_date IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM drives d WHERE d.car_id=c.id
                  AND d.start_date > latest.start_date
                  AND d.start_date <= {as_of}::timestamptz AT TIME ZONE 'UTC'
              )
              AND NOT EXISTS (
                SELECT 1 FROM states st WHERE st.car_id=c.id
                  AND st.state::text IN ('asleep', 'offline')
                  AND st.start_date >= latest.start_date
                  AND st.start_date <= {as_of}::timestamptz AT TIME ZONE 'UTC'
              )
        ) cp ON true
    """
