"""Tests for shared feature computation.

Run: pytest test_shared_features.py --noconftest -v
"""
import pytest
from shared_features import build_extra_features


def test_basic_forecasts():
    f = build_extra_features(ecmwf=55.0, gfs=53.0, icon=54.0, city="chicago", wx={})
    assert f["ecmwf_high"] == 55.0
    assert f["gfs_high"] == 53.0
    assert f["forecast_high"] == 55.0  # max(ecmwf, gfs)
    assert "model_std" in f
    assert "model_range" in f
    assert f["model_range"] == 2.0  # 55 - 53


def test_wind_dir_offshore_coastal():
    f = build_extra_features(ecmwf=55.0, gfs=53.0, icon=None, city="miami", wx={"wind_dir_afternoon": 90})
    assert abs(f["wind_dir_offshore"] - 1.0) < 0.01  # cos(0) = 1.0, coast_normal=90


def test_wind_dir_offshore_inland():
    f = build_extra_features(ecmwf=55.0, gfs=53.0, icon=None, city="chicago", wx={})
    assert f["wind_dir_offshore"] == 0.0  # no coast normal for chicago


def test_none_forecasts():
    f = build_extra_features(ecmwf=55.0, gfs=None, icon=None, city="chicago", wx={})
    assert "gfs_high" not in f
    assert "model_std" not in f  # need 2+ temps
    assert "forecast_high" not in f  # need both ecmwf + gfs


def test_wx_features_passed_through():
    wx = {"humidity_afternoon": 45.0, "wind_speed_max": 12.0}
    f = build_extra_features(ecmwf=55.0, gfs=53.0, icon=None, city="chicago", wx=wx)
    assert f["humidity_afternoon"] == 45.0
    assert f["wind_speed_max"] == 12.0
