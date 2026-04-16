"""Unit tests for chronyc output parsers — no mocking or external deps needed."""
from lucid_component_chrony.component import (
    parse_chronyc_sources_csv,
    parse_chronyc_tracking_csv,
)


# -- parse_chronyc_tracking_csv -----------------------------------------------

VALID_TRACKING_CSV = (
    "C0A80064,192.168.0.100,2,1681725791.123456,0.000012345,"
    "0.000008123,0.000015432,1.234000,0.001000,0.012000,"
    "0.001234,0.000567,64.0,0"
)


def test_parse_tracking_valid():
    result = parse_chronyc_tracking_csv(VALID_TRACKING_CSV)
    assert result is not None
    assert result["ref_id"] == "192.168.0.100"
    assert result["stratum"] == 2
    assert abs(result["offset_ms"] - 0.008123) < 0.000001
    assert abs(result["rms_offset_ms"] - 0.015432) < 0.000001
    assert abs(result["frequency_ppm"] - 1.234) < 0.001
    assert abs(result["root_delay_ms"] - 1.234) < 0.001
    assert abs(result["root_dispersion_ms"] - 0.567) < 0.001
    assert result["update_interval_s"] == 64.0
    assert result["leap_status"] == "0"


def test_parse_tracking_negative_offset():
    csv = (
        "C0A80064,192.168.0.100,2,1681725791.0,0.0,"
        "-0.005000,0.0,0.0,0.0,0.0,0.0,0.0,64.0,0"
    )
    result = parse_chronyc_tracking_csv(csv)
    assert result is not None
    assert result["offset_ms"] == -5.0


def test_parse_tracking_empty_ref_name():
    """When ref_id_name is empty, fall back to hex."""
    csv = (
        "7F000001,,3,1681725791.0,0.0,"
        "0.001000,0.0,0.0,0.0,0.0,0.0,0.0,64.0,0"
    )
    result = parse_chronyc_tracking_csv(csv)
    assert result is not None
    assert result["ref_id"] == "7F000001"


def test_parse_tracking_empty_input():
    assert parse_chronyc_tracking_csv("") is None
    assert parse_chronyc_tracking_csv("   ") is None


def test_parse_tracking_garbage():
    assert parse_chronyc_tracking_csv("not,csv,data") is None


def test_parse_tracking_too_few_fields():
    assert parse_chronyc_tracking_csv("a,b,c,d,e,f") is None


def test_parse_tracking_non_numeric_stratum():
    csv = (
        "C0A80064,192.168.0.100,abc,1681725791.0,0.0,"
        "0.001000,0.0,0.0,0.0,0.0,0.0,0.0,64.0,0"
    )
    assert parse_chronyc_tracking_csv(csv) is None


# -- parse_chronyc_sources_csv -----------------------------------------------

VALID_SOURCES_CSV = """\
^*,192.168.0.100,2,6,377,32,+0.000123,-0.000456,0.000789
^-,pool.ntp.org,3,6,177,64,+0.001234,-0.002345,0.003456
"""


def test_parse_sources_active():
    result = parse_chronyc_sources_csv(VALID_SOURCES_CSV)
    # 377 octal = 255 decimal
    assert result == 255


def test_parse_sources_combined_source():
    csv = "^=,192.168.0.100,2,6,377,32,+0.000123,-0.000456,0.000789\n"
    assert parse_chronyc_sources_csv(csv) == 255


def test_parse_sources_partial_reachability():
    csv = "^*,192.168.0.100,2,6,017,32,+0.000123,-0.000456,0.000789\n"
    # 017 octal = 15 decimal
    assert parse_chronyc_sources_csv(csv) == 15


def test_parse_sources_no_active():
    csv = "^-,pool.ntp.org,3,6,377,64,+0.001234,-0.002345,0.003456\n"
    assert parse_chronyc_sources_csv(csv) == 0


def test_parse_sources_empty():
    assert parse_chronyc_sources_csv("") == 0


def test_parse_sources_garbage():
    assert parse_chronyc_sources_csv("garbage data") == 0
