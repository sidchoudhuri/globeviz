#!/usr/bin/env python3
"""
globe_gen.py -- rotating ASCII globe for the C64 (standalone PRG)

Reads the adamsky/globe earth texture (globe_texture.txt), does all the
spherical projection trig once in Python, and emits a 64tass source file
(globe.asm) containing:

  * a 35-row x 256-column texture (each row page-aligned, so longitude
    wrap-around is free 8-bit arithmetic on the 6502)
  * per-screen-cell lookup tables: texture row page ($FF = outside the
    disc) and base theta byte
  * a tight 6502 frame loop that rotates the globe in real time

Build:  64tass --cbm-prg globe.asm -o globe.prg
Run:    python3 globe_gen.py --build --run        (uploads to the U64)

Original BASIC by Terror; texture from https://github.com/adamsky/globe
"""

VERSION = "2.2.1"
BUILD = "2026-06-10-5"

import argparse
import math
import pathlib
import subprocess
import sys
import urllib.request

# ---------------------------------------------------------------- config

SCREEN_W, SCREEN_H = 40, 25
CX, CY = 19.5, 12.0          # disc centre in cells
RX, RY = 13.0, 12.0          # disc radii in cells (C64 cells ~square on PAL)
TEX_W = 256                  # resampled texture width -> theta is one byte
TEX_PAGE = 0x18              # texture page (0x18; moved to 0x38 when --sid)
SID_TABLES = 0x3000          # with --sid: tables here, texture at $3800
U64_IP = "192.168.2.64"

# Literal texture chars, lowercase charset: water '.' blue, land brown.
# Lowercase-charset screen codes: '@'=0, 'g'=7, 'H'=72, '.'=46. The VIC reads
# lowercase glyphs from char ROM shadow at $1800, so no conflict with our
# texture RAM at $1800.
CHAR_MAP = {
    ".": (46, 6),
    "H": (72, 9),
    "g": (7, 9),
    "@": (0, 9),
}

# --night: city-lights texture (adamsky/globe earth_night.txt) on the dark
# side. The color LUT is indexed by screen code, so night's dim '.' becomes
# ',' (code 44) to avoid colliding with blue day water at code 46.
CHAR_MAP_NIGHT = {
    " ": (32, 0),    # dark
    ".": (44, 12),   # dim light: ',' grey
    ":": (58, 7),    # light: ':' yellow
    ";": (59, 1),    # bright: ';' white
}
NIGHT_DEFAULT = (32, 0)

# Directional light fixed in camera space (terminator static on screen,
# matching the fast/BASIC versions and the original repo's fixed light).
SUN = (-0.83, -0.32, 0.45)
DEFAULT_CELL = (46, 6)       # anything unexpected renders as water

PREVIEW_GLYPH = {46: ".", 72: "H", 7: "g", 0: "@", 32: "_", 44: ",", 58: ":", 59: ";"}

# ---------------------------------------------------------------- texture


def parse_psid(path):
    """Returns dict(load, init, play, song0, data) for a PSID file."""
    import struct
    d = pathlib.Path(path).read_bytes()
    assert d[:4] == b"PSID", "only PSID supported (RSID needs its own environment)"
    ver, off, load, init, play, songs, start = struct.unpack(">HHHHHHH", d[4:18])
    data = d[off:]
    if load == 0:
        load = data[0] | data[1] << 8
        data = data[2:]
    assert play != 0, "play address 0 (RSID-style) not supported"
    return {"load": load, "init": init, "play": play,
            "song0": max(0, start - 1), "data": data,
            "name": d[22:54].rstrip(b"\0").decode("latin1")}


ZP_CANDIDATES = [0xFB, 0xFD, 0x02, 0x04]   # pairs (c, c+1), KERNAL-safe


