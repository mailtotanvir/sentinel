import pytest

from correct_parse_duration import parse_duration


VALID_CASES = [
    ("0", 0),
    ("9", 9),
    ("45", 45),
    ("1020", 1020),
    ("007", 7),
    ("000", 0),
    ("0s", 0),
    ("9s", 9),
    ("59s", 59),
    ("60s", 60),
    ("61s", 61),
    ("0m", 0),
    ("1m", 60),
    ("10m", 600),
    ("90m", 5400),
    ("007m", 420),
    ("0h", 0),
    ("1h", 3600),
    ("24h", 86400),
    ("100h", 360000),
    ("1h30m", 5400),
    ("1h2m3s", 3723),
    ("30m1h", 5400),
    ("15s30m1h", 5415),
    ("1h30", 3630),
    ("1h0m0s", 3600),
    ("0h0m0s", 0),
    ("2m3m", 300),
    ("100h100m100s", 366100),
    ("1H30M", 5400),
    ("1h2M3S", 3723),
    ("5S", 5),
    ("  1h30m  ", 5400),
    ("\t1h\n", 3600),
    (" 42 ", 42),
    ("999999999999h", 999999999999 * 3600),
]

EMPTY_INPUTS = ["", " ", "\t", "\n", "\r", " \t\r\n  "]

UNIT_WITHOUT_NUMBER_INPUTS = ["h", "m", "s", "H", "hh", "1hs", "10hm"]

UNEXPECTED_CHARACTER_INPUTS = [
    "-5m",
    "1.5h",
    "1e3",
    "1h 30m",
    "abc",
    "+30s",
    "1h,",
    "d",
    "1h#30m",
]


class TestValidDurations:
    @pytest.mark.parametrize(("text", "expected"), VALID_CASES)
    def test_parses_to_expected_seconds(self, text, expected):
        assert parse_duration(text) == expected

    def test_returns_int(self):
        assert isinstance(parse_duration("2h"), int)

    def test_equivalent_representations_agree(self):
        assert (
            parse_duration("1h")
            == parse_duration("60m")
            == parse_duration("3600s")
            == 3600
        )

    def test_unit_order_does_not_change_total(self):
        orders = ["1h30m15s", "15s30m1h", "30m15s1h", "1h15s30m"]
        results = {parse_duration(text) for text in orders}
        assert results == {3600 + 1800 + 15}

    def test_repeated_units_accumulate(self):
        assert parse_duration("1h1h") == 7200


class TestEmptyInput:
    @pytest.mark.parametrize("text", EMPTY_INPUTS)
    def test_raises_value_error(self, text):
        with pytest.raises(ValueError, match="empty duration"):
            parse_duration(text)

    def test_exact_message_for_empty_string(self):
        with pytest.raises(ValueError) as excinfo:
            parse_duration("")
        assert str(excinfo.value) == "empty duration"

    def test_none_treated_as_empty(self):
        with pytest.raises(ValueError, match="empty duration"):
            parse_duration(None)


class TestUnitWithoutNumber:
    @pytest.mark.parametrize("text", UNIT_WITHOUT_NUMBER_INPUTS)
    def test_raises_value_error(self, text):
        with pytest.raises(ValueError, match="without number"):
            parse_duration(text)

    def test_message_names_unit_and_original_text(self):
        with pytest.raises(ValueError) as excinfo:
            parse_duration("10hm")
        assert str(excinfo.value) == "unit 'm' without number in '10hm'"


class TestUnexpectedCharacter:
    @pytest.mark.parametrize("text", UNEXPECTED_CHARACTER_INPUTS)
    def test_raises_value_error(self, text):
        with pytest.raises(ValueError, match="unexpected character"):
            parse_duration(text)

    def test_message_quotes_lowercase_char_and_original_text(self):
        with pytest.raises(ValueError) as excinfo:
            parse_duration("1H30X")
        assert str(excinfo.value) == "unexpected character 'x' in '1H30X'"
