"""Verify evaluator and feature_actor use the same shared feature computation.

After the shared_features extraction, both modules import build_extra_features
from the same source — parity is guaranteed by construction. These tests verify
the wiring is correct and the shared function works through both import paths.
"""
import sys
import os
import pytest
import numpy as np

# Ensure kalshi_weather_ml is importable
_KW_ROOT = os.environ.get("KALSHI_WEATHER_ROOT", "/home/mike/code/altmarkets/kalshi-weather")
sys.path.insert(0, f"{_KW_ROOT}/src")

from shared_features import build_extra_features, COAST_NORMAL


# Test cases: representative cities covering coast/inland, with/without ICON
TEST_CASES = [
    # (ecmwf, gfs, icon, city, wx_dict)
    (55.0, 53.0, 54.0, "chicago", {"wind_speed_afternoon": 10.0, "wind_dir_afternoon": 180.0}),
    (72.0, 70.0, None, "miami", {"wind_speed_afternoon": 8.0, "wind_dir_afternoon": 90.0}),
    (65.0, 67.0, 66.0, "new_york", {"wind_speed_afternoon": 15.0, "wind_dir_afternoon": 270.0}),
    (80.0, 78.0, 79.0, "houston", {}),
    (45.0, 48.0, 46.0, "boston", {"wind_dir_afternoon": 45.0}),
    (60.0, 60.0, 60.0, "los_angeles", {"wind_speed_afternoon": 5.0, "wind_dir_afternoon": 200.0}),
    # Cities that DON'T have coast normals (inland)
    (55.0, 57.0, 56.0, "denver", {"wind_dir_afternoon": 300.0}),
    (70.0, 68.0, 69.0, "atlanta", {"wind_speed_afternoon": 12.0}),
    # Edge cases
    (50.0, 50.0, 50.0, "chicago", {}),  # identical forecasts
    (None, 55.0, None, "chicago", {}),   # missing ecmwf
    (55.0, None, None, "chicago", {}),   # missing gfs
]


class TestSharedImportWiring:
    """Verify both modules import from shared_features."""

    def test_evaluator_imports_shared(self):
        """evaluator.build_extra_features IS shared_features.build_extra_features."""
        import evaluator
        assert evaluator.build_extra_features is build_extra_features

    def test_feature_actor_imports_shared(self):
        """feature_actor.build_extra_features IS shared_features.build_extra_features."""
        import feature_actor
        assert feature_actor.build_extra_features is build_extra_features


class TestFeatureComputation:
    """Verify build_extra_features produces correct output for representative cases."""

    @pytest.mark.parametrize("ecmwf,gfs,icon,city,wx", TEST_CASES)
    def test_features_computed(self, ecmwf, gfs, icon, city, wx):
        features = build_extra_features(ecmwf, gfs, icon, city, wx)

        # Basic sanity
        if ecmwf is not None:
            assert features["ecmwf_high"] == ecmwf
        if gfs is not None:
            assert features["gfs_high"] == gfs
        if ecmwf is not None and gfs is not None:
            assert features["forecast_high"] == max(ecmwf, gfs)

        # Model spread requires 2+ temps
        temps = [t for t in (ecmwf, gfs, icon) if t is not None]
        if len(temps) >= 2:
            assert "model_std" in features
            assert "model_range" in features
        else:
            assert "model_std" not in features

        # wind_dir_offshore always present
        assert "wind_dir_offshore" in features


class TestScoreParity:
    """Verify score_opportunities works with shared features."""

    def test_score_parity_with_mock_models(self):
        """Using mock models, verify the pipeline produces valid scores."""
        from unittest.mock import MagicMock
        from kalshi_weather_ml.strategy import score_opportunities
        from datetime import datetime, timezone

        mock_model = MagicMock()
        mock_model.predict_bust_prob.return_value = 0.05

        models = [mock_model, mock_model]
        names = ["emos", "ngboost_spread"]
        weights = [0.1, 0.9]
        now = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)

        for ecmwf, gfs, icon, city, wx in TEST_CASES:
            if ecmwf is None or gfs is None:
                continue

            features = build_extra_features(ecmwf, gfs, icon, city, wx)

            scores = score_opportunities(
                ticker=f"KXHIGH-TEST-T{int(ecmwf)}",
                city=city, direction="above",
                threshold=ecmwf, settlement_date="2026-03-15",
                ecmwf=ecmwf, gfs=gfs,
                models=models, model_names=names, model_weights=weights,
                now=now, extra_features=features,
            )
            assert len(scores) > 0
            for s in scores:
                assert 0 <= s.p_win <= 1


class TestCoastNormal:
    """Verify COAST_NORMAL has expected cities."""

    def test_coast_normal_has_expected_cities(self):
        expected = {
            "los_angeles", "san_francisco", "miami", "new_york",
            "boston", "seattle", "houston", "new_orleans",
            "philadelphia", "washington_dc",
        }
        assert set(COAST_NORMAL.keys()) == expected

    def test_coast_normal_values_are_degrees(self):
        for city, deg in COAST_NORMAL.items():
            assert 0 <= deg < 360, f"{city}: {deg} not in [0, 360)"
