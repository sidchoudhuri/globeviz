#!/usr/bin/env python3
"""
gen_globe_fast.py -- pre-rendered rotating globe, pure C64 BASIC, instant start.

All projection + frame rendering happens here in Python, at full texture
resolution (no downsample). The emitted BASIC program is just PRINT
statements whose string literals carry the PETSCII control codes
(home/crsr-down, colors, RVS on/off) inline -- the classic type-in trick.
No precompute on the C64: it starts spinning immediately at PRINT speed.

The PRG is tokenized directly by this script (only PRINT/POKE/FOR/TO/
NEXT/GOTO/REM tokens needed). A readable listing can be produced with
petcat -2 globe_fast.prg.

Layout per frame: one PRINT homing the cursor and stepping down to the
first globe row, then one PRINT per row (trailing spaces trimmed, CR
advances the row -- no 40-column wrap games needed), then an optional
delay loop FOR D=1 TO DL.
"""

VERSION = "2.2.0"
BUILD = "2026-06-10-5"

import argparse
import math
import pathlib

# --- screen geometry (matches the slow BASIC version) ---
CX, CY = 19.5, 12.0
RX, RY = 12.0, 11.0
TEX_H = 35

# Literal texture chars in the lowercase charset: water '.' blue, land brown.
# texture char -> (petscii char, petscii color code, rvs, screencode, colorram)
# PETSCII: lowercase ascii <-> $41-$5A, uppercase <-> $C1-$DA.
CELLS = {
    ".": (46, 31, 0, 46, 6),    # '.' blue
    "H": (200, 149, 0, 72, 9),  # 'H' brown
    "g": (71, 149, 0, 7, 9),    # 'g' brown
    "@": (64, 149, 0, 0, 9),    # '@' brown
}

# Night side (--night): city-lights texture from adamsky/globe, chars
# ' ' '.' ':' ';' in ascending brightness -> black / grey / yellow / white.
CELLS_NIGHT = {
    ".": (46, 152, 0, 46, 12),  # dim light: grey
    ":": (58, 158, 0, 58, 7),   # light: yellow
    ";": (59, 5, 0, 59, 1),     # bright light: white
}

# Directional light fixed in camera space (mirrors the repo's fixed light:
# the terminator stays put on screen while the map rotates through it).
# Unit-ish vector toward the sun: upper-left, slightly in front.
SUN = (-0.83, -0.32, 0.45)
NIGHT_THRESHOLD = 0.0           # dot(normal, sun) < 0 -> night side

# tokens (note: operators like = are tokens too -- $B2 -- not ASCII!)
PRINT, GOTO, POKE, FOR, TO, NEXT, REM, EQ = 0x99, 0x89, 0x97, 0x81, 0xA4, 0x82, 0x8F, 0xB2
HOME, DOWN, RVSON, RVSOFF = 19, 17, 18, 146


def load_texture(path, pad="."):
    rows = [r for r in pathlib.Path(path).read_text().splitlines() if r.strip()]
    w = max(len(r) for r in rows)
    return [r.ljust(w, pad) for r in rows]


def projection():
    """Per cell: (phi frac 0..1, theta frac 0..1, is_night) or absent."""
    grid = {}
    for sy in range(25):
        for sx in range(40):
            nx = (sx - CX) / RX
            ny = (sy - CY) / RY
            d = nx * nx + ny * ny
            if d > 1.0:
                continue
            nz = math.sqrt(1.0 - d)
            phi = math.acos(max(-1.0, min(1.0, -ny)))
            theta = math.atan2(nz, nx)
            lum = nx * SUN[0] + ny * SUN[1] + nz * SUN[2]
            grid[(sy, sx)] = (phi / math.pi, theta / (2.0 * math.pi),
                              lum < NIGHT_THRESHOLD)
    return grid


def render_frame(tex, grid, frac, night_tex=None):
    """frac = rotation fraction 0..1 -> 25x40 of (char, is_night) or None.
    Night cells sample night_tex (own dimensions) when provided."""
    w = len(tex[0])
    frame = [[None] * 40 for _ in range(25)]
    for (sy, sx), (pf, tf, dark) in grid.items():
        if dark and night_tex is not None:
            nh, nw = len(night_tex), len(night_tex[0])
            ty = min(nh - 1, int(pf * nh))
            frame[sy][sx] = (night_tex[ty][int((tf + frac) * nw) % nw], True)
        else:
            ty = min(TEX_H - 1, int(pf * TEX_H))
            frame[sy][sx] = (tex[ty][int((tf + frac) * w) % w], False)
    return frame


def cell_def(c):
    """(char, is_night) -> CELLS row, or None for blank (night ' ')."""
    char, dark = c
    if dark:
        return CELLS_NIGHT.get(char)   # ' ' -> None -> blank
    return CELLS[char]


def encode_row(cells):
    """Row of (char, is_night)/None -> PETSCII bytes (codes embedded, CR-style)."""
    defs = [None if c is None else cell_def(c) for c in cells]
    # Trim only cells OUTSIDE the disc (blank in every frame). In-disc blanks
    # (night ' ') change between frames and must print a space to erase.
    last = max((i for i, c in enumerate(cells) if c is not None), default=-1)
    out = bytearray()
    rv, cc = 0, 0
    for d in defs[: last + 1]:
        if d is None:
            if rv:
                out.append(RVSOFF)
                rv = 0
            out.append(32)
            continue
        ch, col, rt, _, _ = d
        if rt != rv:
            out.append(RVSON if rt else RVSOFF)
            rv = rt
        if col != cc:
            out.append(col)
            cc = col
        out.append(ch)
    return bytes(out)


# ----------------------------------------------------------- tokenizer

