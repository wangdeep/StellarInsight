#!/usr/bin/env python3
"""
download_nebulae.py — Download real EVE Online nebula textures from CCP's CDN.

Run once from the xylon_eve directory (or project root):
    python3 scripts/download_nebulae.py
    python3 scripts/download_nebulae.py --force   # re-download all

Downloads DDS cubemap textures directly from CCP's resource CDN,
extracts a face, converts to JPEG, and saves to static/nebulae/.

Requirements: pip install requests Pillow
"""

import os, sys, time, struct
from pathlib import Path
from io import BytesIO

try:
    import requests
    from PIL import Image
except ImportError:
    print("ERROR: Missing dependencies. Run:")
    print("  pip install requests Pillow")
    sys.exit(1)

# Output to static/nebulae/ relative to this script's parent directory
OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "nebulae"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TARGET_WIDTH = 400
QUALITY = 82
HEADERS = {"User-Agent": "XylonBot/1.4 (EVE Corp Tool; stellarinsight)"}

# ── Region code → (filename, CCP CDN URL) ────────────────────────────────────
NEBULA_SOURCES = {
    # ── Amarr ──
    "a01": ("genesis",              "http://res.eveonline.ccpgames.com/fb/fbdd62f5fe5b4b38_37f3dbbf6e2a48cd006d3fea764ac7c2"),
    "a02": ("kador",                "http://res.eveonline.ccpgames.com/65/65a09a5f64475ef9_89c8a8950be55c6ea546b49d84da5af8"),
    "a03": ("domain",               "http://res.eveonline.ccpgames.com/8e/8e2045b05e9aeb5e_2f4793ec99f6966749fa1c862b82cfb6"),
    "a04": ("the_bleak_lands",      "http://res.eveonline.ccpgames.com/85/85d2a394d335433f_d5cd6cd448c22ff1f8ee2d19c26e1b3e"),
    "a05": ("devoid",               "http://res.eveonline.ccpgames.com/da/daa18f450ef5d304_4df40248edf2e128850bd3260c77603e"),
    "a06": ("tash-murkon",          "http://res.eveonline.ccpgames.com/75/7512f0439de0f945_f7aa6452497fac7a728a9db2552e69cd"),
    "a07": ("kor-azor",             "http://res.eveonline.ccpgames.com/15/15f2c1bf42cd346a_612f7f9bdac2a973c97cb514210e8e2b"),
    "a08": ("aridia",               "http://res.eveonline.ccpgames.com/da/da6f5e3d1b76602b_f3665f521abd7ea7aef2119d9711ff00"),
    "a09": ("khanid",               "http://res.eveonline.ccpgames.com/59/59d09b18e14c0240_b52302b8018f39b36667dd667ae17330"),
    "a10": ("querious",             "http://res.eveonline.ccpgames.com/fe/fec97d13928c6dba_08f8336a39af5fc3dbaacf377fc1292e"),
    "a11": ("delve",                "http://res.eveonline.ccpgames.com/cf/cf6651b9365d6655_1cbc58779d990253411fdf6e38d48878"),
    "a12": ("period_basis",         "http://res.eveonline.ccpgames.com/bf/bf1857b85ad63714_0146c77fee83665c64457b6a9bdf64b6"),
    "a13": ("derelik",              "http://res.eveonline.ccpgames.com/8c/8c4800862d53ec8f_f4535b9c786fc7ba1f667fd87d663a99"),
    "a14": ("providence",           "http://res.eveonline.ccpgames.com/37/373b801a7ace916e_a6a07b53b08d1ca381e46b7a7d7f41f6"),
    "a15": ("catch",                "http://res.eveonline.ccpgames.com/e0/e03b439f6247b0c9_a9a1c6a8227e5032f623c4734e1b8921"),
    "a16": ("stain",                "http://res.eveonline.ccpgames.com/b6/b6e2632e3dd7a348_da0fd8010304da3b2e4d1a4b9be38559"),
    "a17": ("paragon_soul",         "http://res.eveonline.ccpgames.com/29/29b99fe47d9a0c33_0ec5b6347505ddedefaa193142702072"),
    "a18": ("esoteria",             "http://res.eveonline.ccpgames.com/2e/2e8c08c61560dac2_11a244119c06864202fa4ac21d269886"),
    # ── Caldari ──
    "c01": ("the_citadel",          "http://res.eveonline.ccpgames.com/a7/a74c90e0df6d352e_b2606d300e06f2aa952b1f325773a548"),
    "c02": ("the_forge",            "http://res.eveonline.ccpgames.com/95/95f06e84f4c00ff3_9b88438cf69db5b5225149a266f443d6"),
    "c03": ("lonetrek",             "http://res.eveonline.ccpgames.com/2c/2c476b9e6cb9b608_a9a8fb24754044935878c7e009568c08"),
    "c04": ("black_rise",           "http://res.eveonline.ccpgames.com/2d/2dab17692ad88d15_af630e8afdc564cd0c61cc6f7f15bc92"),
    "c05": ("pure_blind",           "http://res.eveonline.ccpgames.com/b7/b7a3ebf9f4ce1a7a_a8bd67e64dc5cc2dd454f53a51bde589"),
    "c06": ("deklein",              "http://res.eveonline.ccpgames.com/98/98123be44dfdec4f_3685b2a579c29e16f2bcaadda084837a"),
    "c07": ("branch",               "http://res.eveonline.ccpgames.com/c9/c90e9c216dabcdd4_a5386981c5de524dfdbbdc1c00bf9e4c"),
    "c08": ("tenal",                "http://res.eveonline.ccpgames.com/e7/e7d58b1f741c48f1_adec0a03c83f7c68fe9b42b95f72f7a3"),
    "c09": ("tribute",              "http://res.eveonline.ccpgames.com/4d/4d25a2142672bab6_32ba3b9551c8f0a7540c10b98078f7a1"),
    "c10": ("vale_of_the_silent",   "http://res.eveonline.ccpgames.com/99/99208419e98be558_c80cda89d994ad6b48462144217c38e3"),
    "c11": ("geminate",             "http://res.eveonline.ccpgames.com/92/92492da801c6ff03_163ff0a2baffd3d5d59f8bac31baaa4c"),
    "c12": ("venal",                "http://res.eveonline.ccpgames.com/ad/adffe24ed22674fe_bcb5e40b2e77d650dbb4e724e6b69387"),
    "c14": ("the_kalevala_expanse", "http://res.eveonline.ccpgames.com/7a/7af6b262243d61a4_d26052b4c691c02cf5a3339059322461"),
    "c15": ("malpais",              "http://res.eveonline.ccpgames.com/a8/a8c4422c70d7c15f_fea842550fcdb4be0ab6fc64b0474e2d"),
    "c16": ("perrigen_falls",       "http://res.eveonline.ccpgames.com/7f/7faef1ba7692900a_e77de2ce29e8e168660a862ec8fae0f0"),
    "c17": ("oasa",                 "http://res.eveonline.ccpgames.com/15/15671360b326d4e5_413a3b48eef2a04145d3f3b03f5fa3f9"),
    "c18": ("outer_passage",        "http://res.eveonline.ccpgames.com/2f/2f2fe35702f110e0_1f48fcce1a9d95acc937499cedb21342"),
    "c19": ("cobalt_edge",          "http://res.eveonline.ccpgames.com/ca/ca2e09b2da79d54b_bf80cf4d0802052eeda629aeb5fc9e83"),
    # ── Gallente ──
    "g01": ("sinq_laison",          "http://res.eveonline.ccpgames.com/9d/9d15140ca81eea3a_eb99a76d700678069715a4a24ac11755"),
    "g02": ("everyshore",           "http://res.eveonline.ccpgames.com/2a/2a93977f42e6690f_88a7655012b231404f0eaa9a5b9af9b8"),
    "g03": ("essence",              "http://res.eveonline.ccpgames.com/5d/5d63eeb17068b394_1d7eeab15e6cd20d8902d65fac14bc25"),
    "g04": ("verge_vendor",         "http://res.eveonline.ccpgames.com/7e/7e86da9877da2d49_2774a3d3d6ebe017f57ac541bd32c65f"),
    "g05": ("placid",               "http://res.eveonline.ccpgames.com/d5/d587171390610dee_b3571d309642405c712e47f97dbb63f9"),
    "g06": ("syndicate",            "http://res.eveonline.ccpgames.com/c8/c80536dd932c88b3_6651dc4d6044954db3904ad714b1f08c"),
    "g07": ("cloud_ring",           "http://res.eveonline.ccpgames.com/55/552dfa27536a1fc8_71d619c9a677a6d4549e6d87577c425e"),
    "g08": ("outer_ring",           "http://res.eveonline.ccpgames.com/9b/9bbc27b126083c7d_e2c108f2978d4d00aec0cf9db5ccb131"),
    "g09": ("solitude",             "http://res.eveonline.ccpgames.com/cc/ccd79fbf2af35742_b2c528e85b29473358faecce9519a90a"),
    "g10": ("fade",                 "http://res.eveonline.ccpgames.com/a3/a3dc40eb0aec1864_efac28d6ab6b28c3118a8359449dd7a8"),
    "g11": ("fountain",             "http://res.eveonline.ccpgames.com/36/3681e8f83fa18a1f_2550059bfef1b79b22a0be1587129759"),
    # ── Minmatar ──
    "m01": ("heimatar",             "http://res.eveonline.ccpgames.com/7c/7c5fecd06297e9c0_33e7db56df57bf5ece3aa95f00b564db"),
    "m02": ("metropolis",           "http://res.eveonline.ccpgames.com/6f/6fb4f3cba7bea236_69eba5cf1a9878d61619d0ef3e265ad5"),
    "m03": ("molden_heath",         "http://res.eveonline.ccpgames.com/8d/8d3e773064363c72_1cccd9002f13d84b2ca21aa40b5178e4"),
    "m04": ("great_wildlands",      "http://res.eveonline.ccpgames.com/39/39fa2be914ce693d_aa8595cc35a3713fcfd6821794db6270"),
    # ── J-Space / Wormhole ──
    "j01": ("wh_generic",           "http://res.eveonline.ccpgames.com/f1/f1eeb0300c591529_b0d5553979c2c247d83d796c1fcf67ee"),
    "j02": ("wh_c5c6",              "http://res.eveonline.ccpgames.com/0d/0d950df736ab8da8_7cd9440d71b6bf50ac2bf01984922807"),
    # ── Special ──
    "gch01": ("pochven",            "http://res.eveonline.ccpgames.com/aa/aa6c1773b07ab26d_1e0d455d70cccea389c45250d6747677"),
}


