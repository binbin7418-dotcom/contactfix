#!/usr/bin/env python3
"""ContactFix — local vCard (.vcf) duplicate merge, damage repair and
re-check tool.

Reads a vCard file in any mix of versions 2.1 / 3.0 / 4.0, repairs damaged
entries (quoted-printable / base64 encodings, mojibake charsets, missing
FN, mixed versions), finds duplicate contacts with multi-key matching
(normalised phone / email / name), merges each duplicate group losslessly
(field union, conflicts written to the merge log), leaves ambiguous pairs
(same email, different people; same name, no shared phone/email) in a
human-review list instead of merging them, then writes a BRAND-NEW .vcf
file — the original is never overwritten — plus a report whose re-check
section re-parses the output and proves: duplicate groups remaining = 0,
contact-count conservation holds, no field was lost.

100% local, standard library only, no network, no account.
"""
import argparse
import base64
import binascii
import os
import re
import sys
import time
from quopri import decodestring as qp_decode

DEFAULT_CC = "86"
OUTPUT_VERSION = "3.0"

BINARY_PROPS = {"PHOTO", "LOGO", "KEY", "SOUND"}
STRUCTURED_PROPS = {"N", "ADR", "ORG"}
MULTI_PROPS = {"TEL", "EMAIL", "ADR", "URL", "IMPP"}
UNION_TOKEN_PROPS = {"CATEGORIES", "NICKNAME"}
PROP_ORDER = [
    "VERSION", "FN", "N", "NICKNAME", "PHOTO", "BDAY", "ANNIVERSARY",
    "GENDER", "ADR", "TEL", "EMAIL", "IMPP", "URL", "ORG", "TITLE",
    "ROLE", "LOGO", "GEO", "TZ", "LANG", "CATEGORIES", "NOTE", "UID",
    "REV", "KEY", "SOUND",
]

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def has_cjk(s):
    return bool(_CJK.search(s or ""))


# --------------------------------------------------------------------------
# text decoding helpers
# --------------------------------------------------------------------------

def _norm_charset(cs):
    if not cs:
        return None
    cs = cs.strip().strip('"').lower()
    return {
        "utf8": "utf-8", "utf-8": "utf-8", "gbk": "gb18030",
        "gb2312": "gb18030", "gb18030": "gb18030", "big5": "big5",
        "iso-8859-1": "latin-1", "latin1": "latin-1",
        "windows-1252": "cp1252", "us-ascii": "ascii",
        "shift_jis": "shift_jis", "sjis": "shift_jis",
    }.get(cs, cs)


def decode_cascade(raw_bytes, charset=None):
    """Decode bytes deterministically: declared charset first, then UTF-8,
    then GB18030, then Latin-1 (which never fails). Returns (text, used,
    fell_back) where fell_back means the declared/first-choice charset did
    not work and a fallback produced the text."""
    declared = _norm_charset(charset)
    candidates = []
    for c in (declared, "utf-8", "gb18030", "latin-1"):
        if c and c not in candidates:
            candidates.append(c)
    for i, c in enumerate(candidates):
        try:
            return raw_bytes.decode(c), c, i > 0
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("latin-1", errors="replace"), "latin-1", True


