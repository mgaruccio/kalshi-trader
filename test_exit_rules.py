"""Tests for CityFeatureState and check_exit_rules.

Run: pytest test_exit_rules.py --noconftest -v

These are pure Python -- no NT dependency.
"""
import pytest
from exit_rules import CityFeatureState, check_exit_rules, compute_danger_points, should_exit


class TestCityFeatureState:
    def test_update_and_get(self):
        state = CityFeatureState()
        state.update("ndbc", {"sst_value": 42.3, "sst_anomaly": 1.5})
        assert state.get("sst_value") == 42.3
        assert state.get("sst_anomaly") == 1.5

    def test_get_missing(self):
        state = CityFeatureState()
        assert state.get("nonexistent") is None

    def test_multi_source_update(self):
        state = CityFeatureState()
        state.update("ndbc", {"sst_value": 42.3})
        state.update("ecmwf", {"forecast_high": 55.0})
        snap = state.snapshot()
        assert snap["sst_value"] == 42.3
        assert snap["forecast_high"] == 55.0

    def test_overwrite_same_key(self):
        state = CityFeatureState()
        state.update("ecmwf", {"forecast_high": 55.0})
        state.update("ecmwf", {"forecast_high": 56.0})
        assert state.get("forecast_high") == 56.0

    def test_skip_nan(self):
        state = CityFeatureState()
        state.update("test", {"good": 1.0, "bad": float("nan")})
        assert state.get("good") == 1.0
        assert state.get("bad") is None

    def test_skip_none(self):
        state = CityFeatureState()
        state.update("test", {"good": 1.0, "bad": None})
        assert state.get("good") == 1.0
        assert state.get("bad") is None

    def test_sources(self):
        state = CityFeatureState()
        state.update("ndbc", {"sst": 42.0})
        state.update("ecmwf", {"temp": 55.0})
        assert state.sources() == {"ndbc", "ecmwf"}

    def test_snapshot_is_copy(self):
        state = CityFeatureState()
        state.update("test", {"val": 1.0})
        snap = state.snapshot()
        snap["val"] = 99.0
        assert state.get("val") == 1.0


