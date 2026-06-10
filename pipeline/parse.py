"""Parse CMTEB functionare_sistem_termoficare.php HTML into outage records.

Era-tolerant per the design spec: tables are selected by fuzzy header
signature, never by position or class name. Unknown layouts raise
ParseFailure so snapshots quarantine loudly instead of dropping silently.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from lxml import html as lxml_html

# Folded substrings that must each match one of the five header cells.
HEADER_TOKENS = ("sector", "zone", "agent", "cauza", "data")

PT_SPLIT_RE = re.compile(r"punct\s+termic\s*:", re.I)
PT_NAME_RE = re.compile(r"^\s*(.*?)\s*(?:--?|–|—)\s*(\d+)\s*(blocuri/imobile|blocuri|imobile)", re.S)
BULLET_RE = re.compile(r"[•]|&bull;")
SEVERITY_RE = re.compile(r"\b(oprire|deficient[ae]?)\b", re.I)
SERVICE_RE = re.compile(r"\b(acc|inc)\b", re.I)
REMEDIERE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})")
STREET_TYPE_RE = re.compile(
    r"^(str|strada|bld|b-?dul|bulevardul|cal|calea|sos|soseaua|intr|intrarea|al|aleea|"
    r"drm|drumul|pta|piata|spl|splaiul|prel|prelungirea)\b\.?\s*",
    re.I,
)

AVARIE_KEYWORDS = ("avari", "remedier", "spartur", "fisur", "neetans", "defect",
                   "pierder", "lipsa tensiune")
PROGRAMAT_KEYWORDS = (
    "moderniz", "reabilit", "revizi", "izolare", "lucrar", "montare",
    "inlocuir", "retehnologiz", "racordare", "probe", "extindere",
    "mentenan", "curatat", "curatare", "spalare",
)


class ParseFailure(Exception):
    """Page layout did not match any known era. Quarantine the snapshot."""


class EmptyState(Exception):
    """Upstream rendered the 'Nu exista inregistrari' banner (usually a transient
    upstream glitch, occasionally a real zero). Skipped for episode continuity,
    but recorded distinctly from real parse failures."""


def fold(text: str) -> str:
    """Diacritic-fold + lowercase + collapse whitespace. The join key everything rides on."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class Record:
    sector: int | None
    pt_raw: str
    pt_norm: str
    pt_type: str | None
    is_partial: bool
    blocks_count: int | None
    severity: str           # 'oprire' | 'deficienta'
    service: str            # 'ACC' | 'INC'
    cause_raw: str
    cause_class: str        # 'avarie' | 'programat' | 'unclassified'
    remediere_raw: str
    streets: list[tuple[str, str, str]] = field(default_factory=list)  # (norm, type, blocks_raw)

    def key_tuple(self) -> tuple:
        return (self.sector, self.pt_norm, self.severity, self.service,
                self.cause_class, self.cause_raw, self.remediere_raw, self.blocks_count)


def normalize_pt(raw: str) -> tuple[str, str | None, bool]:
    """Return (pt_norm, pt_type, is_partial). Modifiers are flags, not identity."""
    name = fold(raw)
    is_partial = False
    m = re.search(r"[\s(\-]*\bpartial\b\)?\s*$", name)
    if m:
        is_partial = True
        name = name[: m.start()].rstrip(" -(")
    pt_type = None
    m = re.match(r"^(pt|ct|sc|mt|cs)\s+(?=\S)", name)
    if m:
        pt_type = m.group(1)
        name = name[m.end():]
    return name.strip(), pt_type, is_partial


def classify_cause(cause_raw: str) -> str:
    folded = fold(cause_raw)
    if any(k in folded for k in AVARIE_KEYWORDS):
        return "avarie"
    if any(k in folded for k in PROGRAMAT_KEYWORDS):
        return "programat"
    return "unclassified"


def decompose_agent(raw: str) -> list[tuple[str, str]]:
    """'Oprire ACC/INC' -> [('oprire','ACC'), ('oprire','INC')]."""
    sev_m = SEVERITY_RE.search(fold(raw))
    if not sev_m:
        raise ParseFailure(f"unknown agent value: {raw!r}")
    severity = "oprire" if sev_m.group(1).startswith("oprire") else "deficienta"
    services = [s.upper() for s in SERVICE_RE.findall(raw)]
    if not services:
        return [(severity, "UNKNOWN")]
    return [(severity, svc) for svc in dict.fromkeys(services)]


def parse_street_line(line: str) -> tuple[str, str, str] | None:
    line = line.strip().strip("-").strip()
    if not line:
        return None
    if " - " in line:
        street_raw, blocks_raw = line.split(" - ", 1)
    else:
        street_raw, blocks_raw = line, ""
    street_raw = street_raw.strip()
    m = STREET_TYPE_RE.match(street_raw)
    stype = fold(m.group(1)) if m else ""
    name = street_raw[m.end():] if m else street_raw
    norm = fold(name)
    norm = norm.lstrip("/- .")
    if not norm or not re.search(r"[a-z]{2}", norm) or norm in (
        "imobil", "imobile", "bl", "blocuri", "nr", "blocuri/imobile",
    ):
        return None  # guard against '/imobile'-class list-suffix junk
    return norm, stype, blocks_raw.strip()


