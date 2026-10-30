"""Minimal Java .class file parser (constant pool + method references)."""

import struct
from dataclasses import dataclass, field

MAJOR_TO_JAVA = {45: "1.1", 46: "1.2", 47: "1.3", 48: "1.4", 49: "5", 50: "6", 51: "7", 52: "8", 53: "9", 54: "10", 55: "11", 56: "12", 57: "13", 58: "14", 59: "15", 60: "16", 61: "17", 62: "18", 63: "19", 64: "20", 65: "21", 66: "22", 67: "23", 68: "24", 69: "25", 70: "26"}


def major_to_java(major):
    if major in MAJOR_TO_JAVA:
        return MAJOR_TO_JAVA[major]
    if major > 70:
        return f"{major - 44}"
    return f"unknown({major})"


@dataclass
class ClassInfo:
    name: str = ""
    super_name: str = ""
    major: int = 0
    interfaces: list = field(default_factory=list)
    class_refs: set = field(default_factory=set)
    member_refs: set = field(default_factory=set)
    source_file: str = ""


class ClassFormatError(Exception):
    pass


def _read_u2(data, pos):
    return struct.unpack_from(">H", data, pos)[0], pos + 2


def _read_u4(data, pos):
    return struct.unpack_from(">I", data, pos)[0], pos + 4


def parse_class(data: bytes) -> ClassInfo:
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        raise ClassFormatError("not a class file")
    pos = 4
    _minor, pos = _read_u2(data, pos)
    major, pos = _read_u2(data, pos)
    count, pos = _read_u2(data, pos)
    utf8, class_idx, ref_idx, nat_idx = {}, {}, {}, {}
    i = 1
    while i < count:
        tag = data[pos]; pos += 1
        if tag == 1:
            length, pos = _read_u2(data, pos); raw = data[pos:pos + length]; pos += length
            try: utf8[i] = raw.decode("utf-8")
            except UnicodeDecodeError: utf8[i] = raw.decode("latin-1")
        elif tag == 7: class_idx[i], pos = _read_u2(data, pos)
        elif tag in (9, 10, 11):
            c, pos = _read_u2(data, pos); nt, pos = _read_u2(data, pos); ref_idx[i] = (c, nt)
        elif tag == 12:
            n, pos = _read_u2(data, pos); _d, pos = _read_u2(data, pos); nat_idx[i] = n
        elif tag in (8, 16, 19, 20): pos += 2
        elif tag in (3, 4): pos += 4
        elif tag in (5, 6): pos += 8; i += 1
        elif tag == 15: pos += 3
        elif tag in (17, 18): pos += 4
        else: raise ClassFormatError(f"unknown constant pool tag {tag}")
        i += 1
    _access, pos = _read_u2(data, pos); this_idx, pos = _read_u2(data, pos); super_idx, pos = _read_u2(data, pos)
    def cname(idx): return utf8.get(class_idx.get(idx, 0), "")
    info = ClassInfo(name=cname(this_idx), super_name=cname(super_idx), major=major)
    iface_count, pos = _read_u2(data, pos)
    for _ in range(iface_count):
        idx, pos = _read_u2(data, pos); info.interfaces.append(cname(idx))
    for idx in class_idx:
        name = utf8.get(class_idx[idx], "")
        if name.startswith("["):
            name = name.lstrip("[")
            if name.startswith("L") and name.endswith(";"): name = name[1:-1]
            else: continue
        if name and name != info.name: info.class_refs.add(name)
    for c, nt in ref_idx.values():
        owner = cname(c)
        if owner.startswith("["): continue
        member = utf8.get(nat_idx.get(nt, 0), "")
        if owner and member: info.member_refs.add((owner, member))
    info.source_file = _find_source_file(data, pos, utf8)
    return info


def _find_source_file(data, pos, utf8):
    try:
        for _ in range(2):
            n, pos = _read_u2(data, pos)
            for _ in range(n):
                pos += 6; attr_count, pos = _read_u2(data, pos)
                for _ in range(attr_count):
                    pos += 2; length, pos = _read_u4(data, pos); pos += length
        attr_count, pos = _read_u2(data, pos)
        for _ in range(attr_count):
            name_idx, pos = _read_u2(data, pos); length, pos = _read_u4(data, pos)
            if utf8.get(name_idx) == "SourceFile": return utf8.get(struct.unpack_from(">H", data, pos)[0], "")
            pos += length
    except (struct.error, IndexError): pass
    return ""