def unescape_text(s):
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in ("\\", ",", ";"):
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def escape_text(s):
    return (s.replace("\\", "\\\\").replace("\r\n", "\\n")
             .replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;"))


def split_escaped(s, sep=";"):
    """Split a structured value on unescaped separators, keeping escapes."""
    parts, buf, i = [], [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            buf.append(ch)
            buf.append(s[i + 1])
            i += 2
        elif ch == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    return parts


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _split_header(line):
    """Split a logical line at the first ':' outside quotes."""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            return line[:i], line[i + 1:]
    return line, None


def _header_is_qp(header):
    h = header.upper()
    return "QUOTED-PRINTABLE" in h or "ENCODING=QP" in h


def unfold(text):
    """Physical lines -> logical lines. Standard folding (leading space or
    tab) plus vCard 2.1 quoted-printable soft breaks: a QP property whose
    line ends with '=' continues on the next physical line even without a
    leading space (the '=' is the soft-break marker and is dropped)."""
    physical = re.split(r"\r\n|\r|\n", text)
    logical = []
    for line in physical:
        if line == "" and not logical:
            continue
        if line[:1] in (" ", "\t") and logical:
            logical[-1] += line[1:]
            continue
        if logical:
            head, _ = _split_header(logical[-1])
            if _header_is_qp(head) and logical[-1].rstrip().endswith("="):
                logical[-1] = logical[-1].rstrip()[:-1] + line
                continue
        logical.append(line)
    return [l for l in logical if l.strip() != ""]


def parse_prop(line):
    """Parse one logical line -> prop dict or None if malformed."""
    header, raw = _split_header(line)
    if raw is None:
        return None
    tokens = []
    buf, in_q = [], False
    for ch in header:
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
        elif ch == ";" and not in_q:
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    tokens.append("".join(buf))
    if not tokens or not tokens[0].strip():
        return None
    name = tokens[0].strip()
    if "." in name:  # group prefix, e.g. item1.EMAIL
        name = name.split(".", 1)[1]
    name = name.upper()
    types, params = set(), {}
    for tok in tokens[1:]:
        tok = tok.strip()
        if not tok:
            continue
        if "=" in tok:
            k, v = tok.split("=", 1)
            k = k.strip().upper()
            vals = [x.strip().strip('"') for x in v.split(",") if x.strip()]
            if k == "TYPE":
                types.update(x.upper() for x in vals)
            else:
                params[k] = vals
        else:
            types.add(tok.upper())
    return {"name": name, "types": types, "params": params, "raw": raw}


def prop_encoding(prop):
    enc = prop["params"].get("ENCODING", [None])[0]
    if enc:
        return enc.upper()
    for t in prop["types"]:
        if t in ("QUOTED-PRINTABLE", "BASE64", "B", "8BIT", "7BIT"):
            return t
    return None


def decode_text_value(prop):
    """Decode a text prop's raw value -> (text, charset_used, fell_back)."""
    raw = prop["raw"]
    enc = prop_encoding(prop)
    charset = prop["params"].get("CHARSET", [None])[0]
    if enc == "QUOTED-PRINTABLE":
        b = qp_decode(raw.encode("utf-8", "surrogateescape"))
        return decode_cascade(b, charset) + (True,)
    if enc in ("BASE64", "B"):
        try:
            b = base64.b64decode(re.sub(r"\s+", "", raw))
        except (binascii.Error, ValueError):
            b = b""
        text, used, fb = decode_cascade(b, charset)
        return text, used, fb, True
    b = raw.encode("utf-8", "surrogateescape")
    if charset or "�" in raw or any(0xDC80 <= ord(c) <= 0xDCFF for c in raw):
        text, used, fb = decode_cascade(b, charset)
        return text, used, fb, False
    return raw, "utf-8", False, False


def decode_binary_value(prop):
    raw = prop["raw"]
    enc = prop_encoding(prop)
    if enc in ("BASE64", "B"):
        try:
            return base64.b64decode(re.sub(r"\s+", "", raw))
        except (binascii.Error, ValueError):
            return b""
    if enc == "QUOTED-PRINTABLE":
        return qp_decode(raw.encode("utf-8", "surrogateescape"))
    return raw.encode("utf-8", "surrogateescape")


def build_prop(line):
    """Logical line -> fully decoded prop dict (value/components) or None."""
    p = parse_prop(line)
    if p is None:
        return None
    name = p["name"]
    prop = {"name": name, "types": set(p["types"]),
            "params": {k: list(v) for k, v in p["params"].items()},
            "value": "", "components": None, "decoded": False,
            "charset_fallback": False}
    if name in BINARY_PROPS:
        prop["value"] = decode_binary_value(p)
        prop["decoded"] = prop_encoding(p) in ("BASE64", "B",
                                                "QUOTED-PRINTABLE")
        return prop
    text, _used, fb, decoded = decode_text_value(p)
    prop["charset_fallback"] = fb
    prop["decoded"] = decoded
    if name in STRUCTURED_PROPS:
        prop["components"] = [unescape_text(c) for c in split_escaped(text)]
        prop["value"] = ";".join(prop["components"])
    else:
        prop["value"] = unescape_text(text)
    # 4.0 tel: URIs carry no extra information for us; normalise to plain.
    if name == "TEL" and prop["value"].lower().startswith("tel:"):
        prop["value"] = prop["value"][4:].split(";")[0]
        prop["tel_uri_normalised"] = True
    return prop


def parse_vcf(path):
    """Parse a .vcf file -> (cards, repairs, stats).
    Card: {index, version_in, props, fn_generated}."""
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("utf-8", "surrogateescape")
    lines = unfold(text)
    cards, repairs = [], []
    cur = None

    def finish(card, closed):
        if not closed:
            repairs.append(f"card #{card['index'] + 1}: missing END:VCARD "
                           "recovered at end of file")
        card["version_in"] = card.get("version_in") or (
            "2.1" if any(prop_encoding(pr) == "QUOTED-PRINTABLE"
                         for pr in card["_raw_props"]) else "3.0")
        if not card.get("_version_seen"):
            repairs.append(f"card #{card['index'] + 1}: no VERSION line, "
                           f"assumed {card['version_in']}")
        del card["_raw_props"], card["_version_seen"]
        cards.append(card)

    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VCARD":
            cur = {"index": len(cards), "props": [], "fn_generated": False,
                   "_raw_props": [], "_version_seen": False}
            continue
        if upper == "END:VCARD":
            if cur is not None:
                finish(cur, True)
                cur = None
            continue
        if cur is None:
            continue  # junk outside cards: ignored
        raw_prop = parse_prop(line)
        if raw_prop is None:
            repairs.append(f"card #{cur['index'] + 1}: dropped malformed "
                           f"line: {line.strip()[:60]!r}")
            continue
        cur["_raw_props"].append(raw_prop)
        prop = build_prop(line)
        if prop is None:
            continue
        if prop["name"] == "VERSION":
            cur["version_in"] = prop["value"].strip()
            cur["_version_seen"] = True
            continue
        if prop["decoded"] and prop["name"] not in BINARY_PROPS:
            repairs.append(f"card #{cur['index'] + 1}: decoded "
                           f"{prop['name']} from "
                           f"{prop_encoding(raw_prop) or 'encoded'}"
                           + (" (charset fallback used)"
                              if prop["charset_fallback"] else ""))
        if prop.get("tel_uri_normalised"):
            repairs.append(f"card #{cur['index'] + 1}: TEL tel:-URI "
                           "normalised to a plain number")
        cur["props"].append(prop)
    if cur is not None:
        finish(cur, False)
    for card in cards:
        repair_card_fn(card, repairs)
    stats = {"versions": {}, "input_bytes": len(data)}
    for c in cards:
        stats["versions"][c["version_in"]] = \
            stats["versions"].get(c["version_in"], 0) + 1
    return cards, repairs, stats


# --------------------------------------------------------------------------
# card accessors + repair of missing FN / N
# --------------------------------------------------------------------------

def get_props(card, name):
    return [p for p in card["props"] if p["name"] == name]


def get_prop(card, name):
    ps = get_props(card, name)
    return ps[0] if ps else None


def fn_of(card):
    p = get_prop(card, "FN")
    return p["value"].strip() if p else ""


def n_components(card):
    p = get_prop(card, "N")
    if p and p["components"]:
        comps = list(p["components"]) + [""] * 5
        return comps[:5]
    return ["", "", "", "", ""]


def fn_from_n(comps):
    family, given = comps[0].strip(), comps[1].strip()
    extra = comps[2].strip()
    if not (family or given or extra):
        return ""
    if has_cjk(family + given + extra):
        return family + given + extra
    return " ".join(x for x in (given, extra, family) if x)


def repair_card_fn(card, repairs):
    """Guarantee an FN: derive it from N, else from email/phone."""
    if fn_of(card):
        return
    comps = n_components(card)
    fn = fn_from_n(comps)
    source = "N"
    if not fn:
        em = get_prop(card, "EMAIL")
        if em and em["value"].strip():
            local = em["value"].split("@")[0]
            fn = re.sub(r"[._\-]+", " ", local).strip().title()
            source = "email"
    if not fn:
        tel = get_prop(card, "TEL")
        if tel and tel["value"].strip():
            fn = tel["value"].strip()
            source = "phone number"
    if not fn:
        fn = f"Unknown Contact {card['index'] + 1}"
        source = "placeholder"
    card["props"].insert(0, {"name": "FN", "types": set(), "params": {},
                             "value": fn, "components": None,
                             "decoded": False, "charset_fallback": False})
    card["fn_generated"] = True
    repairs.append(f"card #{card['index'] + 1}: missing FN generated "
                   f"from {source}: {fn!r}")


def synthesise_n(fn):
    """Best-effort N components from an FN string (for 3.0 output)."""
    fn = fn.strip()
    if not fn:
        return ["", "", "", "", ""]
    if has_cjk(fn):
        return [fn[0], fn[1:], "", "", ""]
    parts = fn.split()
    if len(parts) == 1:
        return [parts[0], "", "", "", ""]
    return [parts[-1], " ".join(parts[:-1]), "", "", ""]


# --------------------------------------------------------------------------
# matching keys
# --------------------------------------------------------------------------

def phone_key(value, cc=DEFAULT_CC):
    """Normalise a phone number for comparison: punctuation removed,
    international prefix unified to +<country code>, national trunk 0 and
    bare mobile forms mapped onto the default country code."""
    v = (value or "").strip()
    if v.lower().startswith("tel:"):
        v = v[4:]
    v = v.split(";")[0]
    digits = re.sub(r"\D", "", v)
    if not digits:
        return ""
    if v.lstrip().startswith("+"):
        return "+" + digits
    if v.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith(cc) and len(digits) >= len(cc) + 10:
        return "+" + digits
    if digits.startswith("0") and len(digits) >= 10:
        return "+" + cc + digits[1:]
    if cc == "86" and len(digits) == 11 and digits.startswith("1"):
        return "+86" + digits
    return digits


def email_key(value):
    return (value or "").strip().lower()


def name_key(value):
    return re.sub(r"[\s\-_.,'’\"()]+", "", (value or "").lower())


def card_name_keys(card):
    keys = set()
    fn = fn_of(card)
    if fn:
        keys.add(name_key(fn))
    comps = n_components(card)
    nk = name_key(comps[0] + comps[1])
    if nk:
        keys.add(nk)
    nk2 = name_key(comps[1] + comps[0])
    if nk2:
        keys.add(nk2)
    return {k for k in keys if k}


def card_phone_keys(card, cc=DEFAULT_CC):
    return {phone_key(p["value"], cc) for p in get_props(card, "TEL")
            if phone_key(p["value"], cc)}


def card_email_keys(card):
    return {email_key(p["value"]) for p in get_props(card, "EMAIL")
            if email_key(p["value"])}


# --------------------------------------------------------------------------
# duplicate grouping (union-find) + human-review pairs
# --------------------------------------------------------------------------

class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def group_edges(cards, cc=DEFAULT_CC):
    """Merge edges: shared phone key, or shared email + shared name key.
    Returns {frozenset({i, j}): set(reasons)}."""
    edges = {}

    def add(i, j, reason):
        if i != j:
            edges.setdefault(frozenset((i, j)), set()).add(reason)

    by_phone, by_email = {}, {}
    for c in cards:
        for k in card_phone_keys(c, cc):
            by_phone.setdefault(k, []).append(c["index"])
        for k in card_email_keys(c):
            by_email.setdefault(k, []).append(c["index"])
    for k, idxs in by_phone.items():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                add(idxs[a], idxs[b], f"same phone {k}")
    name_map = {c["index"]: card_name_keys(c) for c in cards}
    for k, idxs in by_email.items():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                if name_map[idxs[a]] & name_map[idxs[b]]:
                    add(idxs[a], idxs[b], f"same email {k} + same name")
    return edges


def find_groups(cards, cc=DEFAULT_CC):
    """Duplicate groups (size >= 2) with the match reasons per member."""
    edges = group_edges(cards, cc)
    dsu = DSU(len(cards))
    for pair in edges:
        i, j = tuple(pair)
        dsu.union(i, j)
    groups = {}
    for c in cards:
        groups.setdefault(dsu.find(c["index"]), []).append(c["index"])
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        reasons = set()
        for pair, rs in edges.items():
            if set(pair) <= set(members):
                reasons.update(rs)
        out.append({"members": sorted(members), "reasons": sorted(reasons)})
    out.sort(key=lambda g: g["members"][0])
    return out


def find_review_pairs(cards, groups, cc=DEFAULT_CC):
    """Ambiguous pairs that must NOT be auto-merged:
    (a) same email but different names — possibly two people sharing one
        mailbox; (b) same name but no shared phone or email — possibly the
        same person, but not provable. One entry per entity pair."""
    grouped = {}
    for gi, g in enumerate(groups):
        for m in g["members"]:
            grouped[m] = gi

    def entity(i):
        return grouped.get(i, ("single", i))

    name_map = {c["index"]: card_name_keys(c) for c in cards}
    # The weak "same name" review signal uses the display name (FN) only:
    # N-component permutations collide across different people who merely
    # share a family/given name, which would flood the review list.
    fn_key = {c["index"]: name_key(fn_of(c)) for c in cards}
    phone_map = {c["index"]: card_phone_keys(c, cc) for c in cards}
    email_map = {c["index"]: card_email_keys(c) for c in cards}
    seen, out = set(), []
    n = len(cards)
    for i in range(n):
        for j in range(i + 1, n):
            if entity(i) == entity(j):
                continue
            pair_key = (entity(i), entity(j))
            shared_email = email_map[i] & email_map[j]
            shared_name = name_map[i] & name_map[j]
            shared_phone = phone_map[i] & phone_map[j]
            reason = None
            if shared_email and not shared_name:
                reason = ("same email " + sorted(shared_email)[0] +
                          ", different names — not merged, your call")
            elif fn_key[i] and fn_key[i] == fn_key[j] \
                    and not shared_email and not shared_phone:
                reason = ("same name, no shared phone or email — "
                          "possibly the same person, not provable")
            if reason and pair_key not in seen:
                seen.add(pair_key)
                out.append({"a": i, "b": j, "reason": reason,
                            "a_fn": fn_of(cards[i]), "b_fn": fn_of(cards[j])})
    return out


# --------------------------------------------------------------------------
# lossless merge
# --------------------------------------------------------------------------

def _dedupe_key(prop, cc):
    name = prop["name"]
    if name == "TEL":
        return phone_key(prop["value"], cc)
    if name == "EMAIL":
        return email_key(prop["value"])
    if name == "ADR":
        return "|".join(re.sub(r"\s+", " ", c.strip().lower())
                        for c in (prop["components"] or []))
    if name == "URL":
        return prop["value"].strip().lower().rstrip("/")
    return re.sub(r"\s+", " ", prop["value"].strip().lower()) \
        if isinstance(prop["value"], str) else prop["name"]


def _new_prop(name, value, types=None, components=None):
    return {"name": name, "types": set(types or set()), "params": {},
            "value": value, "components": components, "decoded": False,
            "charset_fallback": False}


def merge_group(cards, group, cc=DEFAULT_CC):
    """Merge one duplicate group. Returns (merged_card, log_lines).
    Every distinct field survives: in the merged card, or — for losing
    values of single-value fields — verbatim in the merge log."""
    members = [cards[i] for i in group["members"]]
    # Representative order: cards with a real (not generated) FN first,
    # then most properties, then original order.
    order = sorted(members, key=lambda c: (c["fn_generated"],
                                           -len(c["props"]), c["index"]))
    log = [f"Group: {len(members)} cards merged "
           f"({', '.join(fn_of(c) or '#' + str(c['index'] + 1) for c in members)}); "
           f"matched on: {'; '.join(group['reasons'])}"]
    merged = {"index": order[0]["index"], "props": [],
              "fn_generated": False, "version_in": OUTPUT_VERSION}

    # --- FN: from the highest-ranked member
    fn_src = order[0]
    merged["props"].append(_new_prop("FN", fn_of(fn_src)))
    distinct_fns = sorted({fn_of(c) for c in members if fn_of(c)})
    if len(distinct_fns) > 1:
        log.append(f"  FN kept: {fn_of(fn_src)!r}; other forms in log: "
                   + ", ".join(repr(f) for f in distinct_fns
                               if f != fn_of(fn_src)))
    merged["fn_generated"] = fn_src["fn_generated"]

    # --- N: most complete component set wins
    n_ranked = sorted(members,
                      key=lambda c: -sum(1 for x in n_components(c) if x))
    n_best = n_components(n_ranked[0])
    distinct_n = {tuple(n_components(c)) for c in members
                  if any(n_components(c))}
    if any(n_best):
        merged["props"].append(_new_prop("N", ";".join(n_best),
                                         components=n_best))
    if len(distinct_n) > 1:
        log.append(f"  N kept: {';'.join(n_best)!r}; other forms in log: "
                   + ", ".join(repr(";".join(t)) for t in sorted(distinct_n)
                               if t != tuple(n_best)))

    handled = {"FN", "N", "VERSION"}
    # --- union-token props (CATEGORIES / NICKNAME)
    for name in UNION_TOKEN_PROPS:
        tokens, seen = [], set()
        for c in order:
            for p in get_props(c, name):
                for tok in re.split(r"[,;]", p["value"]):
                    tok = tok.strip()
                    if tok and tok.lower() not in seen:
                        seen.add(tok.lower())
                        tokens.append(tok)
        if tokens:
            merged["props"].append(_new_prop(name, ",".join(tokens)))
            log.append(f"  {name}: union of {len(tokens)} value(s)")
        handled.add(name)

    # --- multi-value props: union by normalised key
    multi_names = []
    for c in order:
        for p in c["props"]:
            if p["name"] not in handled and p["name"] not in multi_names \
                    and p["name"] in MULTI_PROPS:
                multi_names.append(p["name"])
    for name in multi_names:
        by_key, variants = {}, 0
        for c in order:
            for p in get_props(c, name):
                k = _dedupe_key(p, cc)
                if not k:
                    k = f"raw:{p['value']}"
                if k in by_key:
                    kept = by_key[k]
                    kept["types"] = kept["types"] | p["types"]
                    if kept["value"] != p["value"]:
                        variants += 1
                else:
                    by_key[k] = _new_prop(
                        name, p["value"], p["types"],
                        list(p["components"]) if p["components"] else None)
        for p in by_key.values():
            merged["props"].append(p)
        log.append(f"  {name}: {len(by_key)} unique value(s) kept"
                   + (f", {variants} format variant(s) unified"
                      if variants else ""))
        handled.add(name)

    # --- binary props: first bytes win, other distinct copies logged
    for name in sorted(BINARY_PROPS):
        vals = []
        for c in order:
            for p in get_props(c, name):
                if p["value"]:
                    vals.append(p)
        if not vals:
            continue
        merged["props"].append(_new_prop(name, vals[0]["value"],
                                         vals[0]["types"]))
        distinct = {bytes(v["value"]) for v in vals}
        if len(distinct) > 1:
            log.append(f"  {name}: {len(distinct)} different values found; "
                       "first kept, others noted here (bytes not copied "
                       "into the card twice)")
        handled.add(name)

    # --- remaining single-value props: first non-empty wins, NOTE unions
    rest = []
    for c in order:
        for p in c["props"]:
            if p["name"] not in handled and p["name"] not in rest:
                rest.append(p["name"])
    for name in rest:
        vals = []
        for c in order:
            for p in get_props(c, name):
                v = p["value"]
                if isinstance(v, str) and v.strip():
                    vals.append((c, p))
        if not vals:
            continue
        if name == "NOTE":
            notes, seen = [], set()
            for _c, p in vals:
                if p["value"].strip().lower() not in seen:
                    seen.add(p["value"].strip().lower())
                    notes.append(p["value"].strip())
            merged["props"].append(_new_prop("NOTE", " | ".join(notes)))
            log.append(f"  NOTE: {len(notes)} note(s) combined")
        else:
            kept_c, kept_p = vals[0]
            merged["props"].append(_new_prop(name, kept_p["value"],
                                             kept_p["types"],
                                             kept_p["components"]))
            distinct = sorted({p["value"].strip() for _c, p in vals})
            if len(distinct) > 1:
                log.append(f"  {name} kept: {kept_p['value']!r} "
                           f"(from card #{kept_c['index'] + 1}); other "
                           "value(s) in log: "
                           + ", ".join(repr(v) for v in distinct
                                       if v != kept_p["value"].strip()))
            else:
                log.append(f"  {name}: kept {kept_p['value']!r} "
                           f"(from card #{kept_c['index'] + 1})")
        handled.add(name)
    return merged, log


def merge_all(cards, groups, cc=DEFAULT_CC):
    """Returns (output_cards, merge_log_lines)."""
    in_group = {}
    for gi, g in enumerate(groups):
        for m in g["members"]:
            in_group[m] = gi
    out, log = [], []
    done = set()
    for c in cards:
        gi = in_group.get(c["index"])
        if gi is None:
            out.append(c)
        elif gi not in done:
            done.add(gi)
            merged, lines = merge_group(cards, groups[gi], cc)
            out.append(merged)
            log.extend(lines)
    return out, log


# --------------------------------------------------------------------------
# serialisation (vCard 3.0, CRLF, 75-octet folding)
# --------------------------------------------------------------------------

def fold_line(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts, cur = [], b
    first = True
    while cur:
        limit = 75 if first else 74
        if len(cur) <= limit:
            parts.append(cur)
            break
        cut = limit
        while cut > 0 and 0x80 <= cur[cut] <= 0xBF:
            cut -= 1
        parts.append(cur[:cut])
        cur = cur[cut:]
        first = False
    out = parts[0].decode("utf-8")
    for p in parts[1:]:
        out += "\r\n " + p.decode("utf-8")
    return out


def render_prop(prop):
    name = prop["name"]
    head = name
    types = sorted(t for t in prop["types"]
                   if t not in ("QUOTED-PRINTABLE", "BASE64", "8BIT",
                                "7BIT", "B"))
    if name in BINARY_PROPS:
        head += ";ENCODING=b"
        if types:
            head += ";TYPE=" + ",".join(types)
        body = base64.b64encode(prop["value"] or b"").decode("ascii")
        return head + ":" + body
    if types:
        head += ";TYPE=" + ",".join(types)
    if prop["components"] is not None:
        body = ";".join(escape_text(c) for c in prop["components"])
    else:
        body = escape_text(prop["value"] or "")
    return head + ":" + body


def render_card(card):
    props = list(card["props"])
    # guarantee an N for 3.0 consumers; synthesis is reported by caller
    if not get_prop(card, "N"):
        comps = synthesise_n(fn_of(card))
        props.append(_new_prop("N", ";".join(comps), components=comps))
        card["_n_synthesised"] = True
    order = {name: i for i, name in enumerate(PROP_ORDER)}
    props.sort(key=lambda p: (order.get(p["name"], 99), p["name"]))
    lines = ["BEGIN:VCARD", f"VERSION:{OUTPUT_VERSION}"]
    for p in props:
        if p["name"] == "VERSION":
            continue
        lines.append(fold_line(render_prop(p)))
    lines.append("END:VCARD")
    return "\r\n".join(lines)


def render_file(cards):
    ordered = sorted(cards, key=lambda c: (name_key(fn_of(c)),
                                           min(card_phone_keys(c)) or ""))
    return "\r\n".join(render_card(c) for c in ordered) + "\r\n"


# --------------------------------------------------------------------------
# re-check / verification
# --------------------------------------------------------------------------

def verify(input_cards, groups, output_cards, merge_log, cc=DEFAULT_CC,
           output_path=None):
    """Re-check the merged result. Re-parses the written file when a path
    is given, so the check covers exactly what the customer receives."""
    v = {}
    expected = len(input_cards) - sum(len(g["members"]) - 1 for g in groups)
    v["expected_output"] = expected
    v["actual_output"] = len(output_cards)
    v["conservation_ok"] = expected == len(output_cards)

    check_cards = output_cards
    if output_path:
        reparsed, _rep, _st = parse_vcf(output_path)
        check_cards = reparsed
        v["reparsed_count"] = len(reparsed)
    v["remaining_groups"] = len(find_groups(check_cards, cc))
    v["cards_missing_fn"] = sum(1 for c in check_cards if not fn_of(c))

    # field-union: every distinct multi-value key and every single-value
    # field of every group member must survive in the merged card or be
    # quoted verbatim in the merge log.
    log_text = "\n".join(merge_log)
    union_failures = []
    merged_by_index = {c["index"]: c for c in output_cards}
    for g in groups:
        merged = merged_by_index.get(g["members"][0])
        if merged is None:
            # merged card carries the representative's original index
            merged = next((c for c in output_cards
                           if c["index"] in g["members"]), None)
        if merged is None:
            union_failures.append(f"group {g['members']}: no merged card")
            continue
        for mi in g["members"]:
            member = input_cards[mi]
            for p in member["props"]:
                name = p["name"]
                if name in ("VERSION",):
                    continue
                if name in MULTI_PROPS:
                    k = _dedupe_key(p, cc)
                    found = any(_dedupe_key(q, cc) == k
                                for q in get_props(merged, name))
                    if not found:
                        union_failures.append(
                            f"card #{mi + 1} {name} key {k!r} lost")
                elif name in BINARY_PROPS:
                    if not get_props(merged, name):
                        union_failures.append(
                            f"card #{mi + 1} {name} lost")
                elif name in UNION_TOKEN_PROPS:
                    for tok in re.split(r"[,;]", p["value"]):
                        tok = tok.strip()
                        if tok and tok not in (get_prop(merged, name)
                                               or {}).get("value", ""):
                            union_failures.append(
                                f"card #{mi + 1} {name} token {tok!r} lost")
                elif name in ("FN", "N"):
                    continue  # canonical forms asserted via FN/N checks
                else:
                    kept = get_prop(merged, name)
                    val = p["value"].strip() if isinstance(
                        p["value"], str) else ""
                    if not val:
                        continue
                    if kept and kept["value"].strip() == val:
                        continue
                    if name == "NOTE" and kept and val in kept["value"]:
                        continue
                    if repr(val) in log_text or val in log_text:
                        continue
                    union_failures.append(
                        f"card #{mi + 1} {name} value {val!r} lost")
    v["union_failures"] = union_failures
    v["union_ok"] = not union_failures
    v["new_errors"] = v["cards_missing_fn"] + len(union_failures) + \
        (0 if v["conservation_ok"] else 1)
    v["ok"] = (v["conservation_ok"] and v["remaining_groups"] == 0
               and v["cards_missing_fn"] == 0 and v["union_ok"])
    return v


# --------------------------------------------------------------------------
# report + top-level runs
# --------------------------------------------------------------------------

def build_report(src_name, cards, repairs, stats, groups, review, mode,
                 verify_result=None, merge_log=None, out_name=None,
                 elapsed=0.0):
    L = []
    L.append(f"# ContactFix Report — {src_name}")
    L.append("")
    vers = ", ".join(f"{k}×{v}" for k, v in sorted(stats["versions"].items()))
    L.append(f"- Input: {len(cards)} contacts ({vers})")
    L.append(f"- Mode: {mode}")
    L.append(f"- Repairs applied: {len(repairs)}")
    for r in repairs[:200]:
        L.append(f"  - {r}")
    if len(repairs) > 200:
        L.append(f"  - … and {len(repairs) - 200} more")
    L.append(f"- Duplicate groups merged: {len(groups)}")
    if merge_log:
        for line in merge_log:
            L.append(f"  - {line}" if not line.startswith("  ")
                     else f"  {line}")
    L.append(f"- Needs your review (NOT merged): {len(review)} pair(s)")
    for r in review:
        L.append(f"  - {r['a_fn'] or 'card #' + str(r['a'] + 1)} <-> "
                 f"{r['b_fn'] or 'card #' + str(r['b'] + 1)}: {r['reason']}")
    if verify_result is not None:
        v = verify_result
        L.append("")
        L.append("## Re-check (output re-parsed and re-scanned)")
        L.append(f"- Output contacts: {v['actual_output']} "
                 f"(expected {v['expected_output']}) — conservation: "
                 f"{'OK' if v['conservation_ok'] else 'FAILED'}")
        L.append(f"- Duplicate groups remaining: {v['remaining_groups']}")
        L.append(f"- Output cards missing FN: {v['cards_missing_fn']}")
        L.append(f"- Field-union check: "
                 f"{'OK — nothing lost' if v['union_ok'] else 'FAILED'}")
        for f_ in v["union_failures"][:50]:
            L.append(f"  - LOST: {f_}")
        L.append(f"- New errors introduced: {v['new_errors']}")
    if out_name:
        L.append(f"- Cleaned file written (new file, original untouched): "
                 f"{out_name}")
    L.append(f"- Elapsed: {elapsed:.2f}s — 100% local, nothing uploaded.")
    return "\n".join(L) + "\n"


def run_scan(path, cc=DEFAULT_CC):
    t0 = time.time()
    cards, repairs, stats = parse_vcf(path)
    groups = find_groups(cards, cc)
    review = find_review_pairs(cards, groups, cc)
    redundant = sum(len(g["members"]) - 1 for g in groups)
    report = build_report(os.path.basename(path), cards, repairs, stats,
                          groups, review, "scan only (nothing written)",
                          elapsed=time.time() - t0)
    return {"cards": cards, "repairs": repairs, "stats": stats,
            "groups": groups, "review": review,
            "redundant_copies": redundant, "report": report,
            "elapsed": time.time() - t0}


def run_fix(path, outdir, cc=DEFAULT_CC):
    t0 = time.time()
    stem = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, f"{stem}.cleaned.vcf")
    if os.path.abspath(out_path) == os.path.abspath(path):
        raise ValueError("output path would overwrite the original — "
                         "choose a different output folder")
    cards, repairs, stats = parse_vcf(path)
    groups = find_groups(cards, cc)
    review = find_review_pairs(cards, groups, cc)
    output_cards, merge_log = merge_all(cards, groups, cc)
    text = render_file(output_cards)
    with open(out_path, "wb") as f:
        f.write(text.encode("utf-8"))
    n_synth = sum(1 for c in output_cards if c.pop("_n_synthesised", False))
    if n_synth:
        repairs = repairs + [f"{n_synth} output card(s): N synthesised "
                             "from FN for vCard 3.0 consumers"]
    v = verify(cards, groups, output_cards, merge_log, cc,
               output_path=out_path)
    report = build_report(os.path.basename(path), cards, repairs, stats,
                          groups, review, "fix", verify_result=v,
                          merge_log=merge_log,
                          out_name=os.path.basename(out_path),
                          elapsed=time.time() - t0)
    report_path = os.path.join(outdir, f"{stem}.contactfix-report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    return {"cards": cards, "repairs": repairs, "stats": stats,
            "groups": groups, "review": review, "verify": v,
            "output_cards": output_cards, "output_path": out_path,
            "report_path": report_path, "report": report,
            "elapsed": time.time() - t0}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ContactFix — merge duplicate vCard contacts, repair "
                    "damaged entries, re-check the result. Local only.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("scan", "fix"):
        sp = sub.add_parser(cmd)
        sp.add_argument("input")
        sp.add_argument("--out", default=None,
                        help="output folder (fix mode)")
        sp.add_argument("--cc", default=DEFAULT_CC,
                        help="default country code for national-format "
                             "numbers (default 86)")
    args = ap.parse_args(argv)
    if args.cmd == "scan":
        s = run_scan(args.input, args.cc)
        print(s["report"])
        print(f"Summary: {len(s['cards'])} contacts · "
              f"{len(s['groups'])} duplicate groups · "
              f"{s['redundant_copies']} redundant copies · "
              f"{len(s['review'])} pair(s) need your review")
    else:
        outdir = args.out or os.path.join(
            os.path.dirname(os.path.abspath(args.input)),
            "contactfix_output")
        s = run_fix(args.input, outdir, args.cc)
        print(s["report"])
        v = s["verify"]
        print(f"Summary: {len(s['cards'])} in → {v['actual_output']} out · "
              f"{len(s['groups'])} groups merged · re-check "
              f"{'OK' if v['ok'] else 'FAILED'} · cleaned file: "
              f"{s['output_path']} · report: {s['report_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
