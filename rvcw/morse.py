"""Pure Morse engine: text -> on/off schedule in milliseconds. No I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Standard ITU: dit=1, dah=3, intra-char gap=1, inter-char gap=3, word gap=7.
# Unit length = 1200 / WPM ms (PARIS standard).


@dataclass(frozen=True)
class Step:
    on: bool
    ms: int


MORSE: dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "-": "-....-",
    "=": "-...-", ":": "---...", "'": ".----.", '"': ".-..-.",
    "(": "-.--.", ")": "-.--.-", ";": "-.-.-.", "_": "..--.-", "+": ".-.-.",
}

PROSIGNS: dict[str, str] = {
    "AR": ".-.-.", "SK": "...-.-", "BT": "-...-", "AS": ".-...",
}

_PROSIGN_RE = re.compile(r"<(AR|SK|BT|AS)>")


def dit_ms(wpm: int) -> int:
    if wpm <= 0:
        raise ValueError(f"wpm must be positive, got {wpm}")
    return round(1200 / wpm)


def _pattern_for(token: str) -> str:
    """Morse pattern for one word token; whole-word '<XX>' is a prosign."""
    t = token.upper()
    m = _PROSIGN_RE.fullmatch(t)
    if m:
        return PROSIGNS[m.group(1)]
    out: list[str] = []
    for ch in t:
        if ch in MORSE:
            out.append(MORSE[ch])
        else:
            raise ValueError(f"no Morse for character {ch!r}")
    return "".join(out)


def _units(text: str) -> list[tuple[bool, int]]:
    """Convert text to raw (on, units) steps; no trailing word gap."""
    units: list[tuple[bool, int]] = []
    for word in text.split():
        m = _PROSIGN_RE.fullmatch(word.upper())
        if m:
            patterns = [PROSIGNS[m.group(1)]]
        else:
            patterns = [_pattern_for(char) for char in word.upper()]
        for ci, pattern in enumerate(patterns):
            for si, sym in enumerate(pattern):
                units.append((True, 3 if sym == "-" else 1))
                if si < len(pattern) - 1:
                    units.append((False, 1))
            if ci < len(patterns) - 1:
                units.append((False, 3))
        units.append((False, 7))  # word gap (removed after the final word)
    if units and units[-1] == (False, 7):
        units.pop()
    return units


def schedule(text: str, wpm: int = 8, repeat: int = 1) -> list[Step]:
    if not text.split():
        raise ValueError("nothing to send (empty/whitespace text)")
    if repeat < 1:
        raise ValueError(f"repeat must be >= 1, got {repeat}")
    dit = dit_ms(wpm)
    words_units = _units(text)
    steps: list[Step] = []
    for r in range(repeat):
        if r:
            steps.append(Step(False, 7 * dit))
        steps.extend(Step(on, ms * dit) for (on, ms) in words_units)
    merged: list[Step] = []
    for s in steps:
        if merged and merged[-1].on == s.on:
            merged[-1] = Step(s.on, merged[-1].ms + s.ms)
        else:
            merged.append(Step(s.on, s.ms))
    if merged and not merged[-1].on:
        merged.pop()  # no trailing off-step; the executor guarantees restore
    return merged
