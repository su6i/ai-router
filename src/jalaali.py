"""Digit transliteration and Jalali (Persian/Solar Hijri) to Gregorian
calendar conversion for normalising `session_chunks.date`.

The conversion algorithm below is a direct port of the arithmetic in the
MIT-licensed jalaali-python reference implementation
(https://github.com/jalaali/jalaali-python), itself based on Kazimierz M.
Borkowski, "The Persian calendar for 3000 years", Earth, Moon and Planets
(1996). Ported here (no new dependency added) because only a single
conversion direction is needed for a bounded year range.
"""

import datetime
import re

# Persian-Indic (U+06F0-U+06F9) and Arabic-Indic (U+0660-U+0669) digits,
# each mapped to the equivalent ASCII digit.
_DIGIT_TRANSLATION = str.maketrans(
    {**{0x06F0 + i: chr(0x30 + i) for i in range(10)},
     **{0x0660 + i: chr(0x30 + i) for i in range(10)}}
)


def translate_digits(s: str) -> str:
    """Transliterate Persian-Indic / Arabic-Indic digits to ASCII."""
    return s.translate(_DIGIT_TRANSLATION)


_BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060,
           2097, 2192, 2262, 2324, 2394, 2456, 3178]


def _div(a: int, b: int) -> int:
    return int(a / b)


def _mod(a: int, b: int) -> int:
    return a - _div(a, b) * b


def _jal_cal(jy: int) -> dict:
    b1 = len(_BREAKS)
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    if jy < jp or jy >= _BREAKS[b1 - 1]:
        raise ValueError(f"Jalaali year out of supported range: {jy}")
    jump = 0
    for i in range(1, b1):
        jm = _BREAKS[i]
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return {"leap": leap, "gy": gy, "march": march}


def _is_leap_jalaali_year(jy: int) -> bool:
    return _jal_cal(jy)["leap"] == 0


def _g2d(gy: int, gm: int, gd: int) -> int:
    d = (_div((gy + _div(gm - 8, 6) + 100100) * 1461, 4)
         + _div(153 * _mod(gm + 9, 12) + 2, 5) + gd - 34840408)
    d = d - _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4) + 752
    return d


def _d2g(jdn: int) -> dict:
    j = (4 * jdn + 139361631
         + _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908)
    i = _div(_mod(j, 1461), 4) * 5 + 308
    gd = _div(_mod(i, 153), 5) + 1
    gm = _mod(_div(i, 153), 12) + 1
    gy = _div(j, 1461) - 100100 + _div(8 - gm, 6)
    return {"gy": gy, "gm": gm, "gd": gd}


def _jalaali_month_length(jy: int, jm: int) -> int:
    if 1 <= jm <= 6:
        return 31
    if 7 <= jm <= 11:
        return 30
    return 30 if _is_leap_jalaali_year(jy) else 29


def jalaali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int] | None:
    """Convert a Jalali calendar date to (gregorian_year, month, day).

    Returns None if `jm`/`jd` are not a valid day-of-month for that Jalali
    year, or `jy` falls outside the algorithm's supported range — never
    raises, so callers can treat None uniformly as "unparseable".
    """
    if not (1 <= jm <= 12):
        return None
    try:
        max_day = _jalaali_month_length(jy, jm)
        if not (1 <= jd <= max_day):
            return None
        r = _jal_cal(jy)
        jdn = _g2d(r["gy"], 3, r["march"]) + (jm - 1) * 31 - _div(jm, 7) * (jm - 7) + jd - 1
        g = _d2g(jdn)
    except ValueError:
        return None
    return g["gy"], g["gm"], g["gd"]


_FULL_DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_PARTIAL_DATE_RE = re.compile(r'^(\d{4})-(\d{2})$')


def normalize_stored_date(raw: str | None) -> str | None:
    """Canonicalise a date string already shaped like `YYYY-MM-DD` or
    `YYYY-MM` (any digit script) into ISO Latin-digit form.

    - A year in 1900-2200 is treated as Gregorian; validated via
      `datetime.date`, so an impossible calendar date (e.g. 2026-02-30)
      returns None rather than being stored.
    - A year in 1300-1499 is treated as Jalali and converted to Gregorian.
      Only full `YYYY-MM-DD` Jalali dates are converted: a bare Jalali
      `YYYY-MM` cannot be mapped to a single Gregorian year-month without
      a day (the calendars' month boundaries don't align), so that shape
      returns None rather than guessing.
    - Anything else (unrecognised shape, out-of-range year, invalid
      day-of-month) returns None.
    """
    if not raw:
        return None
    s = translate_digits(raw)

    m = _FULL_DATE_RE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2200:
            try:
                datetime.date(y, mo, d)
            except ValueError:
                return None
            return f"{y:04d}-{mo:02d}-{d:02d}"
        if 1300 <= y <= 1499:
            g = jalaali_to_gregorian(y, mo, d)
            if g is None:
                return None
            gy, gm, gd = g
            return f"{gy:04d}-{gm:02d}-{gd:02d}"
        return None

    m = _PARTIAL_DATE_RE.match(s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1900 <= y <= 2200 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
        return None

    return None
