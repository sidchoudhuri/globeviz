# ASCII Globe for the Commodore 64

A spinning ASCII Earth for the C64 — rendered in real time in machine
language at ~25 fps, with optional night side (city lights), SID music,
and static or scrolling text.

Texture and night-side concept from [adamsky/globe](https://github.com/adamsky/globe).
Original BASIC port by Terror. Water is blue `.` characters; land is brown,
shown as the texture's literal `H g @` characters in the lowercase charset.

## Requirements

- Python 3 (no packages needed for basic builds)
- [64tass](https://sourceforge.net/projects/tass64/) to assemble (`--build`)
- `py65` (`pip install py65`) — only needed when embedding a SID, so the
  generator can scan the tune's zero-page usage
- VICE's `c1541` if you want to build a .d64, `petcat` for the BASIC variants

## Quick start

```sh
# day globe
python3 globe_gen.py --build

# night side with city lights
python3 globe_gen.py --night --out globe_night.asm --build

# the full demo: night + music + title + scrolling credits
python3 globe_gen.py --night \
    --sid everything_counts.sid \
    --text "* everything counts *" \
    --scroll "Your credits scroll here ... " \
    --out globe_sid.asm --build

# build and run directly on an Ultimate 64 (REST API)
python3 globe_gen.py --night --build --run --ip 192.168.2.64
```

`--build` assembles the generated .asm into a .prg with 64tass.
`--run` POSTs the .prg to the Ultimate 64's `/v1/runners:run_prg` endpoint.
Use `--out somename.asm` to avoid overwriting a previous build (the .prg
gets the same name with a .prg extension).

## Options (globe_gen.py v2.3.0)

| Option | Default | What it does |
|---|---|---|
| `--night` | off | City lights on the dark side; the terminator stays fixed on screen while the map rotates through it |
| `--sid FILE.SID` | none | Embed a PSID tune, played via the KERNAL CIA IRQ. The tune must load between roughly $0B00 and $2FFF (the generator errors out if it doesn't fit) |
| `--text "..."` | none | Static text line, max 40 chars, centered |
| `--text-row N` | 0 | Screen row for the static text (0 or 24 recommended; 1–23 overlap the globe) |
| `--scroll "..."` | none | Scrolling ticker, any length |
| `--scroll-row N` | 24 | Screen row for the ticker |
| `--scroll-speed N` | 3 | Render loops per 1-character scroll step (higher = slower) |
| `--texture FILE` | globe_texture.txt | Day texture (ASCII: `. H g @`) |
| `--texture-night FILE` | globe_texture_night.txt | Night texture (ASCII: ` ` `.` `:` `;`) |
| `--out FILE` | globe.asm | Output assembly filename |
| `--build` | off | Assemble with 64tass |
| `--run` | off | Upload the .prg to the U64 |
| `--ip ADDR` | 192.168.2.64 | Ultimate 64 address |
| `--no-preview` | off | Skip the ASCII preview printed after generation |

All options combine freely. The .prg is a normal BASIC-stub program:
`LOAD`, `RUN`, done. On the U64 you can also just run it from the menu.

## The BASIC versions

Two more generators make pure BASIC V2 variants of the same globe:

- **gen_globe_fast.py** — pre-renders rotation frames into PRINT statements
  with embedded PETSCII color codes; starts instantly, ~5 fps.
  `--frames N` (42 is the RAM ceiling), `--night` for city lights.
  Emits a tokenized .prg directly.
- **gen_globe_basic.py** — the program computes everything itself on the
  C64 (~3–4 min of trig at startup, then ~1 fps forever). `--rle` stores
  the texture run-length encoded (25 → 12 disk blocks, ~15 s extra decode).
  Emits a petcat listing; tokenize with
  `petcat -w2 -o globe_basic.prg -- globe_basic.bas`.

## Putting it on a disk

```sh
c1541 -format "globe,64" d64 globe.d64 \
  -write globe.prg "globe ml" \
  -write globe_ml_sid.prg "globe sid"
```

## How it works (the short version)

All trigonometry happens in Python at build time. For each of the 1000
screen cells the generator precomputes two bytes: which texture row the
cell samples (stored as a memory page number, $FF meaning "outside the
disc") and its base longitude. The texture is resampled to 256 columns
with each row page-aligned, so on the 6502 a cell is just
`lda (ptr),y` with `y = longitude + angle` — longitude wrap-around is free
8-bit overflow. Rotating the globe is `inc angle`. Color RAM is written
right after each character through a 256-byte char→color lookup.

Night mode adds zero instructions: the night texture's pages sit after the
day texture's, and dark-side cells (one `dot(normal, sun)` per cell, done
in Python) simply point at night pages in their lookup entry.

SID playback hooks the play routine into the KERNAL's CIA interrupt at
`$0314` (`JSR play / JMP $EA31`), so tunes that reprogram the CIA timer set
their own tempo — double-speed tunes just work. Because the music
interrupts the renderer mid-frame, the generator emulates the tune first
and places the renderer's zero-page pointer at addresses the tune never
touches; every build is also verified in a 6502 emulator before it leaves
Python.

The scroller is a self-modifying pointer sliding over the message buffer,
whose first 40 bytes are duplicated at the end — every frame is a straight
40-byte copy, and the wrap is seamless.

## Files

| File | What |
|---|---|
| globe_gen.py | ML generator (this README's main subject) |
| gen_globe_fast.py | Pre-rendered BASIC generator |
| gen_globe_basic.py | Self-computing BASIC generator |
| globe_texture.txt | Day texture, 35×300 |
| globe_texture_night.txt | Night texture (city lights), 75×300 |
| globe.d64 | All builds on one disk |
