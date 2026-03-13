"""Shared feature computation for evaluator and feature_actor.

Single source of truth for build_extra_features(). Both the standalone
evaluator (ML scoring) and the FeatureActor (live exit monitoring) import
this to ensure identical feature sets.
"""
import numpy as np

COAST_NORMAL = {
    "los_angeles": 240, "san_francisco": 270, "miami": 90,
    "new_york": 150, "boston": 90, "seattle": 270,
    "houston": 150, "new_orleans": 180, "philadelphia": 135,
    "washington_dc": 135,
}


def build_extra_features(
    ecmwf: float | None,
    gfs: float | None,
    icon: float | None,
    city: str,
    wx: dict | None,
) -> dict[str, float]:
    """Compute all derived features: forecasts + model spread + wind_dir_offshore."""
    features: dict[str, float] = {}

    if ecmwf is not None:
        features["ecmwf_high"] = ecmwf
    if gfs is not None:
        features["gfs_high"] = gfs
    if ecmwf is not None and gfs is not None:
        features["forecast_high"] = max(ecmwf, gfs)

    temps = [t for t in (ecmwf, gfs, icon) if t is not None]
    if len(temps) >= 2:
        arr = np.array(temps)
        features["model_std"] = float(np.std(arr, ddof=0))
        features["model_range"] = float(np.ptp(arr))

    if wx:
        features.update(wx)

    coast_normal = COAST_NORMAL.get(city)
    wind_dir = (wx or {}).get("wind_dir_afternoon")
    if coast_normal is not None and wind_dir is not None:
        angle_diff = (wind_dir - coast_normal) * np.pi / 180
        features["wind_dir_offshore"] = float(np.cos(angle_diff))
    else:
        features["wind_dir_offshore"] = 0.0

    return features
