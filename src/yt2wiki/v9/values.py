"""V9 exact-value extraction: shared scanners plus domestic measures.

:func:`yt2wiki.literals.extract_literals` covers technical identifiers,
paths, flags, versions and SI-style measures, but not the domestic
exact values recipe/report/review prose lives on: ``4 1/2 cups``,
``2 1/2 teaspoons``, ``100 degrees``, ``$300,000``, ``14,000``. The
type-aware validator must catch mutations of exactly these values
(King Arthur quantities, Vox prices), so this module supplements the
shared extractor without changing it: legacy pipeline behavior is
untouched, while V9 reuses :mod:`yt2wiki.literals` normalization,
matching and literal records throughout.
"""

from __future__ import annotations

import re
from typing import Any

from ..literals import Literal, dedupe_literals, extract_literals

#: Domestic unit words the shared MEASURE_RE unit list does not cover.
_COOKING_UNITS = (
    "cups?", "teaspoons?", "tablespoons?", "tsp", "tbsp", "ounces?", "oz",
    "pounds?", "lbs?", "grams?", "degrees?", "hertz", "miles?", "feet",
    "foot", "inches?", "nits", "pixels?", "liters?", "litres?",
    "milliliters?", "pints?", "quarts?", "gallons?", "pinch(?:es)?",
    "dash(?:es)?", "cloves?", "slices?", "cans?", "sticks?",
    "minutes?", "hours?", "days?", "weeks?", "months?", "years?",
    "loaves?", "loaf", "servings?", "pieces?", "sheets?", "strips?",
    "dollars?", "cents?",
)

_FRACTION = r"(?:\d+\s+)?\d+\s*/\s*\d+"
_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_NUMBER = _NUM
_RANGE = rf"{_NUM}\s*-\s*{_NUM}"
_UNIT_ALT = "|".join(_COOKING_UNITS)
#: A bare digit is never a value when it is the denominator of a
#: fraction, part of a larger number, or the tail of a range: /, . and
#: - join, they do not separate.
_NOHIT_BEFORE = r"(?<![\w./-])"

RANGE_MEASURE_RE = re.compile(
    rf"{_NOHIT_BEFORE}(?P<value>{_RANGE})\s*(?P<unit>{_UNIT_ALT})(?![\w])",
    re.IGNORECASE)
FRACTION_MEASURE_RE = re.compile(
    rf"{_NOHIT_BEFORE}(?P<value>{_FRACTION})\s*(?P<unit>{_UNIT_ALT})(?![\w])",
    re.IGNORECASE)
UNIT_MEASURE_RE = re.compile(
    rf"{_NOHIT_BEFORE}(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT_ALT})(?![\w])",
    re.IGNORECASE)
CURRENCY_RE = re.compile(
    rf"{_NOHIT_BEFORE}(?P<value>\$\d(?:[\d,]*\d)?(?:\.\d{{1,2}})?)")
COMMA_NUMBER_RE = re.compile(
    rf"{_NOHIT_BEFORE}(?P<value>\d{{1,3}}(?:,\d{{3}})+)(?![\w\d,])")


def _supplement(text: str) -> list[Literal]:
    out: list[Literal] = []
    for rx in (RANGE_MEASURE_RE, FRACTION_MEASURE_RE, UNIT_MEASURE_RE,
               CURRENCY_RE, COMMA_NUMBER_RE):
        for m in rx.finditer(text):
            # The literal is the whole match (number plus unit): "4 1/2
            # cups" and "4 1/2 grams" are different claims and must never
            # compare equal.
            value = m.group(0).strip()
            if not value:
                continue
            out.append(Literal(kind="measurement", literal=value,
                               context=text[max(0, m.start() - 40):m.end() + 40]))
    return out


def extract_exact_values(text: str, *, max_values: int = 200,
                         **kwargs: Any) -> list[Literal]:
    """Shared literals plus domestic measures, deduplicated.

    Accepts (and ignores) the ``t_start``/``t_end`` kwargs of
    :func:`extract_literals` so call sites stay interchangeable.
    """
    _ = kwargs
    if not text:
        return []
    base = extract_literals(text, max_values=max_values)
    seen = {lit.normalized or lit.literal for lit in base}
    extra = []
    for lit in _supplement(text):
        key = lit.normalized or lit.literal
        if key in seen:
            continue
        seen.add(key)
        extra.append(lit)
    return dedupe_literals(base + extra)[:max_values]