def ident(s):
    """Code-context bytes: letters/digits/punct as PETSCII, operators as tokens."""
    OPS = {"=": 0xB2, "+": 0xAA, "-": 0xAB, "*": 0xAC, "/": 0xAD,
           ">": 0xB1, "<": 0xB3}
    out = bytearray()
    for ch in s:
        if ch in OPS:
            out.append(OPS[ch])
        else:
            assert ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:,;() $%", \
                f"unhandled char in code context: {ch!r}"
            out.append(ord(ch))
    return bytes(out)


def tokenize(lines):
    """lines: list of (lineno, payload bytes). Returns PRG bytes."""
    addr = 0x0801
    out = bytearray([0x01, 0x08])
    for num, body in lines:
        assert len(body) <= 250, f"line {num}: {len(body)} bytes"
        link = addr + 4 + len(body) + 1
        out += link.to_bytes(2, "little") + num.to_bytes(2, "little") + body + b"\x00"
        addr = link
    out += b"\x00\x00"
    return bytes(out)


def build_program(tex, grid, nf, night_tex=None):
    rows = sorted({sy for (sy, _) in grid})
    first = rows[0]

    lines = [
        (100, bytes([REM]) + ident(" ROTATING GLOBE - PRE-RENDERED")),
        (110, bytes([REM]) + ident(" TEXTURE: ADAMSKY/GLOBE - BASIC BY TERROR")),
        (120, ident("DL=0:") + bytes([POKE]) + ident("53280,0:") + bytes([POKE]) + ident("53281,0")),
        (130, bytes([PRINT]) + b'"' + bytes([147, 14]) + b'";')   # clr + lowercase charset,
    ]
    n = 1000
    for f in range(nf):
        frame = render_frame(tex, grid, f / nf, night_tex)
        prefix = bytes([HOME] + [DOWN] * first)
        for j, sy in enumerate(rows):
            body = encode_row(frame[sy])
            lit = (prefix if j == 0 else b"") + body
            lines.append((n, bytes([PRINT]) + b'"' + lit + b'"'))
            n += 1
        lines.append((n, bytes([FOR]) + ident("D=1") + bytes([TO]) + ident("DL:") + bytes([NEXT])))
        n = (n // 100 + 1) * 100
    lines.append((n, bytes([GOTO]) + ident("1000")))
    return lines


# ----------------------------------------------------------- validation

def simulate(lines, grid, tex, nf, night_tex=None):
    """Interpret the PRINT byte stream like the C64 screen editor and
    diff every frame against a direct reference render."""
    rows = sorted({sy for (sy, _) in grid})
    scr = [[32] * 40 for _ in range(25)]
    col = [[255] * 40 for _ in range(25)]   # 255 = never written
    body_lines = [b for _, b in lines if b[:1] == bytes([PRINT])]
    body_lines = body_lines[1:]             # skip the clr/charset line

    per_frame = len(rows)
    assert len(body_lines) == nf * per_frame
    cy = cx = 0
    for f in range(nf):
        for b in body_lines[f * per_frame:(f + 1) * per_frame]:
            lit = b[2:-1]                   # strip PRINT, quotes
            rv, cur = 0, None
            for byte in lit:
                if byte == HOME: cy = cx = 0
                elif byte == DOWN: cy += 1
                elif byte == RVSON: rv = 1
                elif byte == RVSOFF: rv = 0
                elif byte in (31, 30, 149, 152, 158, 5):
                    cur = {31: 6, 30: 5, 149: 9, 152: 12, 158: 7, 5: 1}[byte]
                else:
                    if 64 <= byte <= 95: sc = byte - 64
                    elif 160 <= byte <= 191: sc = byte - 64
                    elif 192 <= byte <= 223: sc = byte - 128
                    else: sc = byte
                    scr[cy][cx] = sc | (0x80 if rv else 0)
                    if cur is not None: col[cy][cx] = cur
                    cx += 1
            cy += 1; cx = 0                 # CR (also clears RVS)
        # diff against reference
        frame = render_frame(tex, grid, f / nf, night_tex)
        for sy in range(25):
            for sx in range(40):
                c = frame[sy][sx]
                d = None if c is None else cell_def(c)
                if d is None:
                    assert scr[sy][sx] == 32, f"f{f} ({sy},{sx}) not blank"
                else:
                    _, _, _, esc, ecol = d
                    assert scr[sy][sx] == esc, f"f{f} ({sy},{sx}) char"
                    assert col[sy][sx] == ecol, f"f{f} ({sy},{sx}) color"
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--night", action="store_true",
                    help="show city lights on the dark side (terminator fixed on screen)")
    ap.add_argument("--texture", default="globe_texture.txt")
    ap.add_argument("--texture-night", default="globe_texture_night.txt")
    ap.add_argument("--out", default="globe_fast.prg")
    args = ap.parse_args()

    here = pathlib.Path(__file__).parent
    tex = load_texture(here / args.texture)
    night_tex = load_texture(here / args.texture_night, pad=" ") if args.night else None
    grid = projection()
    lines = build_program(tex, grid, args.frames, night_tex)
    prg = tokenize(lines)
    simulate(lines, grid, tex, args.frames, night_tex)
    print(f"simulation: all {args.frames} frames match reference render")

    pathlib.Path(args.out).write_bytes(prg)
    free = 38911 - (len(prg) - 2)
    print(f"wrote {args.out}: {len(prg)} bytes ({args.frames} frames, "
          f"{len(lines)} lines, {free} BASIC bytes free)")
    assert free > 300, "too big for BASIC RAM (needs ~50 bytes for vars + stack)"


if __name__ == "__main__":
    main()
