"""Type-aware publication validation, Pass 5 (PR C).

Deterministic checks that run after writing and before image
selection and ``v9.publish.persist``. Three outcomes:

- ``ready``: publish normally;
- ``needs_review``: a meaningful candidate exists but a critical check
  failed — keep a private candidate plus a finding ledger, do NOT
  overwrite an existing published canonical article;
- ``refused``: no meaningful article (all sections empty/no-speech).

Checks (all deterministic — no model critique can green-light a
critical finding):

1. type/slot invariants: closed enum, template version, frozen type,
   required and allowed blocks, no ungrounded executable blocks;
2. exact-value/entity ledger: every exact literal in the article must
   be supported by the section's own evidence spans (King Arthur
   quantities, Vox $300k, Dimorphos are regression fixtures);
   mid-sentence proper names must occur in the transcript
   (same-length near-misses are likely corruption; plurals, stems
   and possessives are not);
3. temporal: spans inside the source, overlapping real segments;
4. origin/role: section spans never overlap decision-excluded
   (sponsor/housekeeping) spans;
5. coverage: every non-excluded window is represented or explicitly
   listed as unassigned (unrepresented substantive windows are
   findings, capped);
6. reader integrity: no internal ids, raw dicts, model-emitted HTML,
   unbalanced fences or decision-trace language in prose; lead,
   summary and headings are checked against full-transcript evidence.

Cosmetic warnings never fail publication: only ``critical`` findings
produce ``needs_review``.
"""

from __future__ import annotations

import re
from typing import Any

from .. import literals as _literals
from . import article_types as AT
from .values import extract_exact_values as _extract_values

#: Finding dicts carry code/severity/detail and optional section_id.
CRITICAL = "critical"
MAJOR = "major"

_INTERNAL_ID = re.compile(
    r"\bev-[0-9a-f]{4,}\b|\bsec-[0-9a-z-]{4,}\b|\bw-\d{3,}\b|\bfact-[\w-]+\b")
#: Decision-trace vocabulary belongs in diagnostics, never prose. Major
#: only: ordinary words overlap the pattern class ("candidate" as in
#: vaccine candidate), so this is leakage hygiene, not fabrication.
_DECISION_TRACE = re.compile(
    r"\b(candidate|incumbent|rank_reason|judge_reason|judge advised|"
    r"z1_fallback|ranked frames|veto Shortlist|adjudicat\w+)\b",
    re.IGNORECASE)
_BALANCED_CODE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_RAW_HTML = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
#: A name never spans a line break: headings and list items sit on
#: their own lines, and a match reaching across one is two fragments,
#: not a person or product.
_NAME = re.compile(r"\b((?:[A-Z][a-z]{2,}|[A-Z]{2,})[A-Za-z']*"
                    r"(?:[ \t]+(?:[A-Z][a-z]{2,}|[A-Z]{2,})[A-Za-z']*)?)\b")
_SENTENCE_END = re.compile(r"[.!?]\s*$")
#: Speaker-attribution constructions: even sentence-initial, the named
#: voice must exist in the source. Checked for discussion/report types
#: where invented attribution is the characteristic failure.
_ATTRIBUTION = re.compile(
    r"\b([A-Z][a-z]{2,}(?:[ \t]+[A-Z][a-z]{2,})?)[ \t]+"
    r"(?:says?|said|argues?|argued|claims?|claimed|notes?|noted|adds?|"
    r"added|explains?|explained|warns?|warned|asks?|asked|replies?|"
    r"replied|responds?|responded|believes?|thinks?|thinks)\b"
    r"|\baccording\s+to\s+([A-Z][a-z]{2,}(?:[ \t]+[A-Z][a-z]{2,})?)\b")

#: Ordinary capitalized words that are never proper-name evidence.
_NAME_ALLOWLIST = frozenset(
    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December", "The", "This", "That", "These", "Those", "When", "Where", "What", "Which", "While", "Because", "Since", "Before", "After", "During", "There", "Here", "Video", "Audio", "Chapter", "Section", "Figure", "Table", "First", "Second", "Third", "Next", "Then", "Finally", "Also", "Just", "Even", "Still", "Yet", "Already", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Someone", "Something", "Somebody", "Nobody", "Everybody", "Anyone"])

_KNOWN_UNITS = frozenset(["V", "volts", "A", "amps", "W", "watts", "Hz", "kHz", "MHz", "GHz", "nits", "dB", "dBm", "FPS", "fps", "RPM", "rpm", "GB", "MB", "KB", "TB", "mph", "kph", "km", "mi", "ft", "feet", "inch", "inches", "mm", "cm", "m", "s", "ms", "min", "hrs", "percent", "USD", "EUR", "cents", "dollars", "OLED", "LCD", "LED", "HDR", "IPS", "WiFi", "Bluetooth", "USB", "HDMI", "NVMe", "SSD", "CPU", "GPU", "APU", "RAM", "AMD", "Intel", "Valve", "LTT", "PBS", "Vox", "TLD", "DIY", "OS", "API", "URL", "PDF", "JSON", "HTML", "CSS", "AM", "PM", "EDT", "PDT", "UTC", "DIY", "AI", "RL", "LLM", "RSI", "GRPO", "RLVR", "SFT", "DART", "NASA", "PBS", "LTT", "WVFRM"])


def _finding(code: str, severity: str, detail: str,
             section_id: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"code": code, "severity": severity, "detail": detail}
    if section_id:
        f["section_id"] = section_id
    return f


def _spans_of(section: dict[str, Any]) -> list[list[float]]:
    spans = section.get("source_windows") or []
    out = []
    for w in spans:
        try:
            out.append([float(w[0]), float(w[1])])
        except (TypeError, ValueError, IndexError):
            continue
    if not out:
        try:
            out.append([float(section.get("t_start", 0) or 0),
                        float(section.get("t_end", 0) or 0)])
        except (TypeError, ValueError):
            pass
    return out