class TestCheckExitRules:
    @pytest.fixture
    def no_position(self):
        return {"KXHIGHCHI-T55-NO": {"side": "no", "threshold": 55.0, "city": "chicago"}}

    def test_obs_approaching_threshold_critical(self, no_position):
        features = {"obs_temp": 54.2}  # within 1F of T55
        alerts = check_exit_rules("chicago", features, no_position)
        assert len(alerts) >= 1
        obs_alert = [a for a in alerts if a["rule_name"] == "obs_approaching_threshold"]
        assert len(obs_alert) == 1
        assert obs_alert[0]["points"] == 3
        assert obs_alert[0]["alert_level"] == "CRITICAL"

    def test_obs_breached_threshold(self, no_position):
        features = {"obs_temp": 56.0}  # above T55
        alerts = check_exit_rules("chicago", features, no_position)
        obs_alert = [a for a in alerts if a["rule_name"] == "obs_approaching_threshold"]
        assert len(obs_alert) == 1
        assert obs_alert[0]["alert_level"] == "CRITICAL"

    def test_obs_safe(self, no_position):
        features = {"obs_temp": 50.0}  # 5F below T55
        alerts = check_exit_rules("chicago", features, no_position)
        obs_alert = [a for a in alerts if a["rule_name"] == "obs_approaching_threshold"]
        assert len(obs_alert) == 0

    def test_sst_contrast_collapse_chicago(self, no_position):
        features = {"sst_value": 48.0, "forecast_high": 52.0}  # contrast = 4F < 5F
        alerts = check_exit_rules("chicago", features, no_position)
        sst_alert = [a for a in alerts if a["rule_name"] == "sst_land_contrast_collapse"]
        assert len(sst_alert) == 1
        assert sst_alert[0]["points"] == 2

    def test_sst_no_alert_inland(self):
        positions = {"KXHIGHATL-T75-NO": {"side": "no", "threshold": 75.0, "city": "atlanta"}}
        features = {"sst_value": 70.0, "forecast_high": 73.0}  # contrast < 5 but ATL is not coastal
        alerts = check_exit_rules("atlanta", features, positions)
        sst_alert = [a for a in alerts if a["rule_name"] == "sst_land_contrast_collapse"]
        assert len(sst_alert) == 0  # Atlanta is CAD, not coastal

    def test_inversion_weakening_lax(self):
        positions = {"KXHIGHLAX-T85-NO": {"side": "no", "threshold": 85.0, "city": "los_angeles"}}
        features = {"inversion_strength_c": 2.5}
        alerts = check_exit_rules("los_angeles", features, positions)
        inv_alert = [a for a in alerts if a["rule_name"] == "inversion_weakening"]
        assert len(inv_alert) == 1

    def test_cad_eroding_dc(self):
        positions = {"KXHIGHDCA-T65-NO": {"side": "no", "threshold": 65.0, "city": "washington_dc"}}
        features = {"afd_cad_flag": 0, "wind_dir_afternoon": 220.0}
        alerts = check_exit_rules("washington_dc", features, positions)
        cad_alert = [a for a in alerts if a["rule_name"] == "cad_eroding"]
        assert len(cad_alert) == 1

    def test_dryline_passage_dallas(self):
        positions = {"KXHIGHDAL-T95-NO": {"side": "no", "threshold": 95.0, "city": "dallas"}}
        features = {"dew_point_drop_3h": 18.0}
        alerts = check_exit_rules("dallas", features, positions)
        dry_alert = [a for a in alerts if a["rule_name"] == "dryline_passage"]
        assert len(dry_alert) == 1

    def test_monsoon_break_phoenix(self):
        positions = {"KXHIGHPHX-T110-NO": {"side": "no", "threshold": 110.0, "city": "phoenix"}}
        features = {"cape_max": 300.0, "humidity_afternoon": 10.0}
        alerts = check_exit_rules("phoenix", features, positions)
        monsoon_alert = [a for a in alerts if a["rule_name"] == "monsoon_break"]
        assert len(monsoon_alert) == 1

    def test_snow_clear_sky_minneapolis(self):
        positions = {"KXHIGHMSP-T30-NO": {"side": "no", "threshold": 30.0, "city": "minneapolis"}}
        features = {"snow_cover": 0, "cloud_cover_afternoon": 10.0}
        alerts = check_exit_rules("minneapolis", features, positions)
        snow_alert = [a for a in alerts if a["rule_name"] == "snow_melt_clear_sky"]
        assert len(snow_alert) == 1
        assert snow_alert[0]["points"] == 1  # Only 1 point for WATCH

    def test_forecast_drift_adverse(self, no_position):
        features = {"entry_margin": 4.0, "current_margin": 1.5}  # 62.5% shrinkage
        alerts = check_exit_rules("chicago", features, no_position)
        drift_alert = [a for a in alerts if a["rule_name"] == "forecast_drift_adverse"]
        assert len(drift_alert) == 1

    def test_no_positions_no_alerts(self):
        alerts = check_exit_rules("chicago", {"obs_temp": 54.5}, {})
        assert alerts == []

    def test_wrong_city_no_alerts(self, no_position):
        # Position is for chicago, checking miami
        alerts = check_exit_rules("miami", {"obs_temp": 80.0}, no_position)
        # No miami positions exist, so no obs alerts
        obs_alert = [a for a in alerts if a["rule_name"] == "obs_approaching_threshold"]
        assert len(obs_alert) == 0


class TestDangerPointsAndExit:
    def test_compute_points(self):
        alerts = [
            {"rule_name": "obs_approaching_threshold", "points": 3},
            {"rule_name": "sst_land_contrast_collapse", "points": 2},
        ]
        total, sources = compute_danger_points(alerts)
        assert total == 5
        assert sources == 2

    def test_should_exit_corroborated(self):
        alerts = [
            {"rule_name": "obs_approaching_threshold", "points": 3},
            {"rule_name": "sst_land_contrast_collapse", "points": 2},
        ]
        assert should_exit(alerts) is True

    def test_should_not_exit_single_source(self):
        # 6 points but all from same rule
        alerts = [
            {"rule_name": "obs_approaching_threshold", "points": 3},
            {"rule_name": "obs_approaching_threshold", "points": 3},
        ]
        assert should_exit(alerts) is False

    def test_should_not_exit_low_points(self):
        alerts = [
            {"rule_name": "snow_melt_clear_sky", "points": 1},
            {"rule_name": "forecast_drift_adverse", "points": 2},
        ]
        assert should_exit(alerts) is False  # 3 points < 5