def _header_signature_ok(table) -> bool:
    first_row = table.xpath(".//tr[1]")
    if not first_row:
        return False
    cells = first_row[0].xpath("./th|./td")
    if len(cells) != len(HEADER_TOKENS):
        return False
    folded = [fold(c.text_content()) for c in cells]
    return all(tok in cell for tok, cell in zip(HEADER_TOKENS, folded))


def _explode_zone_cell(cell) -> list[tuple[str, int | None, list]]:
    """Split a 'Zone afectate' cell into per-PT (name_raw, blocks_count, street_lines)."""
    # Render the cell with <br> as newlines so bullets stay line-structured.
    raw_html = lxml_html.tostring(cell, encoding="unicode")
    raw_html = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
    text = lxml_html.fromstring(raw_html).text_content()
    chunks = PT_SPLIT_RE.split(text)
    out = []
    for chunk in chunks[1:]:  # chunk 0 is pre-"Punct termic:" noise
        m = PT_NAME_RE.match(chunk)
        if m:
            name_raw = m.group(1).strip()
            blocks_count = int(m.group(2))
            rest = chunk[m.end():]
        else:
            # No "-- N blocuri" suffix: name is the first line.
            lines = chunk.strip().split("\n", 1)
            name_raw = lines[0].strip().strip("-").strip()
            blocks_count = None
            rest = lines[1] if len(lines) > 1 else ""
        street_lines = [s for part in rest.split("\n") for s in BULLET_RE.split(part) if s.strip()]
        if name_raw:
            out.append((name_raw, blocks_count, street_lines))
    return out


def parse_page(html_bytes: bytes) -> list[Record]:
    doc = lxml_html.fromstring(html_bytes)

    candidates = [t for t in doc.xpath("//table") if _header_signature_ok(t)]
    if not candidates:
        if "nu exista inregistrari" in fold(doc.text_content()):
            raise EmptyState("upstream returned the empty-records banner")
        raise ParseFailure("no table matched the 5-header signature")

    st_tables = [t for t in candidates
                 if t.xpath("ancestor::div[@id='ST']") or t.xpath("ancestor::div[contains(@id,'ST')]")]
    tables = st_tables[:1] if st_tables else candidates

    records: list[Record] = []
    seen: set[tuple] = set()
    for table in tables:
        for row in table.xpath(".//tr[position()>1]"):
            tds = row.xpath("./td")
            if len(tds) < 5:
                continue
            sector_txt = tds[0].text_content().strip()
            sector = int(sector_txt) if sector_txt.isdigit() else None
            agent_raw = tds[2].text_content().strip()
            cause_raw = re.sub(r"\s+", " ", tds[3].text_content()).strip()
            remediere_raw = tds[4].text_content().strip()
            if not agent_raw and not tds[1].text_content().strip():
                continue
            cause_class = classify_cause(cause_raw)
            for name_raw, blocks_count, street_lines in _explode_zone_cell(tds[1]):
                pt_norm, pt_type, is_partial = normalize_pt(name_raw)
                streets = [s for s in (parse_street_line(l) for l in street_lines) if s]
                for severity, service in decompose_agent(agent_raw):
                    rec = Record(sector, name_raw, pt_norm, pt_type, is_partial,
                                 blocks_count, severity, service, cause_raw,
                                 cause_class, remediere_raw, streets)
                    k = rec.key_tuple()
                    if k not in seen:   # ST + S1..S6 duplicates collapse here
                        seen.add(k)
                        records.append(rec)
    return records


def content_hash(records: list[Record]) -> str:
    canon = sorted(r.key_tuple() for r in records)
    return hashlib.sha256(repr(canon).encode()).hexdigest()


def parse_remediere(raw: str):
    m = REMEDIERE_RE.search(raw)
    if not m:
        return None
    d, mo, y, h, mi = (int(g) for g in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:00"


def harta_points(html_text: str) -> list[tuple[str, float, float]]:
    """(denumire, lat, lon) for every PT on the map page, JSON-decoded.

    Parses the inline passedFeatures_* arrays with a real JSON parser; regexing
    raw page text leaves \\uXXXX and \\/ escapes in names (a fifth of all PTs)
    and silently breaks every downstream join.
    """
    import json as _json

    points = []
    for color in ("verde", "galben", "rosu"):
        m = re.search(rf"var passedFeatures_{color}\s*=\s*(\[.*?\]);", html_text, re.S)
        if not m:
            continue
        for p in _json.loads(m.group(1)):
            name = (p.get("denumire") or "").strip()
            if name:
                points.append((name, p.get("latitudine"), p.get("longitudine")))
    return points