def extract_face_from_dds(data: bytes) -> "Image.Image":
    """Extract a usable face from a DDS cubemap texture."""
    try:
        img = Image.open(BytesIO(data))
        img = img.convert("RGB")
        w, h = img.size
        if h >= w * 5:      # Vertical cubemap strip
            face = img.crop((0, 0, w, w))
        elif w >= h * 5:    # Horizontal cubemap strip
            face = img.crop((0, 0, h, h))
        else:
            face = img
        return face
    except Exception:
        pass

    # Manual DDS parse for unsupported formats
    if data[:4] != b'DDS ':
        raise RuntimeError("Not a DDS file")
    hdr = struct.unpack_from('<IIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII', data, 4)
    height, width = hdr[2], hdr[3]
    pixel_data = data[128:]

    face_size = width * width * 4
    if len(pixel_data) >= face_size:
        try:
            face = Image.frombytes("RGBA", (width, width), pixel_data[:face_size], "raw", "BGRA")
            return face.convert("RGB")
        except Exception:
            pass

    face_size_rgb = width * width * 3
    if len(pixel_data) >= face_size_rgb:
        try:
            return Image.frombytes("RGB", (width, width), pixel_data[:face_size_rgb])
        except Exception:
            pass

    raise RuntimeError(f"Cannot parse DDS: {width}x{height}, {len(pixel_data)} pixel bytes")


