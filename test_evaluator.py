"""Tests for the standalone evaluator."""
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from db import init_db, get_connection, get_latest_evaluations, get_desired_orders


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)
        yield db_path


@pytest.fixture(autouse=True)
def _fresh_rolling_features():
    """All evaluate_cycle tests assume rolling features are fresh."""
    with patch("evaluator._check_rolling_features_staleness", return_value=True):
        yield


class TestEvaluateCycle:
    """Test the core evaluation cycle."""

    def test_evaluate_cycle_writes_evaluations(self, tmp_db):
        """Given mock markets and forecasts, cycle writes evaluations for every ticker."""
        from evaluator import evaluate_cycle

        conn = get_connection(tmp_db)

        # Mock dependencies
        mock_markets = [
            {
                "ticker": "KXHIGHCHI-26MAR15-T55",
                "city": "chicago",
                "direction": "above",
                "threshold": 55.0,
                "settlement_date": "2026-03-15",
                "yes_bid": 85,
                "yes_ask": 87,
            },
        ]

        # Build a mock ModelScore to return from score_opportunities
        from kalshi_weather_ml.strategy import ModelScore
        mock_scores = [
            ModelScore(
                ticker="KXHIGHCHI-26MAR15-T55",
                city="chicago",
                direction="above",
                side="no",
                threshold=55.0,
                settlement_date="2026-03-15",
                ecmwf=52.0,
                gfs=53.0,
                margin=3.0,
                consensus=53.0,
                h_to_peak=20.0,
                p_win=0.95,
                model_scores={"emos": 0.95, "ngboost_spread": 0.95},
                n_models_scored=2,
            ),
            ModelScore(
                ticker="KXHIGHCHI-26MAR15-T55",
                city="chicago",
                direction="above",
                side="yes",
                threshold=55.0,
                settlement_date="2026-03-15",
                ecmwf=52.0,
                gfs=53.0,
                margin=-3.0,
                consensus=52.0,
                h_to_peak=20.0,
                p_win=0.05,
                model_scores={"emos": 0.05, "ngboost_spread": 0.05},
                n_models_scored=2,
            ),
        ]

        mock_redis = MagicMock()
        mock_config = MagicMock()
        mock_config.min_p_win = 0.90
        mock_config.above_no_enabled = True
        mock_config.above_yes_enabled = True
        mock_config.below_no_enabled = True
        mock_config.below_yes_enabled = True
        mock_config.min_models_scored = 2
        mock_config.min_margin = None
        mock_config.ensemble_require_unanimous = False
        mock_config.max_no_cost_cents = 92
        mock_config.stable_ladder_offsets_cents = (0, 1, 3, 5, 10)
        mock_config.stable_size = 3
        mock_config.thin_margin_threshold_f = 2.0
        mock_config.thin_margin_size_factor = 0.5
        mock_config.max_contracts_per_ticker = 20
        mock_config.max_total_deployed_cents = 4000

        with patch("evaluator.fetch_open_markets", return_value=mock_markets), \
             patch("evaluator.get_forecast", return_value=52.0), \
             patch("evaluator.get_weather_features", return_value={"wind_speed_afternoon": 10.0}), \
             patch("evaluator.get_forecast_icon", return_value=53.0), \
             patch("evaluator.score_opportunities", return_value=mock_scores), \
             patch("evaluator.apply_entry_filters", return_value=[mock_scores[0]]):

            evaluate_cycle(
                db_conn=conn,
                redis_client=mock_redis,
                models=[],
                model_names=[],
                model_weights=[],
                config=mock_config,
                stream_key="test-signals",
            )

        evals = get_latest_evaluations(conn)
        assert len(evals) > 0
        # Should have evaluations for the ticker (both sides)
        tickers = [e["ticker"] for e in evals]
        assert "KXHIGHCHI-26MAR15-T55" in tickers
        # Both sides should be recorded
        sides = [e["side"] for e in evals if e["ticker"] == "KXHIGHCHI-26MAR15-T55"]
        assert "no" in sides
        assert "yes" in sides
        conn.close()

    def test_evaluate_cycle_pass_and_filtered_recorded(self, tmp_db):
        """Passing scores get filter_result='pass', rejected get 'filtered'."""
        from evaluator import evaluate_cycle
        from kalshi_weather_ml.strategy import ModelScore

        conn = get_connection(tmp_db)

        mock_markets = [
            {
                "ticker": "KXHIGHCHI-26MAR15-T55",
                "city": "chicago",
                "direction": "above",
                "threshold": 55.0,
                "settlement_date": "2026-03-15",
                "yes_bid": 85,
                "yes_ask": 87,
            },
        ]

        no_score = ModelScore(
            ticker="KXHIGHCHI-26MAR15-T55", city="chicago", direction="above",
            side="no", threshold=55.0, settlement_date="2026-03-15",
            ecmwf=52.0, gfs=53.0, margin=3.0, consensus=53.0,
            h_to_peak=20.0, p_win=0.95,
            model_scores={"emos": 0.95}, n_models_scored=1,
        )
        yes_score = ModelScore(
            ticker="KXHIGHCHI-26MAR15-T55", city="chicago", direction="above",
            side="yes", threshold=55.0, settlement_date="2026-03-15",
            ecmwf=52.0, gfs=53.0, margin=-3.0, consensus=52.0,
            h_to_peak=20.0, p_win=0.05,
            model_scores={"emos": 0.05}, n_models_scored=1,
        )

        mock_config = MagicMock()
        mock_config.min_p_win = 0.90
        mock_config.above_no_enabled = True
        mock_config.above_yes_enabled = True
        mock_config.below_no_enabled = True
        mock_config.below_yes_enabled = True
        mock_config.min_models_scored = 1
        mock_config.min_margin = None
        mock_config.ensemble_require_unanimous = False
        mock_config.stable_ladder_offsets_cents = (0, 1, 3, 5, 10)
        mock_config.stable_size = 3
        mock_config.thin_margin_threshold_f = 2.0
        mock_config.thin_margin_size_factor = 0.5
        mock_config.max_contracts_per_ticker = 20
        mock_config.max_total_deployed_cents = 4000
        mock_config.max_no_cost_cents = 92

        with patch("evaluator.fetch_open_markets", return_value=mock_markets), \
             patch("evaluator.get_forecast", return_value=52.0), \
             patch("evaluator.get_weather_features", return_value={}), \
             patch("evaluator.get_forecast_icon", return_value=53.0), \
             patch("evaluator.score_opportunities", return_value=[no_score, yes_score]), \
             patch("evaluator.apply_entry_filters", return_value=[no_score]):  # only NO passes

            evaluate_cycle(
                db_conn=conn,
                redis_client=None,
                models=[], model_names=[], model_weights=[],
                config=mock_config,
                stream_key="test-signals",
            )

        evals = get_latest_evaluations(conn)
        by_side = {e["side"]: e for e in evals}
        assert by_side["no"]["filter_result"] == "pass"
        assert by_side["yes"]["filter_result"] == "filtered"
        conn.close()

    def test_evaluate_cycle_writes_heartbeat(self, tmp_db):
        """Cycle writes evaluator heartbeat."""
        from evaluator import evaluate_cycle
        from db import get_heartbeats

        conn = get_connection(tmp_db)

        with patch("evaluator.fetch_open_markets", return_value=[]):
            mock_redis = MagicMock()
            evaluate_cycle(
                db_conn=conn,
                redis_client=mock_redis,
                models=[], model_names=[], model_weights=[],
                config=MagicMock(),
                stream_key="test-signals",
            )

        heartbeats = get_heartbeats(conn)
        names = [h["process_name"] for h in heartbeats]
        assert "evaluator" in names
        conn.close()

    def test_evaluate_cycle_no_redis_skips_publish(self, tmp_db):
        """When redis_client is None, no Redis publish is attempted."""
        from evaluator import evaluate_cycle

        conn = get_connection(tmp_db)

        with patch("evaluator.fetch_open_markets", return_value=[]):
            # Should not raise even with redis_client=None
            evaluate_cycle(
                db_conn=conn,
                redis_client=None,
                models=[], model_names=[], model_weights=[],
                config=MagicMock(),
                stream_key="test-signals",
            )

        conn.close()

    def test_evaluate_cycle_publishes_to_redis_on_pass(self, tmp_db):
        """Passing signals are published to Redis as msgpack ModelSignal."""
        import msgpack as _msgpack
        from evaluator import evaluate_cycle
        from kalshi_weather_ml.strategy import ModelScore

        conn = get_connection(tmp_db)

        mock_markets = [
            {
                "ticker": "KXHIGHCHI-26MAR15-T55",
                "city": "chicago",
                "direction": "above",
                "threshold": 55.0,
                "settlement_date": "2026-03-15",
                "yes_bid": 85,
                "yes_ask": 87,
            },
        ]

        no_score = ModelScore(
            ticker="KXHIGHCHI-26MAR15-T55", city="chicago", direction="above",
            side="no", threshold=55.0, settlement_date="2026-03-15",
            ecmwf=52.0, gfs=53.0, margin=3.0, consensus=53.0,
            h_to_peak=20.0, p_win=0.95,
            model_scores={"emos": 0.95}, n_models_scored=1,
        )

        mock_config = MagicMock()
        mock_config.stable_ladder_offsets_cents = (0,)
        mock_config.stable_size = 1
        mock_config.thin_margin_threshold_f = 0.0
        mock_config.thin_margin_size_factor = 1.0
        mock_config.max_contracts_per_ticker = 20
        mock_config.max_total_deployed_cents = 4000
        mock_config.max_no_cost_cents = 92

        mock_redis = MagicMock()
        xadd_calls = []
        mock_redis.xadd.side_effect = lambda key, fields: xadd_calls.append((key, fields))

        with patch("evaluator.fetch_open_markets", return_value=mock_markets), \
             patch("evaluator.get_forecast", return_value=52.0), \
             patch("evaluator.get_weather_features", return_value={}), \
             patch("evaluator.get_forecast_icon", return_value=53.0), \
             patch("evaluator.score_opportunities", return_value=[no_score]), \
             patch("evaluator.apply_entry_filters", return_value=[no_score]):

            from nautilus_trader.serialization.serializer import MsgSpecSerializer as _MsgSpecSerializer
            import msgspec as _msgspec_mod
            ser = _MsgSpecSerializer(encoding=_msgspec_mod.msgpack)

            evaluate_cycle(
                db_conn=conn,
                redis_client=mock_redis,
                models=[], model_names=[], model_weights=[],
                config=mock_config,
                stream_key="test-signals",
                serializer=ser,
            )

        assert len(xadd_calls) == 1
        stream_key, fields = xadd_calls[0]
        assert stream_key == "test-signals"
        assert fields[b"type"] == b"ModelSignal"
        # Decode payload via NT serializer and verify full roundtrip
        from nautilus_trader.serialization.serializer import MsgSpecSerializer as _MsgSpecSerializer
        import msgspec as _msgspec_mod
        deser = _MsgSpecSerializer(encoding=_msgspec_mod.msgpack)
        restored = deser.deserialize(fields[b"payload"])
        assert type(restored).__name__ == "ModelSignal"
        assert restored.ticker == "KXHIGHCHI-26MAR15-T55"
        assert restored.side == "no"
        expected_p_win = no_score.p_win
        assert restored.p_win == pytest.approx(expected_p_win, abs=0.01)
        conn.close()