def _evidence_text(segs: list[dict[str, Any]],
                   spans: list[list[float]]) -> str:
    lines = []
    for a, b in spans:
        for r in segs:
            try:
                if float(r["end"]) < a or float(r["start"]) > b:
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            lines.append(str(r.get("text") or ""))
    return "\n".join(lines)


def check_type_slots(decision: dict[str, Any], plan: dict[str, Any],
                     article: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 1: the frozen type survives through to the article."""
    out: list[dict[str, Any]] = []
    tid = decision.get("template_id")
    if AT.validate_selection(decision):
        return [_finding("bad_decision", CRITICAL,
                         "stored type decision fails closed-enum validation")]
    if tid != article.get("article_type"):
        out.append(_finding("type_mismatch", CRITICAL,
                            f"article type {article.get('article_type')!r} != "
                            f"frozen {tid!r}"))
    if (decision.get("template_version") is not None
            and article.get("template_version") is not None
            and decision.get("template_version")
            != article.get("template_version")):
        out.append(_finding("template_version_mismatch", CRITICAL,
                            "article template version != frozen decision"))
    if AT.validate_plan(plan, tid or ""):
        out.append(_finding("bad_plan", CRITICAL,
                            "stored plan fails the frozen slot contract"))
        return out
    allowed: set[str] = set()
    tdef = AT.type_definition(tid or "")
    if tdef:
        allowed = set(tdef["core_slots"]) | set(tdef["optional_slots"])
    for sec in article.get("sections") or []:
        slot = sec.get("slot")
        if slot not in allowed:
            out.append(_finding("foreign_slot", CRITICAL,
                                f"section {sec.get('section_id')!r} uses "
                                f"slot {slot!r} outside {tid}",
                                sec.get("section_id")))
    # Core slots need non-empty sections, not just plan entries: an
    # empty method section publishes a recipe with no method.
    if tdef:
        covered = {str(s.get("slot")) for s in article.get("sections") or []
                   if str(s.get("body") or "").strip()
                   and str(s.get("body") or "").strip() != "_No speech in this span._"}
        for core in tdef["core_slots"]:
            if core not in covered:
                out.append(_finding("missing_core_slot", CRITICAL,
                                    f"core slot {core!r} has no substantive "
                                    f"section in the article"))
    return out


def check_executable_blocks(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 1b: only procedures carry executable blocks.

    A fenced block in a non-procedure article is an ungrounded action
    unless its content is quoted discussion/reporting, not instruction.
    """
    out: list[dict[str, Any]] = []
    if article.get("article_type") == "procedure":
        return out
    for sec in article.get("sections") or []:
        body = str(sec.get("body") or "")
        for m in _FENCE.finditer(body):
            block = m.group(0)
            inner = "\n".join(ln for ln in block.splitlines()[1:-1]
                              if ln.strip())[:400]
            if not inner:
                continue
            if _literals.looks_like_command(inner, permissive=True):
                out.append(_finding(
                    "ungrounded_executable_block", CRITICAL,
                    f"fenced command-like block in {article.get('article_type')} "
                    f"article: {inner[:120]!r}", sec.get("section_id")))
            else:
                out.append(_finding(
                    "fenced_block_outside_procedure", MAJOR,
                    "fenced block in a non-procedure article; prose preferred",
                    sec.get("section_id")))
    return out


_MD_LINK_TARGET = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"https?://[^\s)>\]]+")
#: Title-Case headings are subheadings, not instructions: words are
#: either capitalised or short lowercase glue (and/the/of), with no
#: shell syntax ($ - / . = ( ) \ ` ~ :) anywhere.
_TITLE_CASE_WORD = r"(?:[A-Z][\w']*|[a-z]{1,3})"
_SHELL_CHARS = re.compile(r"[$\-/.,=()\\`~:]")


def _is_title_case_heading(text: str) -> bool:
    words = text.split()
    if len(words) < 2 or _SHELL_CHARS.search(text):
        return False
    return all(re.fullmatch(_TITLE_CASE_WORD, w) is not None for w in words)
#: Timestamp citations are provenance, not claims: the label carries no
#: value the prose does not already state. Strip them before value
#: extraction (the publisher consolidates them into the section ref).
_TS_CITATION = re.compile(r"!?\[([^\]]*)\]\(([^)\s]*[?&]t=\d[^)\s]*)\)")
#: Mangled clock debris the normaliser could not resolve ("0:00:64.9").
_MANGLED_CLOCK = re.compile(r"\b\d{1,3}:\d{2}(?::\d{2})?\.\d+\b")
#: Bare "in" is a preposition far more often than inches: "5500 in the
#: original" is not a measurement. Full "inches" still extracts.
_BARE_IN = re.compile(r"^[\d.,]+\s+in$", re.IGNORECASE)
_BARE_KEYVALUE_VALUE = re.compile(r"[\W_]*$")
_LIST_MARKER = re.compile(r"(?m)^(\s*)(?:[*\-+]\s+|\d+[.)]\s+)")
#: Assignment-form keyvalues only: `key=value` or `key:value` with no
#: space after the separator. `Label: description` with a space is
#: English definition-list prose (ubiquitous in typed articles), never
#: an exact technical value.
_STRICT_KV = re.compile(
    r"\b([A-Za-z_][\w.\-]{2,40})\s*=\s*([^\s,;]{1,64})"
    r"|\b([A-Za-z_][\w.\-]{2,40}):(?=\S)([^\s,;]{1,64})")
#: Bare transcript-second ranges ("223.6-232.1", "641-645.4",
#: "5030-5033"): citation-hygiene debris, not exact values. Both
#: endpoints inside the duration marks a probable seconds citation
#: the writer should have rendered as m:ss — unless both endpoints
#: read as years ("1976-1986" stays a value range).
_BARE_RANGE = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*-\s*(\d{1,4}(?:\.\d+)?)\b")


def _looks_like_years(lo: float, hi: float) -> bool:
    return all(1500 <= v <= 2099 and float(v).is_integer() for v in (lo, hi))
