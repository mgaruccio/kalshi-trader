"""Verify evaluator and feature_actor produce identical scores.

This is a parity test — the new evaluator path must produce the exact same
p_win values as the old feature_actor path for the same inputs.
"""
import sys
import os
import pytest
import numpy as np

# Ensure kalshi_weather_ml is importable
_KW_ROOT = os.environ.get("KALSHI_WEATHER_ROOT", "/home/mike/code/altmarkets/kalshi-weather")
sys.path.insert(0, f"{_KW_ROOT}/src")

from evaluator import build_extra_features as eval_build_features
from feature_actor import _build_extra_features as actor_build_features


# Test cases: representative cities covering coast/inland, with/without ICON
TEST_CASES = [
    # (ecmwf, gfs, icon, city, date, wx_dict)
    (55.0, 53.0, 54.0, "chicago", "2026-03-15", {"wind_speed_afternoon": 10.0, "wind_dir_afternoon": 180.0}),
    (72.0, 70.0, None, "miami", "2026-03-15", {"wind_speed_afternoon": 8.0, "wind_dir_afternoon": 90.0}),
    (65.0, 67.0, 66.0, "new_york", "2026-03-15", {"wind_speed_afternoon": 15.0, "wind_dir_afternoon": 270.0}),
    (80.0, 78.0, 79.0, "houston", "2026-03-15", {}),
    (45.0, 48.0, 46.0, "boston", "2026-03-15", {"wind_dir_afternoon": 45.0}),
    (60.0, 60.0, 60.0, "los_angeles", "2026-03-15", {"wind_speed_afternoon": 5.0, "wind_dir_afternoon": 200.0}),
    # Cities that DON'T have coast normals (inland)
    (55.0, 57.0, 56.0, "denver", "2026-03-15", {"wind_dir_afternoon": 300.0}),
    (70.0, 68.0, 69.0, "atlanta", "2026-03-15", {"wind_speed_afternoon": 12.0}),
    # Edge cases
    (50.0, 50.0, 50.0, "chicago", "2026-03-15", {}),  # identical forecasts
    (None, 55.0, None, "chicago", "2026-03-15", {}),   # missing ecmwf — should still handle (though score_opportunities won't run)
    (55.0, None, None, "chicago", "2026-03-15", {}),   # missing gfs
]


class TestFeatureParity:
    """Verify build_extra_features produces identical output in both code paths."""

    @pytest.mark.parametrize("ecmwf,gfs,icon,city,date,wx", TEST_CASES)
    def test_features_match(self, ecmwf, gfs, icon, city, date, wx):
        eval_features = eval_build_features(ecmwf, gfs, icon, city, date, wx)
        actor_features = actor_build_features(ecmwf, gfs, icon, city, date, wx)

        # Same keys
        assert set(eval_features.keys()) == set(actor_features.keys()), \
            f"Key mismatch for {city}: eval={sorted(eval_features.keys())} actor={sorted(actor_features.keys())}"

        # Same values (float comparison)
        for key in eval_features:
            assert eval_features[key] == pytest.approx(actor_features[key], abs=1e-10), \
                f"Value mismatch for {city}/{key}: eval={eval_features[key]} actor={actor_features[key]}"


class TestScoreParity:
    """Verify score_opportunities produces identical p_win with both feature sets."""

    def test_score_parity_with_mock_models(self):
        """Using mock models, verify the pipeline produces the same p_win."""
        from unittest.mock import MagicMock
        from kalshi_weather_ml.strategy import score_opportunities
        from datetime import datetime, timezone

        # Mock model that returns predictable p_bust based on inputs
        mock_model = MagicMock()
        mock_model.predict_bust_prob.return_value = 0.05

        models = [mock_model, mock_model]
        names = ["emos", "ngboost_spread"]
        weights = [0.1, 0.9]
        now = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)

        for ecmwf, gfs, icon, city, date, wx in TEST_CASES:
            if ecmwf is None or gfs is None:
                continue  # score_opportunities requires both

            eval_features = eval_build_features(ecmwf, gfs, icon, city, date, wx)
            actor_features = actor_build_features(ecmwf, gfs, icon, city, date, wx)

            eval_scores = score_opportunities(
                ticker=f"KXHIGH-TEST-T{int(ecmwf)}",
                city=city, direction="above",
                threshold=ecmwf, settlement_date=date,
                ecmwf=ecmwf, gfs=gfs,
                models=models, model_names=names, model_weights=weights,
                now=now, extra_features=eval_features,
            )

            actor_scores = score_opportunities(
                ticker=f"KXHIGH-TEST-T{int(ecmwf)}",
                city=city, direction="above",
                threshold=ecmwf, settlement_date=date,
                ecmwf=ecmwf, gfs=gfs,
                models=models, model_names=names, model_weights=weights,
                now=now, extra_features=actor_features,
            )

            for es, as_ in zip(eval_scores, actor_scores):
                assert es.p_win == pytest.approx(as_.p_win, abs=1e-10), \
                    f"p_win mismatch for {city} {es.side}: eval={es.p_win} actor={as_.p_win}"
                assert es.margin == pytest.approx(as_.margin, abs=1e-10)
                assert es.consensus == pytest.approx(as_.consensus, abs=1e-10)


class TestCoastNormalParity:
    """Verify _COAST_NORMAL dicts are identical."""

    def test_coast_normal_dicts_match(self):
        from evaluator import _COAST_NORMAL as eval_coast
        from feature_actor import _COAST_NORMAL as actor_coast

        assert eval_coast == actor_coast, \
            f"Mismatch: eval_only={set(eval_coast) - set(actor_coast)}, actor_only={set(actor_coast) - set(eval_coast)}"
