# tests/test_morse.py
import pytest

from rvcw.morse import Step, schedule


def test_single_dit_e():
    assert schedule("E", 8) == [Step(True, 150)]


def test_letter_s():
    assert schedule("S", 8) == [
        Step(True, 150), Step(False, 150), Step(True, 150),
        Step(False, 150), Step(True, 150),
    ]


def test_letter_a():
    assert schedule("A", 8) == [Step(True, 150), Step(False, 150), Step(True, 450)]


def test_word_gap():
    # B = "-..." : dah + 3 dits (brief golden omitted the final dit).
    assert schedule("A B", 8) == [
        Step(True, 150), Step(False, 150), Step(True, 450),
        Step(False, 1050),
        Step(True, 450), Step(False, 150), Step(True, 150),
        Step(False, 150), Step(True, 150),
        Step(False, 150), Step(True, 150),
    ]


def test_prosign_brackets():
    # AR = ".-.-." (brief golden was ".--." = P / A+`-.`, contradicting
    # the brief's own PROSIGNS table).
    assert schedule("<AR>", 8) == [
        Step(True, 150), Step(False, 150), Step(True, 450),
        Step(False, 150), Step(True, 150),
        Step(False, 150), Step(True, 450),
        Step(False, 150), Step(True, 150),
    ]


def test_lowercase_normalized():
    assert schedule("e", 8) == schedule("E", 8)


def test_repeat_merged():
    assert schedule("E", 8, repeat=2) == [
        Step(True, 150), Step(False, 1050), Step(True, 150),
    ]


def test_unknown_char_raises():
    with pytest.raises(ValueError, match="no Morse"):
        schedule("H!")


def test_empty_raises():
    with pytest.raises(ValueError, match="nothing to send"):
        schedule("   ")


def test_bad_wpm_raises():
    with pytest.raises(ValueError):
        schedule("E", 0)
    with pytest.raises(ValueError):
        schedule("E", 8, repeat=0)