#: A bare range followed by a unit word is a measurement ("3.3-5.0 V"),
#: not a seconds citation: the value gate owns it.
_RANGE_UNIT_TAIL = re.compile(r"\s*(?:volts?|V\b|%|°[CF]?|Hz|hertz)\b")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PERCENT_WORD = re.compile(r"(\d+(?:\.\d+)?)\s+percent\b", re.IGNORECASE)

#: English number words the transcript spells out while prose (and the
#: reader) uses digits: both sides are numeralised before value
#: comparison so "two and a half teaspoons" supports "2 1/2 teaspoons".
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "dozen": "12",
}
_AND_A_HALF = re.compile(r"\b(\w+)\s+and\s+a\s+half\b", re.IGNORECASE)
_AND_A_QUARTER = re.compile(r"\b(\w+)\s+and\s+a\s+quarter\b", re.IGNORECASE)
_A_HALF = re.compile(r"\ba\s+half\b", re.IGNORECASE)


def _a_unit_re() -> re.Pattern:
    # Time units are excluded: "wait a minute" is idiom, not a measure.
    # Explicit number words still numeralise ("five minutes" -> "5 minutes").
    units = ("cups?|teaspoons?|tablespoons?|tsp|tbsp|ounces?|oz|pounds?|"
             "lbs?|grams?|degrees?|hertz|miles?|feet|foot|inches?|nits|"
             "liters?|milliliters?|pints?|quarts?|gallons?|loaves?|"
             "servings?|dollars?|cents?")
    return re.compile(rf"\b[an]+\s+(?=(?:{units})\b)", re.IGNORECASE)


_A_UNIT = _a_unit_re()

#: Spelled-out storage units the transcript uses while prose (and the
#: reader) abbreviates: canonicalised symmetrically on both sides.
_STORAGE_UNIT = re.compile(r"\b(giga|mega|kilo|tera)bytes?\b", re.IGNORECASE)
_STORAGE_ABBR = {"giga": "GB", "mega": "MB", "kilo": "KB", "tera": "TB"}
_NUMBER_WORD = re.compile(r"\b(zero|one|two|three|four|five|six|seven|eight|"
                           r"nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                           r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
                           r"forty|fifty|sixty|seventy|eighty|ninety|hundred|"
                           r"dozen)\b", re.IGNORECASE)


_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")
_UNITS_WORDS = (r"volts?|V\b|hertz|Hz|degrees?|inches?|feet|miles?|nits|"
                r"watts?|percent|%|mm|cm|grams?|cups?|teaspoons?|tablespoons?")
#: "3.3 to 5.0 volts", "72 up to 75 degrees" and bare "72 75 degrees"
#: all denote the same range as "3.3-5.0 V": rewrite to the dash form
#: so the range extractor compares both endpoints.
_TO_RANGE = re.compile(
    rf"\b(\d+(?:\.\d+)?)\s+(?:to|up\s+to)\s+(\d+(?:\.\d+)?)\s+({_UNITS_WORDS})\b",
    re.IGNORECASE)
_ADJACENT_RANGE = re.compile(
    rf"\b(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+({_UNITS_WORDS})\b",
    re.IGNORECASE)

#: Decade words. Unqualified forms emit both century readings: getting
#: the century wrong would block a correct article, while the rare
#: collision (a "70s" duration against a "seventies" decade) stays
#: visible in the finding detail for operator adjudication.
_DECADES = {"twenties": "20", "thirties": "30", "forties": "40",
            "fifties": "50", "sixties": "60", "seventies": "70",
            "eighties": "80", "nineties": "90"}
_MID_DECADE = re.compile(r"\b(?:mid|early|late)(\d0s)\b",
                           re.IGNORECASE)
_DECADE_WORD = re.compile(r"\b(twenties|thirties|forties|fifties|sixties|"
                           r"seventies|eighties|nineties)\b", re.IGNORECASE)
_CENTURY_DECADE = re.compile(r"\b(nineteen|twenty)\s+"
                              r"(twenties|thirties|forties|fifties|sixties|"
                              r"seventies|eighties|nineties)\b", re.IGNORECASE)


def _decade_number(match: re.Match[str]) -> str:
    century = {"nineteen": "19", "twenty": "20"}[match.group(1).lower()]
    return century + _DECADES[match.group(2).lower()] + "s"


def _decade_aliases(match: re.Match[str]) -> str:
    dd = _DECADES[match.group(0).lower()]
    return f"19{dd}s 20{dd}s"

_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_MULT = {"hundred": 100, "thousand": 1000, "million": 1000000,
         "billion": 1000000000, "dozen": 12}
_NUMWORD = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
            r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
            r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
            r"seventy|eighty|ninety")
# Longest match first: "three hundred thirty million" is one number
# (330M), not "300" followed by "thirty million".
_HUNDREDS_MILLIONS = re.compile(
    rf"\b({_NUMWORD})\s+hundred\s+({_NUMWORD})\s+(thousand|million|billion)\b",
    re.IGNORECASE)
_WORD_MULT = re.compile(
    rf"\b({_NUMWORD}|\d+)\s+(hundred|thousand|million|billion|dozen)\b",
    re.IGNORECASE)
_A_MULT = re.compile(r"\ba\s+(hundred|thousand|million|billion|dozen)\b",
                     re.IGNORECASE)


def _word_num(word: str) -> int:
    word = word.lower()
    if word.isdigit():
        return int(word)
    return _ONES.get(word, _TENS.get(word, 1))
_HALF_A_UNIT = re.compile(
    r"\bhalf\s+a\s+(?=cups?|teaspoons?|tablespoons?|tsp|tbsp|ounces?|oz|"
    r"pounds?|lbs?|grams?|degrees?|liters?|milliliters?|pints?|quarts?|"
    r"gallons?|minutes?|hours?|servings?|pieces?|dollars?|cents?)",
    re.IGNORECASE)