def scan_tune_zp(sid, plays=2000):
    """Emulate the tune standalone; return set of zp addresses it reads or
    writes, so the renderer's zp pointer can dodge them. None if py65 absent."""
    try:
        from py65.devices.mpu6502 import MPU
        from py65.memory import ObservableMemory
    except ImportError:
        return None
    base = bytearray(65536)
    base[sid["load"]:sid["load"] + len(sid["data"])] = sid["data"]
    mem = ObservableMemory(subject=base)
    touched = set()
    mem.subscribe_to_write(range(0x100), lambda a, v: touched.add(a))
    mem.subscribe_to_read(range(0x100), lambda a: touched.add(a))
    mpu = MPU(memory=mem)
    for addr, a in [(sid["init"], sid["song0"])] + [(sid["play"], 0)] * plays:
        mpu.a, mpu.x, mpu.y = a, 0, 0
        mpu.sp = 0xFD
        mem[0x01FE] = 0xFE
        mem[0x01FF] = 0xFF
        mpu.pc = addr
        n = 0
        while mpu.pc != 0xFFFF:
            mpu.step()
            n += 1
            assert n < 1000000, "tune routine never returned"
    return touched


def pick_zp_ptr(touched):
    for c in ZP_CANDIDATES:
        if c not in touched and (c + 1) not in touched:
            return c
    raise SystemExit("no free zp pair for the render pointer; tune too greedy")


def load_texture(path, pad="."):
    rows = pathlib.Path(path).read_text().splitlines()
    rows = [r for r in rows if r.strip()]
    width = max(len(r) for r in rows)
    return [r.ljust(width, pad) for r in rows]


def resample_texture(rows, char_map=None, default=None, out_h=None):
    """rows x ~300 ascii -> out_h x 256 screen codes (rows resampled too)."""
    char_map = char_map or CHAR_MAP
    default = default or DEFAULT_CELL
    h = len(rows)
    out_h = out_h or h
    w = max(len(r) for r in rows)
    out = []
    for j in range(out_h):
        row = rows[int(j * h / out_h)]
        line = bytearray(TEX_W)
        for i in range(TEX_W):
            src = row[int(i * w / TEX_W)] if int(i * w / TEX_W) < len(row) else " "
            line[i] = char_map.get(src, default)[0]
        out.append(bytes(line))
    return out


# ----------------------------------------------------------- projection


def build_tables(tex_h, night_h=0):
    """Per screen cell: texture row page (or $FF) and base theta byte.
    With night_h > 0, dark-side cells point at the night texture's pages,
    which sit directly after the day texture's tex_h pages."""
    rowhi = bytearray(1000)
    thbase = bytearray(1000)
    for sy in range(SCREEN_H):
        for sx in range(SCREEN_W):
            i = sy * SCREEN_W + sx
            nx = (sx - CX) / RX
            ny = (sy - CY) / RY
            d = nx * nx + ny * ny
            if d > 1.0:
                rowhi[i] = 0xFF
                continue
            nz = math.sqrt(1.0 - d)
            phi = math.acos(max(-1.0, min(1.0, -ny)))   # 0 at north pole
            theta = math.atan2(nz, nx)                  # front hemisphere
            dark = night_h and (nx * SUN[0] + ny * SUN[1] + nz * SUN[2]) < 0
            if dark:
                ty = min(night_h - 1, int(phi / math.pi * night_h))
                rowhi[i] = TEX_PAGE + tex_h + ty
            else:
                ty = min(tex_h - 1, int(phi / math.pi * tex_h))
                rowhi[i] = TEX_PAGE + ty
            assert rowhi[i] < 0x80, "texture page >= $80 breaks the bmi skip"
            thbase[i] = int(theta / (2.0 * math.pi) * TEX_W) & 0xFF
    return rowhi, thbase


def build_collut(night=False):
    lut = bytearray(256)
    for code, color in CHAR_MAP.values():
        lut[code] = color
    if night:
        for code, color in CHAR_MAP_NIGHT.values():
            lut[code] = color
    return lut


# -------------------------------------------------------------- preview