class TestLoadModels:
    """Test model loading."""

    def test_load_models_returns_lists(self):
        """load_models returns (models, names, weights) lists."""
        from evaluator import load_models

        mock_config = MagicMock()
        mock_config.ensemble_models = ["emos", "ngboost"]
        mock_config.ensemble_weights = {"emos": 0.1, "ngboost": 0.9}

        mock_emos_instance = MagicMock()
        mock_ngb_instance = MagicMock()

        with patch("evaluator.EMOSModel") as mock_emos, \
             patch("evaluator.NGBoostModel") as mock_ngb:
            mock_emos.load.return_value = mock_emos_instance
            mock_ngb.load.return_value = mock_ngb_instance

            models, names, weights = load_models(mock_config)

        assert len(models) == 2
        assert names == ["emos", "ngboost"]
        assert weights == [0.1, 0.9]

    def test_load_models_unknown_name_raises(self):
        """Unknown model name raises ValueError."""
        from evaluator import load_models

        mock_config = MagicMock()
        mock_config.ensemble_models = ["unknown_model"]
        mock_config.ensemble_weights = {}

        with pytest.raises(ValueError, match="Unknown model"):
            load_models(mock_config)

    def test_load_models_default_weights(self):
        """Missing weight defaults to 1.0."""
        from evaluator import load_models

        mock_config = MagicMock()
        mock_config.ensemble_models = ["emos"]
        mock_config.ensemble_weights = {}  # no weight specified

        with patch("evaluator.EMOSModel") as mock_emos:
            mock_emos.load.return_value = MagicMock()
            models, names, weights = load_models(mock_config)

        assert weights == [1.0]


