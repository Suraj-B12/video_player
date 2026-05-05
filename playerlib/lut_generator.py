"""
Generate Sony S-Log3 + S-Gamut3.Cine conversion LUTs from publicly documented
math. Output: standard Adobe .cube 3D LUT files, compatible with mpv's lut3d
filter and most NLEs.

References:
  - Sony S-Log3 / S-Gamut3.Cine technical brief (SUP-WP-COLOR-1402EN)
  - ITU-R BT.709 OETF
  - sRGB / IEC 61966-2-1 OETF

Why we generate instead of redistributing Sony's official LUTs: Sony does
not grant a redistribution license for their packaged LUTs. The transfer
function and primaries math is openly published; the linear conversion we
compute here is functionally identical to Sony's "linear" pack (S-Gamut3.Cine
to Rec.709) without copying their proprietary creative looks.

Usage:
    py -3.13 -m playerlib.lut_generator [--size 33]
"""

from __future__ import annotations

import argparse
from pathlib import Path

# ─── Color matrices ──────────────────────────────────────────────────────────
# S-Gamut3.Cine RGB → CIE XYZ (D65), per Sony S-Gamut3.Cine spec.
SGAMUT3CINE_TO_XYZ = [
    [ 0.5990839,  0.2489255,  0.1024943],
    [ 0.2150758,  0.8850685, -0.1001443],
    [-0.0320658, -0.0276134,  1.1487819],
]
# XYZ → Rec.709/sRGB RGB (D65), ITU-R BT.709 / IEC 61966-2-1.
XYZ_TO_REC709 = [
    [ 3.2406255, -1.5372080, -0.4986286],
    [-0.9689307,  1.8757561,  0.0415175],
    [ 0.0557101, -0.2040211,  1.0569959],
]
# XYZ → DCI-P3 D65 RGB.
XYZ_TO_P3D65 = [
    [ 2.4934969, -0.9313836, -0.4027108],
    [-0.8294890,  1.7626641,  0.0236247],
    [ 0.0358458, -0.0761724,  0.9568845],
]