def preview(tex, rowhi, thbase, angle=0):
    lines = []
    for sy in range(SCREEN_H):
        line = []
        for sx in range(SCREEN_W):
            i = sy * SCREEN_W + sx
            if rowhi[i] == 0xFF:
                line.append(" ")
            else:
                row = tex[rowhi[i] - TEX_PAGE]
                code = row[(thbase[i] + angle) & 0xFF]
                line.append(PREVIEW_GLYPH.get(code, "?"))
        lines.append("".join(line))
    return "\n".join(lines)


# ------------------------------------------------------------- assembly


def byte_rows(data, per_line=16):
    out = []
    for o in range(0, len(data), per_line):
        chunk = ", ".join("${:02x}".format(b) for b in data[o : o + per_line])
        out.append("        .byte " + chunk)
    return "\n".join(out)


def render_block(n):
    base = n * 250
    return f"""        ldx #0
rblk{n}   lda rowhi+{base},x
        bmi rskp{n}
        sta ptr+1
        lda thbase+{base},x
        clc
        adc angle
        tay
        lda (ptr),y
        sta $0400+{base},x
        tay
        lda collut,y
        sta $d800+{base},x
rskp{n}   inx
        cpx #250
        bne rblk{n}
"""


def emit_asm(tex, rowhi, thbase, collut, charset_d018=0x17, sid=None, zp_ptr=0xFB):
    blocks = "".join(render_block(n) for n in range(4))
    tex_bytes = b"".join(tex)
    tex_org = TEX_PAGE << 8
    if sid:
        music_init = f"""
        sei             ; hook SID player into the KERNAL CIA IRQ
        lda #${sid['song0']:02x}
        ldx #$00
        ldy #$00
        jsr ${sid['init']:04x}  ; sid init
        lda #<irq
        sta $0314
        lda #>irq
        sta $0315
        cli
"""
        sid_block = f"""
irq     jsr ${sid['play']:04x}  ; sid play, driven at KERNAL CIA rate
        jmp $ea31       ; full KERNAL IRQ (keyboard + CIA ack)

        .cerror * > ${sid['load']:04x}, "code overlaps SID data"
        * = ${sid['load']:04x}
sid                     ; {sid['name']}
{byte_rows(sid['data'])}

        .cerror * > ${SID_TABLES:04x}, "SID data overlaps tables"
        * = ${SID_TABLES:04x}
"""
    else:
        music_init = ""
        sid_block = ""
    return f"""; globe.asm -- rotating ASCII globe, generated by globe_gen.py v{VERSION} ({BUILD})
; texture: https://github.com/adamsky/globe | original BASIC by Terror
; build: 64tass --cbm-prg globe.asm -o globe.prg

ptr     = ${zp_ptr:02x}           ; zp pointer into texture row (lo byte always 0)
                        ; (chosen to avoid the SID tune's zero-page usage)

        * = $0801
        .word (+), 2026
+       .null $9e, format("%4d", start)
        .word 0
angle   .byte 0         ; rotation (absolute: zp is contested territory)

start   lda #${charset_d018:02x}
        sta $d018       ; lowercase charset
        lda #0
        sta $d020       ; black border
        sta $d021       ; black background
        sta ptr         ; texture rows are page-aligned -> lo always 0
        sta angle

        ldx #0          ; clear screen + color RAM
clr     lda #32
        sta $0400,x
        sta $0400+250,x
        sta $0400+500,x
        sta $0400+750,x
        lda #0
        sta $d800,x
        sta $d800+250,x
        sta $d800+500,x
        sta $d800+750,x
        inx
        cpx #250
        bne clr

{music_init}main
wait    lda $d012       ; loose vsync
        bne wait
        jsr render
        inc angle
        jmp main

; one indexed texture fetch per visible cell, ~25 fps on PAL
render
{blocks}        rts
{sid_block}
        .align $100
collut                  ; screen code -> color RAM value
{byte_rows(collut)}

rowhi                   ; texture row page per cell, $ff = outside disc
{byte_rows(rowhi)}

thbase                  ; base theta byte per cell
{byte_rows(thbase)}

        .cerror * > ${tex_org:04x}, "code/tables overlap the texture"
        * = ${tex_org:04x}
tex                     ; 35 rows x 256 cols, one page per row
{byte_rows(tex_bytes)}
"""