class TestBuildExtraFeatures:
    """Test feature building."""

    def test_build_extra_features_basic(self):
        """Basic feature building with ecmwf + gfs."""
        from evaluator import build_extra_features

        features = build_extra_features(
            ecmwf=55.0, gfs=53.0, icon=54.0,
            city="chicago", date="2026-03-15",
            wx={"wind_speed_afternoon": 10.0},
        )
        assert "ecmwf_high" in features
        assert "gfs_high" in features
        assert "model_std" in features
        assert "model_range" in features
        assert features["model_range"] == 2.0  # max(55, 54, 53) - min(55, 54, 53) = 55 - 53 = 2
        assert features["ecmwf_high"] == 55.0
        assert features["gfs_high"] == 53.0
        assert features["forecast_high"] == 55.0  # max(ecmwf, gfs)

    def test_build_extra_features_no_icon(self):
        """Feature building without ICON falls back to 2-model spread."""
        from evaluator import build_extra_features

        features = build_extra_features(
            ecmwf=55.0, gfs=53.0, icon=None,
            city="chicago", date="2026-03-15",
            wx={},
        )
        assert "model_std" in features
        assert "model_range" in features
        # 2-model spread: range = 55 - 53 = 2
        assert features["model_range"] == 2.0

    def test_build_extra_features_wind_dir_offshore_computed(self):
        """wind_dir_offshore is computed when city has coast normal and wind_dir present."""
        from evaluator import build_extra_features
        import numpy as np

        features = build_extra_features(
            ecmwf=55.0, gfs=53.0, icon=None,
            city="miami",
            date="2026-03-15",
            wx={"wind_dir_afternoon": 90.0},  # exactly onshore
        )
        assert "wind_dir_offshore" in features
        # miami coast_normal=90, wind_dir=90 → angle_diff=0 → cos(0)=1.0
        assert abs(features["wind_dir_offshore"] - 1.0) < 1e-9

    def test_build_extra_features_no_coast_normal(self):
        """Cities without coast normal get wind_dir_offshore=0.0."""
        from evaluator import build_extra_features

        features = build_extra_features(
            ecmwf=55.0, gfs=53.0, icon=None,
            city="chicago",  # inland — no coast normal
            date="2026-03-15",
            wx={"wind_dir_afternoon": 180.0},
        )
        assert features["wind_dir_offshore"] == 0.0

    def test_build_extra_features_wx_merged(self):
        """Weather features from wx dict are merged into output."""
        from evaluator import build_extra_features

        wx = {"wind_speed_afternoon": 15.0, "precip_mm": 2.5}
        features = build_extra_features(
            ecmwf=55.0, gfs=53.0, icon=None,
            city="chicago", date="2026-03-15",
            wx=wx,
        )
        assert features["wind_speed_afternoon"] == 15.0
        assert features["precip_mm"] == 2.5