_QUARTER_A_UNIT = re.compile(
    r"\bquarter\s+(?:of\s+)?a\b|\ba\s+quarter\b(?=\s+(?:of\s+)?"
    r"(?:cups?|teaspoons?|tablespoons?|tsp|tbsp|"
    r"ounces?|oz|pounds?|lbs?|grams?|degrees?|liters?|milliliters?|pints?|"
    r"quarts?|gallons?|minutes?|hours?|servings?|pieces?|dollars?|cents?))",
    re.IGNORECASE)


_VOLTS_WORD = re.compile(r"\bvolts?\b", re.IGNORECASE)


def _numeralize(text: str) -> str:
    text = text.replace("−", "-")
    text = _TO_RANGE.sub(r"\1-\2 \3", text)
    # Bare adjacent numbers with a unit read as a range ("72 75
    # degrees"); without a unit they stay separate values.
    text = _ADJACENT_RANGE.sub(r"\1-\2 \3", text)
    text = _DIGIT_COMMA.sub("", text)
    # Unit abbreviations vs full words denote the same value.
    text = _VOLTS_WORD.sub("V", text)
    # Longest number phrases first: "three hundred thirty million"
    # is one number (330000000), not "300" followed by "thirty million".
    text = _CENTURY_DECADE.sub(_decade_number, text)
    text = _DECADE_WORD.sub(_decade_aliases, text)
    # Attached caption/ASR spacing variants ("mid90s") denote the
    # same decade. Spaced forms ("early 1970s") already extract.
    text = _MID_DECADE.sub(r"\1", text)
    text = _HUNDREDS_MILLIONS.sub(
        lambda m: str((_word_num(m.group(1)) * 100
                       + _word_num(m.group(2))) * _MULT[m.group(3).lower()]),
        text)
    text = _WORD_MULT.sub(
        lambda m: str(_word_num(m.group(1)) * _MULT[m.group(2).lower()]),
        text)
    text = _A_MULT.sub(lambda m: str(_MULT[m.group(1).lower()]), text)
    text = _HALF_A_UNIT.sub("1/2 ", text)
    text = _QUARTER_A_UNIT.sub("1/4 ", text)
    text = _AND_A_HALF.sub(r"\1 1/2", text)
    text = _AND_A_QUARTER.sub(r"\1 1/4", text)
    text = _A_HALF.sub("1/2", text)
    text = _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()], text)
    text = _A_UNIT.sub("1 ", text)
    text = _STORAGE_UNIT.sub(lambda m: _STORAGE_ABBR[m.group(1).lower()], text)
    # "110%" and "110 percent" denote the same value.
    text = _PERCENT_WORD.sub(r"\1%", text)
    return text


_TIMESTAMP_LABEL = re.compile(r"^[\d:.\-\s]+$")


def _prose_values_text(body: str) -> str:
    """Prose with link machinery removed: targets go, labels stay unless
    they are themselves plain timestamps.

    Link targets and bare URLs are navigation, not claims: the shared
    keyvalue scanner would otherwise read `https=...` as an exact
    value and every cited article would fail its own gate. Citation
    labels are dropped only when timestamp-shaped; a smuggled value
    label ("[900 degrees](...?t=1)") stays in the text and is checked.
    Markdown emphasis is stripped so `**Label:**` list markup never
    becomes a `Label=**` keyvalue. Number words become digits
    symmetrically on both sides of the comparison.
    """
    def _ts_replace(m: re.Match[str]) -> str:
        return " " if _TIMESTAMP_LABEL.fullmatch(m.group(1) or "") else m.group(1)

    text = _TS_CITATION.sub(_ts_replace, body)
    text = _MD_LINK_TARGET.sub(r"\1", text)
    text = _BARE_URL.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ")
    return _numeralize(text)