# ----------------------------------------------------------------- main


def run_on_u64(ip, prg_path):
    prg = pathlib.Path(prg_path).read_bytes()
    url = f"http://{ip}/v1/runners:run_prg"
    req = urllib.request.Request(
        url, data=prg, method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f"U64 responded: {resp.status} {resp.reason}")


def main():
    ap = argparse.ArgumentParser(description="generate rotating-globe PRG for C64")
    ap.add_argument("--night", action="store_true",
                    help="city lights on the dark side (terminator fixed on screen)")
    ap.add_argument("--texture", default="globe_texture.txt")
    ap.add_argument("--texture-night", default="globe_texture_night.txt")
    ap.add_argument("--sid", default=None, metavar="FILE.SID",
                    help="embed a PSID tune, played via the KERNAL CIA IRQ")
    ap.add_argument("--out", default="globe.asm")
    ap.add_argument("--build", action="store_true", help="assemble with 64tass")
    ap.add_argument("--run", action="store_true", help="upload PRG to the U64 (implies --build)")
    ap.add_argument("--ip", default=U64_IP)
    ap.add_argument("--no-preview", action="store_true")
    args = ap.parse_args()

    global TEX_PAGE
    sid = None
    if args.sid:
        sid = parse_psid(args.sid)
        TEX_PAGE = 0x39            # make room: sid hole, tables at $3000-$38FF
        end = sid["load"] + len(sid["data"])
        assert end <= SID_TABLES, f"SID data ends ${end:04x}, past tables"
        print(f"sid: '{sid['name']}' load ${sid['load']:04x}-${end-1:04x}, "
              f"init ${sid['init']:04x}, play ${sid['play']:04x}")
        touched = scan_tune_zp(sid)
        if touched is None:
            print("WARNING: py65 not installed -- cannot scan the tune's zero-page")
            print(f"         usage; assuming $fb/$fc are free. pip install py65")
            zp_ptr = 0xFB
        else:
            zp_ptr = pick_zp_ptr(touched)
            print(f"tune touches zp: {' '.join(f'${a:02x}' for a in sorted(touched))}"
                  f" -> render pointer at ${zp_ptr:02x}/${zp_ptr+1:02x}")

    if not args.sid:
        zp_ptr = 0xFB

    here = pathlib.Path(__file__).parent
    tex_path = pathlib.Path(args.texture)
    if not tex_path.exists():
        tex_path = here / args.texture

    rows = load_texture(tex_path)
    tex = resample_texture(rows)
    night_h = 0
    if args.night:
        ntex_path = pathlib.Path(args.texture_night)
        if not ntex_path.exists():
            ntex_path = here / args.texture_night
        nrows = load_texture(ntex_path, pad=" ")
        night = resample_texture(nrows, CHAR_MAP_NIGHT, NIGHT_DEFAULT, out_h=len(rows))
        night_h = len(night)
        tex = tex + night                  # night pages follow day pages
    rowhi, thbase = build_tables(len(rows), night_h)
    collut = build_collut(args.night)

    asm = emit_asm(tex, rowhi, thbase, collut, sid=sid, zp_ptr=zp_ptr)
    out = pathlib.Path(args.out)
    out.write_text(asm)
    print(f"wrote {out} ({len(asm)} bytes, texture {len(rows)}x{len(rows[0])} -> {len(rows)}x{TEX_W})")

    if not args.no_preview:
        print(preview(tex, rowhi, thbase))

    if args.build or args.run:
        prg = out.with_suffix(".prg")
        cmd = ["64tass", "--cbm-prg", str(out), "-o", str(prg)]
        print("$ " + " ".join(cmd))
        r = subprocess.run(cmd)
        if r.returncode != 0:
            sys.exit(r.returncode)
        size = prg.stat().st_size
        print(f"built {prg} ({size} bytes, ${0x0801:04x}-${0x0801 + size - 3:04x})")
        if args.run:
            run_on_u64(args.ip, prg)


if __name__ == "__main__":
    main()