class TestComputeDesiredLadder:
    """Test ladder computation."""

    def test_compute_ladder_basic(self):
        """Basic ladder with 5 offsets."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=90,
            config_offsets=(0, 1, 3, 5, 10),
            config_size=3,
            max_cost_cents=92,
            thin_margin_threshold_f=2.0,
            thin_margin_size_factor=0.5,
            margin=5.0,  # not thin
            capacity=20,
            budget=5000,
        )
        assert len(orders) > 0
        # First order at bid (offset 0)
        assert orders[0]["price_cents"] == 90
        assert orders[0]["qty"] == 3

    def test_compute_ladder_respects_cost_ceiling(self):
        """Orders above max_cost_cents are skipped."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=95,  # above ceiling
            config_offsets=(0, 1, 3),
            config_size=3,
            max_cost_cents=92,
            thin_margin_threshold_f=0,
            thin_margin_size_factor=1.0,
            margin=5.0,
            capacity=20,
            budget=5000,
        )
        # Prices: 95 (skip, >92), 94 (skip, >92), 92 (include)
        prices = [o["price_cents"] for o in orders]
        assert all(p <= 92 for p in prices)

    def test_compute_ladder_thin_margin(self):
        """Thin margin reduces size."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=88,
            config_offsets=(0,),
            config_size=4,
            max_cost_cents=92,
            thin_margin_threshold_f=2.0,
            thin_margin_size_factor=0.5,
            margin=1.0,  # thin!
            capacity=20,
            budget=5000,
        )
        assert orders[0]["qty"] == 2  # 4 * 0.5 = 2

    def test_compute_ladder_respects_budget(self):
        """Orders stop when budget exhausted."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=90,
            config_offsets=(0, 1, 3, 5, 10),
            config_size=3,
            max_cost_cents=92,
            thin_margin_threshold_f=0,
            thin_margin_size_factor=1.0,
            margin=5.0,
            capacity=20,
            budget=300,  # tight budget — 90*3=270, then next order 89*3=267 > 30 remaining
        )
        total = sum(o["price_cents"] * o["qty"] for o in orders)
        assert total <= 300

    def test_compute_ladder_respects_capacity(self):
        """Orders stop when position capacity exhausted."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=90,
            config_offsets=(0, 1, 3),
            config_size=3,
            max_cost_cents=92,
            thin_margin_threshold_f=0,
            thin_margin_size_factor=1.0,
            margin=5.0,
            capacity=5,  # only room for 5 contracts total
            budget=50000,
        )
        total_qty = sum(o["qty"] for o in orders)
        assert total_qty <= 5

    def test_compute_ladder_empty_when_all_above_ceiling(self):
        """Returns empty list when all prices exceed max_cost_cents."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=99,
            config_offsets=(0, 1, 2, 3, 4, 5, 6),  # prices 99..93 all above 92
            config_size=3,
            max_cost_cents=92,
            thin_margin_threshold_f=0,
            thin_margin_size_factor=1.0,
            margin=5.0,
            capacity=20,
            budget=5000,
        )
        assert orders == []

    def test_compute_ladder_reason_includes_offset(self):
        """Each order's reason field includes the offset."""
        from evaluator import compute_desired_ladder

        orders = compute_desired_ladder(
            bid_cents=85,
            config_offsets=(0, 2),
            config_size=2,
            max_cost_cents=92,
            thin_margin_threshold_f=0,
            thin_margin_size_factor=1.0,
            margin=5.0,
            capacity=20,
            budget=5000,
        )
        assert len(orders) == 2
        assert "offset=0" in orders[0]["reason"]
        assert "offset=2" in orders[1]["reason"]


