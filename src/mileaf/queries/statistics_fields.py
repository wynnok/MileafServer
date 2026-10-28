"""Optional source requirements for independently versioned statistics features."""

from typing import Literal

from pydantic import BaseModel

# Routes register only implemented features before the application's startup probe.
STATISTICS_REQUIREMENTS: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {}


def statistics_feature_supported(columns: frozenset[tuple[str, str]], feature: str) -> bool:
    requirements = STATISTICS_REQUIREMENTS.get(feature)
    return requirements is not None and all(
        (table, column) in columns for table, fields in requirements.items() for column in fields
    )


class StatisticsCapabilities(BaseModel):
    contract_version: Literal["statistics-insights-v1"] = "statistics-insights-v1"
    minimum_server_version: Literal["0.3.0"] = "0.3.0"
    tested_schema_version: int = 20260808090000
    features: dict[str, bool]


def statistics_capabilities(columns: frozenset[tuple[str, str]]) -> StatisticsCapabilities:
    return StatisticsCapabilities(
        features={
            feature: statistics_feature_supported(columns, feature)
            for feature in (
                "driving_coverage",
                "driving_analysis",
                "range_observations",
                "parking_observations",
            )
        }
    )
