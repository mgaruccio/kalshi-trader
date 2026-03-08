"""Tests for ClimateEvent, ModelSignal, DangerAlert.

Run: pytest test_data_types.py --noconftest -v
"""
import pytest
from data_types import ClimateEvent, ModelSignal, DangerAlert


class TestClimateEvent:
    def test_construction(self):
        ts = 1_709_913_600_000_000_000  # 2024-03-08T12:00:00Z
        evt = ClimateEvent(
            source="ndbc_buoy_sst",
            city="chicago",
            features={"sst_value": 42.3, "sst_anomaly": 1.5},
            ts_event=ts,
            ts_init=ts,
        )
        assert evt.source == "ndbc_buoy_sst"
        assert evt.city == "chicago"
        assert evt.features["sst_value"] == 42.3
        assert evt.ts_event == ts
        assert evt.ts_init == ts

    def test_repr(self):
        evt = ClimateEvent("afd", "miami", {"afd_confidence": 0.8}, 0, 0)
        assert "ClimateEvent" in repr(evt)
        assert "miami" in repr(evt)

    def test_empty_features(self):
        evt = ClimateEvent("metar", "denver", {}, 0, 0)
        assert evt.features == {}


class TestModelSignal:
    def test_construction(self):
        ts = 1_709_913_600_000_000_000
        sig = ModelSignal(
            city="chicago",
            ticker="KXHIGHCHI-26MAR08-T55",
            side="no",
            p_win=0.95,
            model_scores={"emos": 0.92, "ngboost": 0.96, "drn": 0.93},
            features_snapshot={"ecmwf_high": 52.1, "gfs_high": 51.8},
            ts_event=ts,
            ts_init=ts,
        )
        assert sig.p_win == 0.95
        assert sig.side == "no"
        assert sig.model_scores["ngboost"] == 0.96

    def test_repr(self):
        sig = ModelSignal("chi", "T55", "no", 0.95, {}, {}, 0, 0)
        assert "0.950" in repr(sig)


class TestDangerAlert:
    def test_construction(self):
        ts = 1_709_913_600_000_000_000
        alert = DangerAlert(
            ticker="KXHIGHCHI-26MAR08-T55",
            city="chicago",
            alert_level="CRITICAL",
            rule_name="obs_approaching_threshold",
            reason="Obs 54.2F within 1F of T55",
            features={"obs_temp": 54.2},
            ts_event=ts,
            ts_init=ts,
        )
        assert alert.alert_level == "CRITICAL"
        assert alert.rule_name == "obs_approaching_threshold"

    def test_repr(self):
        alert = DangerAlert("T55", "chi", "WATCH", "drift", "test", {}, 0, 0)
        assert "WATCH" in repr(alert)
        assert "drift" in repr(alert)
