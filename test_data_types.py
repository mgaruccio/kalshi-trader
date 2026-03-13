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


class TestRoundtrip:
    def test_model_signal_roundtrip(self):
        signal = ModelSignal(
            city="chicago", ticker="KXHIGHCHI-26MAR15-T55",
            side="no", p_win=0.97,
            model_scores={"emos": 0.98, "ngboost_spread": 0.96},
            features_snapshot={"ecmwf_high": 55.0, "gfs_high": 54.0},
            ts_event=1000, ts_init=1000,
        )
        d = ModelSignal.to_dict(signal)
        restored = ModelSignal.from_dict(d)
        assert restored.city == signal.city
        assert restored.ticker == signal.ticker
        assert restored.side == signal.side
        assert abs(restored.p_win - signal.p_win) < 1e-9
        assert restored.model_scores == signal.model_scores
        assert restored.features_snapshot == signal.features_snapshot
        assert restored.ts_event == signal.ts_event
        assert restored.ts_init == signal.ts_init

    def test_danger_alert_roundtrip(self):
        alert = DangerAlert(
            ticker="KXHIGHCHI-26MAR15-T55", city="chicago",
            alert_level="CRITICAL", rule_name="obs_past_threshold",
            reason="Observed 56F exceeds threshold 55F",
            features={"current_temp": 56.0},
            ts_event=2000, ts_init=2000,
        )
        d = DangerAlert.to_dict(alert)
        restored = DangerAlert.from_dict(d)
        assert restored.ticker == alert.ticker
        assert restored.city == alert.city
        assert restored.alert_level == alert.alert_level
        assert restored.rule_name == alert.rule_name
        assert restored.reason == alert.reason
        assert restored.features == alert.features
        assert restored.ts_event == alert.ts_event
        assert restored.ts_init == alert.ts_init

    def test_climate_event_roundtrip(self):
        event = ClimateEvent(
            source="open_meteo", city="chicago",
            features={"ecmwf_high": 55.0},
            ts_event=3000, ts_init=3000,
            date="2026-03-15",
        )
        d = ClimateEvent.to_dict(event)
        restored = ClimateEvent.from_dict(d)
        assert restored.source == event.source
        assert restored.city == event.city
        assert restored.date == event.date
        assert restored.features == event.features
        assert restored.ts_event == event.ts_event
        assert restored.ts_init == event.ts_init