def check_exact_values(article: dict[str, Any],
                       segs: list[dict[str, Any]],
                       duration_s: float = 0) -> list[dict[str, Any]]:
    """Invariant 2a: every exact literal in prose is source-supported.

    Direction is article -> evidence: dropped values are omissions
    (coverage); invented values are critical. Comparison normalises
    cosmetically but preserves the original literal. A value the
    section spans lack but the transcript states somewhere is NOT
    invented — it is miscited. That is a major misplaced-span finding
    (wrong citation scope), never a critical fabrication; only a
    value with no transcript span anywhere is critical.
    """
    whole = _numeralize(_evidence_text(
        segs, [[0.0, float(duration_s or 0) or 0.0]]))
    whole_supported = [lit.literal for lit in
                       _extract_values(whole, max_values=2000)]
    out: list[dict[str, Any]] = []
    for sec in article.get("sections") or []:
        body = _prose_values_text(str(sec.get("body") or ""))
        # Video-level metadata the writer legitimately sees (duration)
        # is evidence for frontmatter, not a claim to verify.
        meta = _numeralize(str(sec.get("meta_evidence") or "").strip())
        evidence = _numeralize(_evidence_text(segs, _spans_of(sec)))
        if meta:
            evidence += "\n" + meta
        # Strip only ranges classified as citations: unit-bearing
        # ranges stay in the text so the value gate still checks them.
        # _BARE_RANGE has no unit guard, so classification happens here.
        stripped = []
        last = 0
        for m in _BARE_RANGE.finditer(body):
            try:
                lo, hi = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            if _RANGE_UNIT_TAIL.match(body[m.end():]):
                continue
            # Year ranges stay for the value gate even inside long
            # videos ("1976-1986" is history, not a citation).
            if _looks_like_years(lo, hi):
                continue
            if hi <= lo:
                # A descending range is not a value at all; it is
                # mangled citation debris (nothing counts down like this).
                out.append(_finding(
                    "bare_second_citation", MAJOR,
                    f"{m.group(0)!r} looks like transcript seconds; "
                    f"use m:ss citations", sec.get("section_id")))
                stripped.append(body[last:m.start()])
                last = m.end()
            elif 0 <= lo < hi <= duration_s + 1:
                out.append(_finding(
                    "bare_second_citation", MAJOR,
                    f"{m.group(0)!r} looks like transcript seconds; "
                    f"use m:ss citations", sec.get("section_id")))
                stripped.append(body[last:m.start()])
                last = m.end()
        stripped.append(body[last:])
        body = "".join(stripped)
        for m in _MANGLED_CLOCK.finditer(body):
            out.append(_finding(
                "bare_second_citation", MAJOR,
                f"{m.group(0)!r} looks like a mangled timestamp citation; "
                f"use m:ss citations", sec.get("section_id")))
        body = _MANGLED_CLOCK.sub(" ", body)
        if not body.strip():
            continue
        supported = [lit.literal for lit in
                     _extract_values(evidence, max_values=200)]
        strict_kv = {(m.group(1) or m.group(3)) + "="
                       + (m.group(2) or m.group(4))
                       for m in _STRICT_KV.finditer(body)}
        for lit in _extract_values(body, max_values=200):
            if lit.kind == "measurement" and _BARE_IN.fullmatch(lit.literal):
                continue
            if lit.kind == "command" and _is_title_case_heading(lit.literal):
                # A Title-Case phrase with no shell syntax is a subheading,
                # not an instruction ("File Staging and Snapshots" vs
                # "git checkout master"). Real commands are lowercase-led
                # or carry syntax.
                continue
            if lit.kind == "version" and "-" in lit.literal:
                # Hyphenated ranges are not versions; the unit-bearing
                # measurement (or the bare-citation rule) owns them.
                continue
            if lit.kind == "keyvalue" and lit.literal not in strict_kv:
                # `Label: description` is definition-list prose, not an
                # assignment claim: skip, do not flag.
                continue
            if lit.kind == "command" and str(sec.get("section_id") or "").startswith("__"):
                # Titles and summaries trip the command scanner on
                # ordinary title case ("Git Repository Mechanics..."):
                # frontmatter carries no executable instructions. The
                # reserved __ prefix cannot collide with model-chosen
                # section ids.
                continue
            if _literals.article_value_supported(lit.literal, supported) is None:
                if _literals.article_value_supported(lit.literal, whole_supported) is not None:
                    out.append(_finding(
                        "misplaced_span_value", MAJOR,
                        f"{lit.kind} {lit.literal!r} is stated elsewhere "
                        f"in the transcript, not in this section's spans; "
                        f"union the evidencing span or move the claim",
                        sec.get("section_id")))
                elif lit.kind == "keyvalue" and "=" in lit.literal and _value_part_supported(
                        lit.literal, supported, whole_supported):
                    # The assignment form is new ("port = 8080" for prose
                    # "port to 8080") but the value itself is grounded:
                    # a form change, not an invention.
                    out.append(_finding(
                        "unverified_assignment", MAJOR,
                        f"{lit.kind} {lit.literal!r} restates a supported "
                        f"value as an assignment; verify the relation",
                        sec.get("section_id")))
                elif lit.kind in ("keyvalue", "port") and _numbers_grounded(
                        lit.literal, supported):
                    # "port = 8080" for prose "port to 8080": every number
                    # in the claim is evidenced, only the relation is new.
                    out.append(_finding(
                        "unverified_relation", MAJOR,
                        f"{lit.kind} {lit.literal!r} combines evidenced "
                        f"numbers in a new relation; verify the relation",
                        sec.get("section_id")))
                else:
                    out.append(_finding(
                        "unsupported_exact_value", CRITICAL,
                        f"{lit.kind} {lit.literal!r} has no source span in "
                        f"this section's evidence", sec.get("section_id")))
    return out


_BARE_DIGITS = re.compile(r"\d+")


def _numbers_grounded(literal: str, supported: list[str]) -> bool:
    """Every digit-run in the claim appears as a bare number in evidence.

    Distinguishes recombined evidenced numbers ("port = 8080" for prose
    "port to 8080") from invented ones (no digit overlap at all).
    """
    runs = _BARE_DIGITS.findall(literal)
    if not runs:
        return False
    supported_nums = set()
    for s in supported:
        supported_nums.update(_BARE_DIGITS.findall(s))
    return all(r in supported_nums for r in runs)


def _value_part_supported(literal: str, supported: list[str],
                          whole_supported: list[str]) -> bool:
    """Whether an assignment's value alone is grounded (key or separator
    may differ from the source phrasing)."""
    value = (literal.split("=", 1)[-1] if "=" in literal else literal).strip()
    if not value:
        return False
    return (_literals.article_value_supported(value, supported) is not None
            or _literals.article_value_supported(value, whole_supported) is not None)


