"""City-specific climate exit rules.

Pure functions and data structures -- no NautilusTrader dependency.
These are used by FeatureActor to evaluate whether climate conditions
warrant a DangerAlert for held positions.

Each rule returns alert dicts with:
    rule_name: str
    alert_level: "WATCH" | "CAUTION" | "CRITICAL"
    points: int (1-3, matches exit_monitor.py convention)
    reason: str
    features: dict[str, float]
"""
from __future__ import annotations

import math


class CityFeatureState:
    """Accumulates features from multiple climate data sources for one city.

    Each source updates its own features. The state tracks the most recent
    value for each feature key, regardless of source.
    """

    def __init__(self):
        self._features: dict[str, float] = {}
        self._source_keys: dict[str, set[str]] = {}  # source -> set of feature keys

    def update(self, source: str, features: dict[str, float]):
        """Update features from a specific source."""
        for k, v in features.items():
            if v is None:
                continue
            if isinstance(v, float) and math.isnan(v):
                continue
            if isinstance(v, (int, float)):
                self._features[k] = v
                self._source_keys.setdefault(source, set()).add(k)

    def get(self, key: str) -> float | None:
        """Get a feature value, or None if not available."""
        return self._features.get(key)

    def snapshot(self) -> dict[str, float]:
        """Return a copy of all current features."""
        return dict(self._features)

    def sources(self) -> set[str]:
        """Return set of sources that have contributed features."""
        return set(self._source_keys.keys())


# --- City groupings for rule applicability ---

COASTAL_SEA_BREEZE = {
    "boston", "chicago", "miami", "houston", "los_angeles",
    "san_francisco", "seattle", "philadelphia",
}
CAD_CITIES = {"atlanta", "washington_dc", "philadelphia", "new_york"}
DRYLINE_CITIES = {"dallas", "oklahoma_city", "austin", "san_antonio"}
MARINE_LAYER_CITIES = {"los_angeles", "san_francisco", "seattle"}
SNOW_CITIES = {"minneapolis", "chicago", "denver", "boston"}
MONSOON_CITIES = {"phoenix", "las_vegas"}
HUMIDITY_CEILING_CITIES = {"houston", "miami", "new_orleans"}

# 500mb ridge thresholds (dam) -- city-specific
RIDGE_THRESHOLDS = {
    "phoenix": 594,
    "seattle": 588,
    "dallas": 588,
    "las_vegas": 594,
}


