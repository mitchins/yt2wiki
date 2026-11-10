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
#: Short abbreviations (in/ft/mm/cm/oz/lb/g/ml/px) are deliberately
#: absent: after number-word normalisation they fire on ordinary prose
#: ("factor of 4 in cost" is not 4 inches). Full words and unambiguous
#: capitals (GB/TB/nits) stay.
_COOKING_UNITS = (
    "cups?", "teaspoons?", "tablespoons?", "tsp", "tbsp", "ounces?",
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
#: Unambiguous capital/symbol units for the supplement. Single
#: lowercase letters are excluded on purpose: "in", "a" and "s" are
#: prepositions/articles/plurals far more often than units.
_SAFE_CAPS = ("V", "Hz", "%", "°C", "°F")
_UNIT_ALT = "|".join(_COOKING_UNITS + _SAFE_CAPS)
#: A bare digit is never a value when it is the denominator of a
#: fraction, part of a larger number, or the tail of a range: /, . and
#: - join, they do not separate. A minus that follows a digit is a
#: range dash, not a sign — so the sign is captured explicitly and a
#: digit never donates one.
_NOHIT_BEFORE = r"(?<![\w./])"
_SIGN = r"(?:(?<!\d)[-−])?"

RANGE_MEASURE_RE = re.compile(
    rf"{_NOHIT_BEFORE}{_SIGN}(?P<value>{_RANGE}(?:\s*(?:{_UNIT_ALT}))?)(?![\w])",
    re.IGNORECASE)
FRACTION_MEASURE_RE = re.compile(
    rf"{_NOHIT_BEFORE}{_SIGN}(?P<value>{_FRACTION})\s*(?P<unit>{_UNIT_ALT})(?![\w])",
    re.IGNORECASE)
UNIT_MEASURE_RE = re.compile(
    rf"{_NOHIT_BEFORE}{_SIGN}(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT_ALT})(?![\w])",
    re.IGNORECASE)
CURRENCY_RE = re.compile(
    rf"{_NOHIT_BEFORE}{_SIGN}(?P<value>\$\d(?:[\d,]*\d)?(?:\.\d{{1,2}})?)")
COMMA_NUMBER_RE = re.compile(
    rf"{_NOHIT_BEFORE}{_SIGN}(?P<value>\d{{1,3}}(?:,\d{{3}})+)(?![\w\d,])")
#: Bare integers are claims too ("top 10", "300 people"). Years are
#: covered separately and stay years; single digits are list numbers.
#: Clock fragments never match: a colon on either side disqualifies.
BARE_INT_RE = re.compile(
    rf"(?<![\w./:]){_SIGN}(?P<value>\d{{2,5}})(?![\w\d:])")
#: Years 1500-2099 are exact values (event dates, model years): the
#: handoff requires the gate to catch year mutations (1976 -> 1986).
#: A trailing "s" is part of the literal ("1990s"); the bare decade
#: ("90s") is emitted as a supported alias for prose that abbreviates.
#: Note the residual ambiguity is deliberate: "90s" also reads as 90
#: seconds. Both readings extract the same literal symmetrically, so a
#: seconds-vs-decade mismatch can only false-attest, never false-block;
#: the finding detail always shows the evidence span for adjudication.
YEAR_RE = re.compile(
    rf"{_NOHIT_BEFORE}{_SIGN}(?P<value>(?:1[5-9]|20)\d{{2}}s?)(?![\w\d])")


def _supplement(text: str) -> list[Literal]:
    out: list[Literal] = []
    for rx in (RANGE_MEASURE_RE, FRACTION_MEASURE_RE, UNIT_MEASURE_RE,
               CURRENCY_RE, COMMA_NUMBER_RE, YEAR_RE, BARE_INT_RE):
        for m in rx.finditer(text):
            # The literal is the whole match (number plus unit): "4 1/2
            # cups" and "4 1/2 grams" are different claims and must never
            # compare equal. Trailing sentence periods are punctuation,
            # not value text.
            value = m.group(0).strip().rstrip(".")
            if not value:
                continue
            out.append(Literal(kind="measurement", literal=value,
                               context=text[max(0, m.start() - 40):m.end() + 40]))
            # A decade alias only for an explicit decade token: "1990s"
            # also supports prose written "90s". A bare year ("1976")
            # emits no alias — "76s" would be a phantom that could
            # support an invented duration.
            if rx is YEAR_RE and re.fullmatch(r"(?:19|20)\d{2}s", value):
                out.append(Literal(kind="measurement",
                                   literal=value[2:],
                                   context=text[max(0, m.start() - 40):m.end() + 40]))
    return out


#: Imperative openers: a sentence starting with one of these verbs is
#: an executable instruction candidate. Shared with the planner so the
#: type decision can see tutorial density, not just topics.
_IMPERATIVE_VERBS = frozenset(
    ["run", "install", "configure", "set", "create", "open", "click", "type", "paste", "copy", "download", "upload", "enable", "disable", "connect", "select", "choose", "enter", "add", "remove", "delete", "edit", "save", "build", "deploy", "start", "stop", "restart", "execute", "use", "go", "navigate", "launch", "update", "change", "switch", "toggle", "check", "verify", "test", "send", "receive", "write", "read", "import", "export", "peel", "chop", "slice", "stir", "bake", "knead", "mix", "combine", "pour", "preheat", "measure", "weigh", "divide", "shape", "score", "proof", "rise", "scan", "pair", "plug"])
_IMPERATIVE_RE = re.compile(
    r"^(?:\d+[.)]\s*|[-*\u2022]\s*)?"
    r"(?:(?:then|next|now|also|just|simply|please)\s+)?(\w+)\b",
    re.IGNORECASE)
_STEP_MARK = re.compile(r"\bstep\s+\d+\b", re.IGNORECASE)


def count_actions(text: str) -> int:
    """Deterministic executability signal for one span of transcript.

    Counts imperative sentences ("Set your allow list"), explicit step
    markers and fenced command blocks. The planner uses the density to
    tell executable tutorials (procedure) from conceptual ones
    (explainer); the writer's own prompts remain the authority on prose.
    """
    if not text:
        return 0
    hits = 0
    prose = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    for sent in re.split(r"[.!?]+\s*", prose):
        m = _IMPERATIVE_RE.match(sent.strip())
        if m and m.group(1).lower() in _IMPERATIVE_VERBS:
            hits += 1
    hits += len(_STEP_MARK.findall(prose))
    hits += text.count("```") // 2
    return hits


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