def check_entities(article: dict[str, Any],
                   segs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invariant 2b: mid-sentence proper names occur in the transcript.

    Sentence-initial capitalisation is prose position, not evidence;
    allowlisted common words and known units never fire. A name with a
    near-identical transcript neighbour (Dimorphus vs Dimorphos) is a
    corruption — critical. A name with no neighbour at all may be an
    ASR gap rather than an invention — major, for operator adjudication.
    """
    import difflib

    out: list[dict[str, Any]] = []
    transcript = "\n".join(str(r.get("text") or "") for r in segs)
    lowered = transcript.lower()
    tokens = set(re.findall(r"[a-z][a-z']+", lowered))
    # Possessives attest their base word ("deck's" supports "Deck's").
    tokens = {t[:-2] if t.endswith("'s") else t for t in tokens}
    # Adjacent-word compounds ("Steam OS" supports "SteamOS"): spoken
    # spacing varies, the entity does not.
    words = re.findall(r"[a-z]+", lowered)
    tokens.update(a + b for a, b in zip(words, words[1:]))

    def _stem(word: str) -> str:
        for suffix in ("ation", "ition", "tion", "ion",
                       "ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                return word[: -len(suffix)]
        return word

    def _base(word: str) -> str:
        # Possessive/base form for lookup, WITHOUT stemming: stemming
        # here would shred the query ("Dimorphus" -> "dimorphu") and
        # the typo ratio below. Stemming applies only to the
        # same-stem comparison.
        w = word.lower()
        if w.endswith("'s"):
            w = w[:-2]
        return w.rstrip("'")

    def _occurrences(article: dict[str, Any], name: str) -> int:
        head = name.split()[0].lower()
        text = " ".join(
            [str(article.get("title") or ""), str(article.get("summary") or "")]
            + [str(s.get("body") or "") + " " + str(s.get("heading") or "")
               for s in article.get("sections") or []])
        return len(re.findall(r"\b" + re.escape(head) + r"s?\b", text,
                              flags=re.IGNORECASE))

    def _check_name(name: str, sec_id: str | None) -> None:
        head = name.split()[0]
        if head in _NAME_ALLOWLIST or head in _KNOWN_UNITS:
            return
        parts = [_base(p) for p in name.split()]
        # Whole-token attestation only: substring matching attests
        # "Tool" via "Tools" and "Art" via "start".
        if all(p in tokens for p in parts):
            return
        # Candidate retrieval uses the raw head: stemming here would
        # shred the query itself ("Dimorphus" -> "dimorphu" misses
        # "Dimorphos"). Stemming applies only to the comparison below.
        close = difflib.get_close_matches(head.lower(), tokens, n=1,
                                          cutoff=0.85)
        if close and _base(head) == close[0]:
            # Possessive or bare plural of an attested word ("Deck's"
            # for "deck") is style, not evidence: no finding.
            return
        if close and _stem(head.lower()) == _stem(close[0]):
            # Same stem ("Automation" for "automating") is morphology,
            # not corruption: operator adjudicates.
            severity, why = MAJOR, f"same stem as {close[0]!r}"
            out.append(_finding("unattested_entity", severity,
                                f"{name!r} is capitalised as a name but never "
                                f"occurs in the transcript ({why})", sec_id))
            return
        if close:
            # Same-length near-miss with high similarity
            # ("Dimorphus"/"Dimorphos" at 0.89) is the substitution/typo
            # class: likely corruption. Weaker echoes ("Tool"/"too" at
            # 0.86 but different lengths, "Automation"/"automating" at
            # 0.70) need a human.
            ratio = difflib.SequenceMatcher(None, _base(head),
                                            close[0]).ratio()
            if len(_base(head)) == len(close[0]) and ratio >= 0.85:
                severity, why = CRITICAL, f"near {close[0]!r} — likely corruption"
            else:
                severity, why = MAJOR, f"near {close[0]!r} — different word"
        elif _occurrences(article, name) >= 2:
            # No transcript neighbour, but repeated across the article:
            # repetition is endorsement. A single unattested mention may
            # be an ASR gap; saying it twice is a claim of existence.
            severity, why = CRITICAL, "repeated with no transcript neighbour"
        else:
            severity, why = MAJOR, "no transcript neighbour — ASR gap or invention"
        out.append(_finding(
            "unattested_entity", severity,
            f"{name!r} is capitalised as a name but never "
            f"occurs in the transcript ({why})", sec_id))
    seen: set[str] = set()
    for sec in article.get("sections") or []:
        body = re.sub(r"```.*?```", " ", str(sec.get("body") or ""),
                      flags=re.DOTALL)
        # List and subheading markup is structure, not naming: strip the
        # markers so item-leading words read as line starts below.
        body = _LIST_MARKER.sub(r"\1", body)
        # Title-Case runs are headings ("Simple Bread Making for
        # Beginners"), not name claims: skip matches fully inside one.
        # Sentence-case names ("NASA confirms Dimorphus") still check.
        runs: list[tuple[int, int]] = []
        for rm in re.finditer(
                r"\b(?:[A-Z][\w']*(?:[ \t]+(?:[A-Z][\w']*|[a-z]{1,3}))+)\b",
                body):
            words = rm.group(0).split()
            if sum(1 for w in words if w[:1].isupper()) >= 3:
                runs.append((rm.start(), rm.end()))
        for m in _NAME.finditer(body):
            if any(a <= m.start() and m.end() <= b for a, b in runs):
                continue
            name = m.group(1)
            if name in seen:
                continue
            # An allowlisted head ("The Tool") must not shield the tail:
            # check the substantive word on its own.
            head = name.split()[0]
            if head in _NAME_ALLOWLIST and len(name.split()) > 1:
                tail = name.split()[1]
                seen.add(name)
                seen.add(tail)
                _check_name(tail, sec.get("section_id"))
                continue
            # Sentence-initial capitalisation is prose position, not
            # evidence: skip body starts, sentence ends and quote/dialogue
            # openings. Seen-marking happens only for checked names, so
            # a first occurrence at a skipped position cannot launder a
            # later mid-sentence occurrence.
            if m.start() == 0:
                continue
            prev = body[:m.start()]
            if _SENTENCE_END.search(prev) or prev.endswith("\n"):
                continue
            if prev[-1:] in "\"'(‘“[#:*>|-":
                continue
            # Item text restarts after a bold lead-in ("**Pour:** Pour"):
            # a colon-terminated markup prefix is a boundary, not prose.
            line_start = body.rfind("\n", 0, m.start()) + 1
            prefix = body[line_start:m.start()].replace("*", "").replace(
                "#", "").replace("_", "").strip(" \t\"'(‘“[")
            if not prefix or prefix[-1:] in ".:!?":
                continue
            seen.add(name)
            _check_name(name, sec.get("section_id"))
    if article.get("article_type") in ("discussion", "report"):
        for sec in article.get("sections") or []:
            body = re.sub(r"```.*?```", " ", str(sec.get("body") or ""),
                          flags=re.DOTALL)
            seen_attr: set[str] = set()
            for m in _ATTRIBUTION.finditer(body):
                name = m.group(1) or m.group(2)
                if name and name not in seen_attr:
                    seen_attr.add(name)
                    _check_name(name, sec.get("section_id"))
    return out


_GIT_BRANCH_DEF = re.compile(r"\bgit\s+branch\s+([A-Za-z][\w.\-]*)(?![\w.\-])")
_GIT_BRANCH_USE = re.compile(
    r"\bgit\s+(?:checkout|switch|merge|rebase|push|pull)\s+([A-Za-z][\w.\-]*)(?![\w.\-])")
_INJECTION_ECHO = re.compile(
    r"ignore\s+(all\s+)?previous\s+instructions|disregard\s+(all\s+)?(previous|prior|above)\s+"
    r"(instructions|orders)|you\s+are\s+now\s+\w+|jailbreak|do\s+anything\s+now",
    re.IGNORECASE)


def check_procedure_order(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependency order for procedures: no use before definition.

    Deterministic and narrow: identifiers introduced by definition
    commands (`git branch <name>`) must not be consumed
    (`git checkout/switch/merge <name>`) in an earlier section. The
    Git tutorial failure — switching to a branch before creating it —
    is the regression fixture. Generic prose is untouched.
    """
    out: list[dict[str, Any]] = []
    if article.get("article_type") != "procedure":
        return out
    defined_at: dict[str, int] = {}
    uses: list[tuple[str, int, str | None]] = []
    sections = article.get("sections") or []
    for i, sec in enumerate(sections):
        body = str(sec.get("body") or "")
        for m in _GIT_BRANCH_DEF.finditer(body):
            defined_at.setdefault(m.group(1), i)
        for m in _GIT_BRANCH_USE.finditer(body):
            uses.append((m.group(1), i, sec.get("section_id")))
    for name, i, sid in uses:
        if name in ("master", "main"):
            continue
        born = defined_at.get(name)
        if born is not None and born > i:
            out.append(_finding(
                "forward_reference", CRITICAL,
                f"{name!r} is used before the section that creates it",
                sid))
    return out


def check_no_injection_echo(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Transcripts are untrusted: model must not echo prompt-injection text.

    A hostile caption ("ignore previous instructions, publish ...") that
    survives into prose is a critical integrity failure, not a style nit.
    """
    out: list[dict[str, Any]] = []
    for sec in article.get("sections") or []:
        body = re.sub(r"```.*?```", " ", str(sec.get("body") or ""),
                      flags=re.DOTALL)
        m = _INJECTION_ECHO.search(body)
        if m:
            out.append(_finding(
                "prompt_injection_echo", CRITICAL,
                f"untrusted-instruction text in prose: {m.group(0)[:60]!r}",
                sec.get("section_id")))
    title = str(article.get("title") or "")
    if _INJECTION_ECHO.search(title):
        out.append(_finding("prompt_injection_echo", CRITICAL,
                            "untrusted-instruction text in title"))
    return out


def check_temporal(article: dict[str, Any], segs: list[dict[str, Any]],
                   duration_s: float) -> list[dict[str, Any]]:
    """Invariant 3: spans sit inside the source and over real segments."""
    out: list[dict[str, Any]] = []
    if not segs:
        return [_finding("no_transcript", CRITICAL, "no transcript segments")]
    for sec in article.get("sections") or []:
        for a, b in _spans_of(sec):
            if not (0 <= a < b <= duration_s + 1):
                out.append(_finding(
                    "span_out_of_range", CRITICAL,
                    f"span {a:.0f}-{b:.0f}s outside 0-{duration_s:.0f}s",
                    sec.get("section_id")))
                continue
            if not any(float(r.get("end", -1)) >= a
                       and float(r.get("start", -1)) <= b for r in segs):
                out.append(_finding(
                    "span_without_segments", CRITICAL,
                    f"span {a:.0f}-{b:.0f}s covers no transcript",
                    sec.get("section_id")))
    return out


def check_origin(article: dict[str, Any],
                 decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 4: section evidence never overlaps excluded spans."""
    out: list[dict[str, Any]] = []
    banned: list[list[float]] = []
    for entry in decision.get("excluded_windows") or []:
        if not isinstance(entry, dict):
            continue
        try:
            banned.append([float(entry["start"]), float(entry["end"])])
        except (TypeError, ValueError, KeyError):
            continue
    if not banned:
        return out
    for sec in article.get("sections") or []:
        for a, b in _spans_of(sec):
            for c, d in banned:
                if a < d and c < b:
                    out.append(_finding(
                        "excluded_span_used", CRITICAL,
                        f"section uses excluded span {c:.0f}-{d:.0f}s",
                        sec.get("section_id")))
                    break
    return out


def check_coverage(article: dict[str, Any], index: dict[str, Any],
                   decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 5: non-excluded windows are represented or listed.

    Windows merged into neighbours are represented through overlap; only
    genuinely unrepresented windows are reported (capped: a long tail of
    repetition is one finding, not fifty).
    """
    out: list[dict[str, Any]] = []
    wins = index.get("windows") or []
    if not wins:
        return out
    banned: set[str] = set()
    for entry in decision.get("excluded_windows") or []:
        if isinstance(entry, dict) and entry.get("window_id"):
            banned.add(entry["window_id"])
    covered: list[list[float]] = []
    for sec in article.get("sections") or []:
        covered.extend(_spans_of(sec))
    missing = []
    for w in wins:
        if w.get("window_id") in banned:
            continue
        a, b = float(w["t_start"]), float(w["t_end"])
        if not any(a < d and c < b for c, d in covered):
            missing.append(w["window_id"])
    unassigned = set(((article.get("coverage") or {}).get("plan_coverage")
                      or {}).get("unassigned_substantive_window_ids") or [])
    genuinely = [m for m in missing if m not in unassigned]
    for wid in genuinely[:5]:
        out.append(_finding("unrepresented_window", MAJOR,
                            f"source window {wid} has no covering section"))
    return out


def check_reader_integrity(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Invariant 6: prose carries no internal machinery or raw markup."""
    out: list[dict[str, Any]] = []
    for sec in article.get("sections") or []:
        body = str(sec.get("body") or "")
        prose = _BALANCED_CODE.sub(" ", body)
        m = _INTERNAL_ID.search(prose)
        if m:
            out.append(_finding("internal_id_leak", CRITICAL,
                                f"internal span id {m.group(0)!r} in prose",
                                sec.get("section_id")))
        if re.search(r"\{['\"]\w+['\"]\s*:", prose):
            out.append(_finding("raw_dict_leak", CRITICAL,
                                "dictionary-shaped text in prose",
                                sec.get("section_id")))
        h = _RAW_HTML.search(prose)
        if h:
            out.append(_finding("raw_html_in_prose", CRITICAL,
                                f"model-emitted markup {h.group(0)[:40]!r}",
                                sec.get("section_id")))
        dt = _DECISION_TRACE.search(prose)
        if dt:
            out.append(_finding("decision_trace_leak", MAJOR,
                                f"internal judging vocabulary {dt.group(0)!r} "
                                f"in prose", sec.get("section_id")))
        for fence in re.findall(r"```(.*?)```", body, flags=re.DOTALL):
            if fence.count('"') % 2 or fence.count("`") % 2:
                out.append(_finding("unbalanced_syntax", CRITICAL,
                                    "fenced block with unbalanced quotes",
                                    sec.get("section_id")))
                break
    title = str(article.get("title") or "")
    if _RAW_HTML.search(title):
        out.append(_finding("raw_html_in_title", CRITICAL,
                            "model-emitted markup in title"))
    if _INTERNAL_ID.search(title):
        out.append(_finding("internal_id_leak", CRITICAL,
                            "internal span id in title"))
    return out


def _subtract_spans(spans: list[list[float]],
                    banned: list[list[float]]) -> list[list[float]]:
    """Evidence windows minus excluded (sponsor/housekeeping) spans."""
    out: list[list[float]] = []
    for a, b in spans:
        cur = [[a, b]]
        for c, d in banned:
            nxt: list[list[float]] = []
            for x, y in cur:
                if d <= x or c >= y:
                    nxt.append([x, y])
                    continue
                if c > x:
                    nxt.append([x, c])
                if d < y:
                    nxt.append([d, y])
            cur = nxt
        out.extend([w for w in cur if w[1] - w[0] > 0.5])
    return out


def _frontmatter_sections(article: dict[str, Any],
                          duration_s: float,
                          banned: list[list[float]] | None = None
                          ) -> list[dict[str, Any]]:
    """Lead, summary and headings as validatable pseudo-sections.

    Frontmatter synthesises the whole video, so its evidence is the full
    span minus excluded (sponsor/housekeeping) spans — checked with the
    same value/entity/integrity rules as body prose, but outside the
    slot, temporal and coverage contracts that govern topical sections.
    Origin still applies: a sponsor-sourced value has no business in
    the title, summary or lead.
    """
    extras: list[dict[str, Any]] = []
    span = _subtract_spans([[0.0, float(duration_s or 0) or 0.0]], banned or [])
    if not span:
        span = [[0.0, float(duration_s or 0) or 0.0]]
    lead_md = str(article.get("lead_md") or "").strip()
    if lead_md:
        extras.append({"section_id": "__lead__", "slot": "lead",
                       "heading": "Lead", "body": lead_md,
                       "t_start": 0.0, "t_end": float(duration_s or 0),
                       "source_windows": span})
    # Title, summary and headings: the reader's first impression carries
    # exact values too. Evidence is the full transcript plus the
    # video-level metadata the writer legitimately sees (title context
    # carries the duration), so "97-minute debate" does not fail here.
    front = (str(article.get("title") or "").strip() + "\n"
             + str(article.get("summary") or "").strip() + "\n"
             + "\n".join(str(s.get("heading") or "")
                         for s in article.get("sections") or [])).strip()
    if front:
        dur = float(duration_s or 0)
        meta = (f"{dur / 60:.0f} minutes" if dur >= 90 else f"{dur:.0f} seconds")
        extras.append({"section_id": "__frontmatter__", "slot": "frontmatter",
                       "heading": "Frontmatter", "body": front,
                       "t_start": 0.0, "t_end": dur,
                       "source_windows": span,
                       "meta_evidence": meta})
    return extras


def validate_typed(decision: dict[str, Any], plan: dict[str, Any],
                   index: dict[str, Any], article: dict[str, Any],
                   segs: list[dict[str, Any]],
                   duration_s: float) -> dict[str, Any]:
    """Full type-aware gate. Returns {status, findings}.

    ``refused`` when the article has no usable sections; ``needs_review``
    on any critical finding; otherwise ``ready`` with majors recorded.
    """
    sections = [s for s in (article.get("sections") or [])
                if str(s.get("body") or "").strip()
                and str(s.get("body") or "").strip() != "_No speech in this span._"]
    if not sections:
        return {"status": "refused",
                "findings": [_finding("empty_article", CRITICAL,
                                      "no usable sections in article")]}
    banned: list[list[float]] = []
    for entry in decision.get("excluded_windows") or []:
        if isinstance(entry, dict):
            try:
                banned.append([float(entry["start"]), float(entry["end"])])
            except (TypeError, ValueError, KeyError):
                continue
    extras = _frontmatter_sections(article, duration_s, banned)
    scoped = dict(article, sections=sections)
    wide = dict(article, sections=sections + extras)
    findings: list[dict[str, Any]] = []
    findings.extend(check_type_slots(decision, plan, scoped))
    findings.extend(check_executable_blocks(wide))
    findings.extend(check_procedure_order(scoped))
    findings.extend(check_no_injection_echo(wide))
    findings.extend(check_exact_values(wide, segs, duration_s))
    findings.extend(check_entities(wide, segs))
    findings.extend(check_temporal(scoped, segs, duration_s))
    findings.extend(check_origin(scoped, decision))
    findings.extend(check_coverage(scoped, index, decision))
    findings.extend(check_reader_integrity(wide))
    criticals = [f for f in findings if f["severity"] == CRITICAL]
    return {"status": "needs_review" if criticals else "ready",
            "findings": findings}