class TestStalenessGate:
    """Test that stale rolling features suppress order generation."""

    def test_stale_features_suppresses_orders(self, tmp_db):
        """When features are stale, evaluations are written but no orders generated."""
        from evaluator import evaluate_cycle
        from db import get_heartbeats

        conn = get_connection(tmp_db)

        mock_markets = [
            {
                "ticker": "KXHIGHCHI-26MAR15-T55",
                "city": "chicago",
                "direction": "above",
                "threshold": 55.0,
                "settlement_date": "2026-03-15",
                "yes_bid": 85,
                "yes_ask": 87,
            },
        ]

        mock_model = MagicMock()
        mock_model.predict_bust_prob.return_value = 0.05

        mock_config = MagicMock()
        mock_config.min_p_win = 0.90
        mock_config.above_no_enabled = True
        mock_config.above_yes_enabled = True
        mock_config.below_no_enabled = True
        mock_config.below_yes_enabled = True
        mock_config.min_models_scored = 1
        mock_config.min_margin = None
        mock_config.ensemble_require_unanimous = False
        mock_config.max_no_cost_cents = 92
        mock_config.max_total_deployed_cents = 4000
        mock_config.max_contracts_per_ticker = 20

        with patch("evaluator._check_rolling_features_staleness", return_value=False), \
             patch("evaluator.fetch_open_markets", return_value=mock_markets), \
             patch("evaluator.get_forecast", return_value=52.0), \
             patch("evaluator.get_weather_features", return_value={}), \
             patch("evaluator.get_forecast_icon", return_value=53.0):

            evaluate_cycle(
                db_conn=conn,
                redis_client=MagicMock(),
                models=[mock_model],
                model_names=["emos"],
                model_weights=[1.0],
                config=mock_config,
                stream_key="test-signals",
            )

        # Evaluations should still be written
        evals = get_latest_evaluations(conn)
        assert len(evals) > 0

        # But no desired orders
        orders = get_desired_orders(conn, status="pending")
        assert len(orders) == 0

        # Heartbeat should say STALE
        heartbeats = get_heartbeats(conn)
        hb = [h for h in heartbeats if h["process_name"] == "evaluator"][0]
        assert hb["status"] == "stale"
        conn.close()