def check_exit_rules(
    city: str,
    features: dict[str, float],
    positions: dict[str, dict],
) -> list[dict]:
    """Evaluate all applicable exit rules for a city.

    Returns list of alert dicts, each with:
        rule_name, alert_level, points, reason, features, tickers

    Only checks rules relevant to the city. Only returns alerts
    for tickers that have active positions in the given city.

    Args:
        city: City identifier (e.g. "chicago", "miami").
        features: Current feature values for this city.
        positions: ticker -> {"side": "no", "threshold": 55.0, "city": "chicago"}.
    """
    alerts: list[dict] = []

    # Find tickers for this city
    city_tickers = [t for t, info in positions.items() if info.get("city") == city]
    if not city_tickers:
        return alerts

    # Rule 1: obs_approaching_threshold (ALL cities)
    obs_temp = features.get("obs_temp")
    if obs_temp is not None:
        for ticker in city_tickers:
            pos_info = positions[ticker]
            threshold = pos_info.get("threshold")
            side = pos_info.get("side", "no")
            if threshold is not None:
                # For NO side: danger = temp approaching threshold from below
                if side == "no":
                    margin = threshold - obs_temp
                else:
                    margin = obs_temp - threshold

                if margin <= 0:
                    alerts.append({
                        "rule_name": "obs_approaching_threshold",
                        "alert_level": "CRITICAL",
                        "points": 3,
                        "reason": (
                            f"Obs {obs_temp:.1f}F BREACHED threshold "
                            f"{threshold}F (margin={margin:.1f}F)"
                        ),
                        "features": {"obs_temp": obs_temp, "threshold": threshold},
                        "tickers": [ticker],
                    })
                elif margin <= 1.0:
                    alerts.append({
                        "rule_name": "obs_approaching_threshold",
                        "alert_level": "CRITICAL",
                        "points": 3,
                        "reason": (
                            f"Obs {obs_temp:.1f}F within 1F of threshold "
                            f"{threshold}F (margin={margin:.1f}F)"
                        ),
                        "features": {"obs_temp": obs_temp, "threshold": threshold},
                        "tickers": [ticker],
                    })

    # Rule 2: sst_land_contrast_collapse (coastal cities)
    if city in COASTAL_SEA_BREEZE:
        sst = features.get("sst_value")
        forecast_high = features.get("forecast_high")
        if sst is not None and forecast_high is not None:
            contrast = forecast_high - sst
            if contrast < 5.0:
                alerts.append({
                    "rule_name": "sst_land_contrast_collapse",
                    "alert_level": "CAUTION",
                    "points": 2,
                    "reason": (
                        f"Land-sea contrast collapsed to {contrast:.1f}F "
                        f"(< 5F threshold)"
                    ),
                    "features": {
                        "sst_value": sst,
                        "forecast_high": forecast_high,
                        "contrast": contrast,
                    },
                    "tickers": city_tickers,
                })

    # Rule 3: inversion_weakening (marine layer cities)
    if city in MARINE_LAYER_CITIES:
        inv_strength = features.get("inversion_strength_c")
        if inv_strength is not None and inv_strength < 3.0:
            alerts.append({
                "rule_name": "inversion_weakening",
                "alert_level": "CAUTION",
                "points": 2,
                "reason": (
                    f"Inversion strength {inv_strength:.1f}C < 3.0C "
                    f"-- marine layer may burn off"
                ),
                "features": {"inversion_strength_c": inv_strength},
                "tickers": city_tickers,
            })

    # Rule 4: cad_eroding (CAD cities)
    if city in CAD_CITIES:
        cad_flag = features.get("afd_cad_flag")
        if cad_flag is not None and cad_flag == 0:
            wind_dir = features.get("wind_dir_afternoon")
            if wind_dir is not None:
                # SW wind (180-270) after CAD = wedge breaking
                if 180 <= wind_dir <= 270:
                    alerts.append({
                        "rule_name": "cad_eroding",
                        "alert_level": "CAUTION",
                        "points": 2,
                        "reason": (
                            f"CAD flag dropped + SW wind ({wind_dir:.0f}) "
                            f"-- wedge eroding"
                        ),
                        "features": {
                            "afd_cad_flag": cad_flag,
                            "wind_dir_afternoon": wind_dir,
                        },
                        "tickers": city_tickers,
                    })

    # Rule 5: dryline_passage (dryline cities)
    if city in DRYLINE_CITIES:
        dew_drop = features.get("dew_point_drop_3h")
        if dew_drop is not None and dew_drop > 15.0:
            alerts.append({
                "rule_name": "dryline_passage",
                "alert_level": "CAUTION",
                "points": 2,
                "reason": (
                    f"Dew point drop {dew_drop:.1f}F in 3h > 15F "
                    f"-- dryline passage"
                ),
                "features": {"dew_point_drop_3h": dew_drop},
                "tickers": city_tickers,
            })

    # Rule 6: monsoon_break (monsoon cities)
    if city in MONSOON_CITIES:
        cape = features.get("cape_max")
        humidity = features.get("humidity_afternoon")
        if cape is not None and humidity is not None:
            if cape < 500 and humidity < 15:
                alerts.append({
                    "rule_name": "monsoon_break",
                    "alert_level": "CAUTION",
                    "points": 2,
                    "reason": (
                        f"CAPE {cape:.0f} < 500 + humidity {humidity:.0f}% "
                        f"< 15% -- monsoon break, extreme heat"
                    ),
                    "features": {
                        "cape_max": cape,
                        "humidity_afternoon": humidity,
                    },
                    "tickers": city_tickers,
                })

    # Rule 7: ridge_intensifying (cities with thresholds)
    if city in RIDGE_THRESHOLDS:
        geo_500 = features.get("geopotential_500_max")
        if geo_500 is not None:
            threshold_dam = RIDGE_THRESHOLDS[city]
            # Open-Meteo returns m^2/s^2; divide by 9.80665 for gpm, /10 for dam
            geo_dam = geo_500 / 98.0665 if geo_500 > 10000 else geo_500
            if geo_dam > threshold_dam:
                alerts.append({
                    "rule_name": "ridge_intensifying",
                    "alert_level": "CAUTION",
                    "points": 2,
                    "reason": (
                        f"500mb height {geo_dam:.0f}dam > "
                        f"{threshold_dam}dam threshold"
                    ),
                    "features": {
                        "geopotential_500_max": geo_500,
                        "threshold_dam": threshold_dam,
                    },
                    "tickers": city_tickers,
                })

    # Rule 8: snow_melt_clear_sky (snow cities)
    if city in SNOW_CITIES:
        snow = features.get("snow_cover")
        cloud = features.get("cloud_cover_afternoon")
        if snow is not None and cloud is not None:
            if snow == 0 and cloud < 20:
                alerts.append({
                    "rule_name": "snow_melt_clear_sky",
                    "alert_level": "WATCH",
                    "points": 1,
                    "reason": (
                        f"Snow cover cleared + cloud {cloud:.0f}% < 20% "
                        f"-- cold bias removed"
                    ),
                    "features": {
                        "snow_cover": snow,
                        "cloud_cover_afternoon": cloud,
                    },
                    "tickers": city_tickers,
                })

    # Rule 9: marine_layer_burnoff (marine layer cities)
    if city in MARINE_LAYER_CITIES:
        cloud_base = features.get("cloud_base_ft")
        if cloud_base is not None and cloud_base > 2000:
            alerts.append({
                "rule_name": "marine_layer_burnoff",
                "alert_level": "CAUTION",
                "points": 2,
                "reason": (
                    f"Cloud base {cloud_base:.0f}ft > 2000ft "
                    f"-- marine layer burning off"
                ),
                "features": {"cloud_base_ft": cloud_base},
                "tickers": city_tickers,
            })

    # Rule 10: humidity_ceiling_removed (humid cities)
    if city in HUMIDITY_CEILING_CITIES:
        dp_departure = features.get("dew_point_departure_30d")
        if dp_departure is not None and dp_departure < -10:
            alerts.append({
                "rule_name": "humidity_ceiling_removed",
                "alert_level": "CAUTION",
                "points": 2,
                "reason": (
                    f"Dew point {dp_departure:.1f}F below 30d mean "
                    f"-- humidity ceiling removed"
                ),
                "features": {"dew_point_departure_30d": dp_departure},
                "tickers": city_tickers,
            })

    # Rule 11: forecast_drift_adverse (ALL cities)
    entry_margin = features.get("entry_margin")
    current_margin = features.get("current_margin")
    if (
        entry_margin is not None
        and current_margin is not None
        and entry_margin > 0
    ):
        shrinkage_pct = (entry_margin - current_margin) / entry_margin
        if shrinkage_pct > 0.5:
            alerts.append({
                "rule_name": "forecast_drift_adverse",
                "alert_level": "CAUTION",
                "points": 2,
                "reason": (
                    f"Margin shrunk {shrinkage_pct:.0%} "
                    f"(entry={entry_margin:.1f}F -> current={current_margin:.1f}F)"
                ),
                "features": {
                    "entry_margin": entry_margin,
                    "current_margin": current_margin,
                },
                "tickers": city_tickers,
            })

    return alerts


def compute_danger_points(alerts: list[dict]) -> tuple[int, int]:
    """Compute total danger points and number of distinct sources.

    Returns (total_points, num_sources).
    Uses the same corroboration logic as exit_monitor.py:
    EXIT requires 5+ points from 2+ distinct rule sources.
    """
    total = sum(a["points"] for a in alerts)
    sources = len({a["rule_name"] for a in alerts})
    return total, sources


def should_exit(alerts: list[dict], exit_threshold: int = 5) -> bool:
    """Determine if alerts warrant an EXIT signal.

    Requires:
    - total points >= exit_threshold (default 5)
    - alerts from 2+ distinct rules (corroboration)
    """
    total, sources = compute_danger_points(alerts)
    return total >= exit_threshold and sources >= 2