def _matmul3x3(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _matmul3v(M, v):
    return [sum(M[i][k] * v[k] for k in range(3)) for i in range(3)]


# ─── Transfer functions ──────────────────────────────────────────────────────
def slog3_eotf(cv: float) -> float:
    """S-Log3 EOTF — code value [0..1] → linear scene-referred (0.18 = 18% gray)."""
    threshold = 171.2102946929 / 1023.0
    if cv >= threshold:
        return (10.0 ** ((cv * 1023.0 - 420.0) / 261.5)) * 0.19 - 0.01
    return (cv * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0)


def rec709_oetf(linear: float) -> float:
    """ITU-R BT.709 OETF — linear → Rec.709 encoded value."""
    if linear < 0:
        return 0.0
    if linear < 0.018:
        return 4.5 * linear
    return 1.099 * (linear ** 0.45) - 0.099


def srgb_oetf(linear: float) -> float:
    """sRGB OETF — used for SDR display on Display P3 panels."""
    if linear < 0:
        return 0.0
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * (linear ** (1.0 / 2.4)) - 0.055


def s_curve(x: float, contrast: float = 1.5) -> float:
    """Cinematic S-curve. 0.5 → 0.5 fixed; symmetric around midtone.
    Approximates the contrast bump in Sony's LC-709A look."""
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    if x < 0.5:
        return 0.5 * (2 * x) ** contrast
    return 1.0 - 0.5 * (2 * (1 - x)) ** contrast


# ─── LUT recipes ─────────────────────────────────────────────────────────────
_M_SCC_TO_REC709 = _matmul3x3(XYZ_TO_REC709, SGAMUT3CINE_TO_XYZ)
_M_SCC_TO_P3D65 = _matmul3x3(XYZ_TO_P3D65, SGAMUT3CINE_TO_XYZ)


def slog3_to_rec709(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Linear conversion: S-Log3 + S-Gamut3.Cine → Rec.709 sRGB."""
    lr, lg, lb = slog3_eotf(r), slog3_eotf(g), slog3_eotf(b)
    or_, og, ob = _matmul3v(_M_SCC_TO_REC709, [lr, lg, lb])
    return rec709_oetf(or_), rec709_oetf(og), rec709_oetf(ob)


def slog3_to_rec709_cine(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Cinematic conversion: S-Log3 + S-Gamut3.Cine → Rec.709 with S-curve."""
    or_, og, ob = slog3_to_rec709(r, g, b)
    return s_curve(or_), s_curve(og), s_curve(ob)


def slog3_to_p3d65(r: float, g: float, b: float) -> tuple[float, float, float]:
    """SDR conversion: S-Log3 + S-Gamut3.Cine → DCI-P3 D65 (sRGB OETF)."""
    lr, lg, lb = slog3_eotf(r), slog3_eotf(g), slog3_eotf(b)
    or_, og, ob = _matmul3v(_M_SCC_TO_P3D65, [lr, lg, lb])
    return srgb_oetf(or_), srgb_oetf(og), srgb_oetf(ob)


# ─── Writer ──────────────────────────────────────────────────────────────────
def write_cube(path: Path, size: int, recipe, title: str | None = None) -> None:
    """Write a 3D LUT in Adobe .cube format. recipe(r, g, b) → (r, g, b)."""
    out: list[str] = []
    if title:
        out.append(f'TITLE "{title}"')
    out.append(f"LUT_3D_SIZE {size}")
    out.append("DOMAIN_MIN 0.0 0.0 0.0")
    out.append("DOMAIN_MAX 1.0 1.0 1.0")
    out.append("")

    denom = size - 1
    for b_i in range(size):
        for g_i in range(size):
            for r_i in range(size):
                ro, go, bo = recipe(r_i / denom, g_i / denom, b_i / denom)
                # Hard-clip to [0, 1] — soft tone-mapping would soften highlights
                # but for a monitoring LUT clipping is the conventional choice.
                ro = 0.0 if ro < 0 else (1.0 if ro > 1 else ro)
                go = 0.0 if go < 0 else (1.0 if go > 1 else go)
                bo = 0.0 if bo < 0 else (1.0 if bo > 1 else bo)
                out.append(f"{ro:.6f} {go:.6f} {bo:.6f}")

    path.write_text("\n".join(out), encoding="utf-8")


RECIPES: tuple[tuple[str, callable, str], ...] = (
    ("SLog3_SGamut3Cine_to_Rec709.cube",
     slog3_to_rec709,
     "Sony S-Log3/S-Gamut3.Cine -> Rec.709 (linear)"),
    ("SLog3_SGamut3Cine_to_Rec709_Cine.cube",
     slog3_to_rec709_cine,
     "Sony S-Log3/S-Gamut3.Cine -> Rec.709 (cinematic S-curve)"),
    ("SLog3_SGamut3Cine_to_DCI-P3-D65.cube",
     slog3_to_p3d65,
     "Sony S-Log3/S-Gamut3.Cine -> DCI-P3 D65 (SDR)"),
)


def generate_all(out_dir: Path, size: int = 33) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for filename, recipe, title in RECIPES:
        target = out_dir / filename
        print(f"  {filename} ({size}^3 entries)")
        write_cube(target, size, recipe, title=title)
        paths.append(target)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Sony S-Log3 conversion LUTs.")
    parser.add_argument("--size", type=int, default=33,
                        help="LUT grid size (33 standard, 65 fine). Default: 33.")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent.parent / "luts" / "conversions",
                        help="Output directory.")
    args = parser.parse_args()

    print(f"Generating S-Log3 conversion LUTs into {args.out}")
    paths = generate_all(args.out, size=args.size)
    print(f"Done — {len(paths)} LUT(s) written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