def download_and_convert(code: str, name: str, url: str, force: bool = False) -> bool:
    out_path = OUT_DIR / f"{name}.jpg"
    if out_path.exists() and not force:
        print(f"  · {name}.jpg  (already exists, skipping)")
        return True

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            print(f"  ✗ {name}  HTTP {resp.status_code}")
            return False

        raw = resp.content
        if len(raw) < 1000:
            print(f"  ✗ {name}  response too small ({len(raw)}B)")
            return False

        face = extract_face_from_dds(raw)
        ratio = TARGET_WIDTH / face.width
        new_h = max(1, int(face.height * ratio))
        face = face.resize((TARGET_WIDTH, new_h), Image.LANCZOS)
        face.save(str(out_path), "JPEG", quality=QUALITY, optimize=True)
        kb = out_path.stat().st_size / 1024
        print(f"  ✓ {name}.jpg  ({face.width}×{new_h}px, {kb:.0f} KB)  [{code}]")
        return True

    except Exception as exc:
        print(f"  ✗ {name}  {exc}")
        return False


def main():
    force = "--force" in sys.argv or "-f" in sys.argv
    print("=" * 60)
    print("Stellar Insight — Nebula Downloader")
    print("=" * 60)
    print(f"CDN:    res.eveonline.ccpgames.com")
    print(f"Output: {OUT_DIR}")
    if force:
        print("Mode:   FORCE re-download all")
    print()

    ok = fail = skip = 0
    for code, (name, url) in NEBULA_SOURCES.items():
        out_path = OUT_DIR / f"{name}.jpg"
        if out_path.exists() and not force:
            skip += 1
            continue
        if download_and_convert(code, name, url, force):
            ok += 1
        else:
            fail += 1
        time.sleep(0.3)

    print()
    total = len(list(OUT_DIR.glob("*.jpg")))
    print(f"Downloaded: {ok}  |  Skipped: {skip}  |  Failed: {fail}")
    print(f"Total images on disk: {total}")
    if fail:
        print(f"\nNote: {fail} failed — CCP CDN sometimes requires a real game client UA.")
        print("Try running again; transient failures are common.")
    else:
        print("\nAll done! Restart the Stellar Insight app to serve the new images.")


if __name__ == "__main__":
    main()
