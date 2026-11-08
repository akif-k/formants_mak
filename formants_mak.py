# -*- coding: utf-8 -*-
"""
formants_mak.py -- real-time F1-F5 formant frequency and bandwidth
visualization (Tkinter + sounddevice + matplotlib).

Audio pipeline: the sounddevice callback only pushes raw mono blocks onto a
queue (kept cheap so it never blocks the realtime audio thread); a separate
daemon thread rolls those blocks into a persistent ~0.5s buffer and runs the
YIN F0 gate (see YinPitchDetector) + Praat Burg formant analysis on that
buffer. This matters because Praat's default Burg
analysis window (25ms) is longer than a single ~512-sample callback block
(~11.6ms at 44100 Hz) -- analyzing the raw block directly silently returns all-zero formants.

Language support (2026-09-12): the phoneme inventory + expected formant
targets ("Target Sounds") come from a LANGUAGE -- built-in Turkish and
English presets, or an external JSON file (e.g. an edited turkish.json).
"Save as JSON" writes the current language to <language>.json next to the
app; "External JSON..." loads any such file and makes it the source of
truth (edits in the Target Sounds dialog are written back to it). See
LANGUAGE_PRESETS / load_language_file / save_language_file.

Open-source edition: free to run from source, no license gate (the
commercial Sonus et Vox modules rtime_mak, resonance_mak and
harmonicity_mak are sold separately via Lemon Squeezy).
Follows the Sonus et Vox module template:
title format, screen fit, Figure (not pyplot), clean close + os._exit,
stdout fix, --selftest.
"""
import os
import sys
import subprocess
import re

# ---------------- SELF-TEST (`python formants_mak.py --selftest`) ----------------
# Template block (see the other modules): imports/exercises every native
# dependency and exits 0/1 with a report. Harmless when running from source.
if "--selftest" in sys.argv:
    _out = os.environ.get("FORMANTS_MAK_SELFTEST_OUT") or os.path.join(os.path.dirname(sys.executable), "selftest_result.txt")
    try:
        import numpy as _np
        import parselmouth as _pm
        import sounddevice as _sd
        import tkinter as _tk
        import matplotlib as _mpl
        _mpl.use("TkAgg")
        from matplotlib.figure import Figure as _Fig
        from matplotlib.backends.backend_agg import FigureCanvasAgg as _Agg
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as _TkAgg  # noqa: F401
        import PIL.Image as _PImage  # noqa: F401
        import PIL.ImageTk as _PImageTk  # noqa: F401
        import openpyxl as _ox  # noqa: F401  (New Language form)
        import unicodedata as _ud  # noqa: F401
        _f = _Fig(figsize=(3, 2)); _ax = _f.add_subplot(111); _ax.plot([0, 1], [0, 1]); _Agg(_f).draw()
        _t = _np.arange(22050) / 44100.0
        _snd = _pm.Sound(0.3 * _np.sin(2 * _np.pi * 150 * _t), sampling_frequency=44100)
        _fm = _snd.to_formant_burg(max_number_of_formants=5.5, maximum_formant=5500.0, time_step=0.01, window_length=0.025)
        _msg = (f"OK numpy={_np.__version__} matplotlib={_mpl.__version__} parselmouth={_pm.__version__} "
                f"sounddevice={_sd.__version__} devices={len(_sd.query_devices())} formant_frames={_fm.get_number_of_frames()}\n")
        _code = 0
    except BaseException:
        import traceback
        _msg = "FAIL\n" + traceback.format_exc()
        _code = 1
    try:
        with open(_out, "w", encoding="utf-8") as _fh:
            _fh.write(_msg)
    except Exception:
        pass
    sys.exit(_code)
# ------------------------------------------------------------------------------

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, colorchooser
import sounddevice as sd
import numpy as np
import parselmouth
import json
import time
import csv
import queue
import threading
from datetime import datetime
import matplotlib
matplotlib.use("TkAgg")
# Figure() instead of pyplot (template): no hidden second Tk root / atexit hook.
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

TITLE = "formants_mak — Real-Time Formants and Bandwidths Graphics for Visual Biofeedback (by Sonus et Vox)"
CONFIG_FILE = "formants_mak.json"
IS_COMPILED = getattr(sys, "frozen", False)

# ---------------- WINDOWED-EXE STDOUT/STDERR FIX (template) ----------------
if IS_COMPILED and sys.stdout is None:
    _log_path = os.path.join(os.path.dirname(sys.executable), "formants_mak_debug.log")
    try:
        _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        _log_file = io.StringIO()
    sys.stdout = _log_file
    sys.stderr = _log_file
# ---------------------------------------------------------------------------

NUM_FORMANTS = 5
ANALYSIS_BUFFER_SECONDS = 0.5

TRACE_KEYS = ['F1', 'F2', 'F3', 'F4', 'F5', 'B1', 'B2', 'B3', 'B4', 'B5']
DEFAULT_TRACE_COLORS = {
    'F1': '#1f77b4', 'F2': '#ff7f0e', 'F3': '#2ca02c', 'F4': '#d62728', 'F5': '#9467bd',
    'B1': '#1f77b4', 'B2': '#ff7f0e', 'B3': '#2ca02c', 'B4': '#d62728', 'B5': '#9467bd',
}
DEFAULT_TRACE_WIDTHS = {
    'F1': 2.5, 'F2': 2.5, 'F3': 2.5, 'F4': 2.5, 'F5': 2.5,
    'B1': 4.0, 'B2': 4.0, 'B3': 4.0, 'B4': 4.0, 'B5': 4.0,
}

FORMANT_RANGE_PRESETS = {"0-5000 Hz": (0, 5000), "0-5500 Hz": (0, 5500)}
FORMANT_RANGE_CUSTOM = "User Specified"
F0_RANGE_PRESETS = {"Normal": (75, 600), "High": (200, 1200)}
F0_RANGE_CUSTOM = "Custom"
BANDWIDTH_LIMIT_PRESETS = {"150 Hz": 150, "300 Hz": 300, "600 Hz": 600}
BANDWIDTH_LIMIT_CUSTOM = "User Specified"

FORMANT_BAND_FRACTION = 0.55  # expected value is the center of a +/-55% band -- default/fallback only;
                               # the live value is user-configurable (see FIDELITY_PRESETS + "Fidelity" in
                               # the Target Sounds dialog) and passed explicitly on every call, since a
                               # Python default-argument value is bound once at def time and won't track
                               # a later change to this module constant.

# "Fidelity" = how strictly a raw formant candidate must match its expected
# target to be accepted, expressed as the band_fraction passed to
# _reassign_by_expected(). Strict = trust the entered expected values more
# (narrower band, more readings rejected as implausible); Loose = accept
# more deviation from the expected values (wider band, fewer readings
# rejected, but more room for a wrong match to slip through).
FIDELITY_PRESETS = {"Strict": 0.30, "Normal": 0.45, "Loose": 0.60}  # compiled-in defaults;
                                                                      # the live values are in config's
                                                                      # "fidelity_presets" (editable in
                                                                      # formants_mak.json).
FIDELITY_CUSTOM = "Custom"

PHONEME_CATEGORIES = ["Vowels", "Semivowels", "Liquids", "Nasals"]   # "Others" removed 2026-09-12
# Vowels 12 -> 8 (2026-09-15, user): with 12 the Target Sounds bar grows so wide
# that the Edit / New Language buttons get squeezed; English was trimmed to 8.
MAX_PHONEMES_PER_CATEGORY = {"Vowels": 8, "Semivowels": 4, "Liquids": 4, "Nasals": 4}

# ---------------- LANGUAGES ----------------
# A "language" = the phoneme inventory shown in the Target Sounds bar plus
# each symbol's expected F1-F5 (Hz) -- approximate adult values, starting
# points for the expected-formant band matching (_reassign_by_expected),
# not authoritative targets. Two built-in presets (embedded in the code, so
# also inside the exe; listed as plain "Turkish" / "English"); every language
# .json next to the app is listed as '<its "language" name> (json)';
# Target Sounds -> "New Language" creates <name in lower case>.json from the
# filled new_language.xlsx form (see NEW LANGUAGE FORM below);
# "External JSON..." loads a file from anywhere. JSON schema:
#   {"language": "Turkish", "phonemes": {category: [symbols]},
#    "phoneme_formants": {symbol: [F1, F2, F3, F4, F5]}}
LANGUAGE_PRESETS = {
    "Turkish": {
        "phonemes": {
            "Vowels": ["a", "e", "ɯ", "i", "o", "œ", "u", "y"],
            "Semivowels": ["ɰ", "j"],
            "Liquids": ["r", "ɹ", "ɫ", "l"],
            "Nasals": ["m", "n", "ŋ"],
        },
        "phoneme_formants": {
            "a": [750, 1300, 2600, 3300, 4200],
            "e": [500, 2100, 2700, 3400, 4300],
            "ɯ": [450, 1200, 2500, 3300, 4200],
            "i": [300, 2300, 3000, 3700, 4500],
            "o": [500, 900, 2500, 3300, 4200],
            "œ": [500, 1600, 2300, 3300, 4200],
            "u": [350, 800, 2500, 3300, 4200],
            "y": [300, 1800, 2500, 3400, 4300],
            "ɰ": [300, 1900, 2500, 3400, 4300],
            "j": [280, 2200, 3000, 3700, 4500],
            "r": [500, 1300, 1700, 2800, 4000],
            "ɹ": [400, 1100, 1600, 2600, 3800],
            "ɫ": [400, 900, 2200, 3300, 4200],
            "l": [360, 1300, 2600, 3300, 4200],
            "m": [280, 1000, 2200, 3300, 4200],
            "n": [280, 1500, 2500, 3300, 4200],
            "ŋ": [280, 1200, 2400, 3300, 4200],
        },
    },
    # American English: F1-F3 adult-male means after Hillenbrand et al.
    # (1995) / Peterson & Barney (1952), rounded; F4-F5 rough estimates.
    # 2026-09-15: trimmed from 12 to the 8 monophthongs (vowel limit 8);
    # dropped e (eɪ) and o (oʊ) -- diphthongal in English -- and the two
    # dialect-dependent/rare ones ɔ (cot-caught merger) and ɝ (r-coloured).
    # Add them back per language file if needed (values kept below).
    "English": {
        "phonemes": {
            "Vowels": ["i", "ɪ", "ɛ", "æ", "ɑ", "ʌ", "ʊ", "u"],
            "Semivowels": ["w", "j"],
            "Liquids": ["ɹ", "l"],
            "Nasals": ["m", "n", "ŋ"],
        },
        "phoneme_formants": {
            "i": [340, 2320, 3000, 3700, 4500],
            "ɪ": [430, 2030, 2680, 3500, 4400],
            "ɛ": [580, 1800, 2600, 3400, 4300],
            "æ": [590, 1950, 2600, 3400, 4300],
            "ɑ": [770, 1330, 2520, 3300, 4200],
            "ʌ": [620, 1200, 2550, 3300, 4200],
            "ʊ": [470, 1120, 2430, 3300, 4200],
            "u": [380, 1000, 2340, 3300, 4200],
            # not in the bar (limit 8); reference values: e (eɪ) 480/2090/2690/3500/4400,
            # o (oʊ) 500/910/2460/3300/4200, ɔ 650/1000/2540/3300/4200, ɝ 470/1380/1710/3300/4200
            "w": [300, 610, 2200, 3300, 4200],
            "j": [280, 2200, 3000, 3700, 4500],
            "ɹ": [400, 1100, 1600, 2600, 3800],
            "l": [360, 1300, 2600, 3300, 4200],
            "m": [280, 1000, 2200, 3300, 4200],
            "n": [280, 1500, 2500, 3300, 4200],
            "ŋ": [280, 1200, 2400, 3300, 4200],
        },
    },
}
LANGUAGE_DEFAULT = "Turkish"
LANGUAGE_EXTERNAL = "External JSON..."   # combobox entry: pick any language .json file
# (The former "New language..." combobox entry was removed 2026-09-15: adding
# a language now happens only via Target Sounds -> New Language, form-based.)

# Backward-compat aliases (older code/config referred to these names)
DEFAULT_PHONEMES = LANGUAGE_PRESETS[LANGUAGE_DEFAULT]["phonemes"]
DEFAULT_PHONEME_FORMANTS = LANGUAGE_PRESETS[LANGUAGE_DEFAULT]["phoneme_formants"]


def app_dir():
    """Folder of the app (exe folder when frozen) -- language files live here."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def language_file_name(language):
    """File name for a language: the displayed name in lower case
    (turkish.json, english.json, 'brazilian portuguese.json'); characters
    Windows does not allow in file names are replaced by '_'."""
    stem = re.sub(r'[\\/:*?"<>|]+', "_", language.strip().lower())
    return f"{stem}.json"


def language_file_path(language):
    return os.path.join(app_dir(), language_file_name(language))


def builtin_shadow_file(path, name):
    """True if `path` is <built-in name>.json next to the app (turkish.json,
    english.json). Such a file takes priority over the embedded preset
    (2026-09-15, user decision): the built-in entry loads it when it exists
    and the embedded values only when it does not, so the user can edit
    Turkish/English freely while the code stays read-only."""
    if name not in LANGUAGE_PRESETS:
        return False
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(language_file_path(name)))


def builtin_label(name):
    """Combobox label of a built-in language: '<name> (json)' while its file
    exists next to the app (values come from the file), plain '<name>' otherwise."""
    return f"{name} (json)" if os.path.exists(language_file_path(name)) else name


def scan_language_files():
    """Language JSON files next to the app -> ordered {display_name: path}.
    The display name is the file's "language" field (file stem if missing);
    Every file-based language is shown as '<name> (json)' -- the built-in
    presets (embedded in the code, also inside the exe) appear as plain
    'Turkish' / 'English'."""
    found = {}
    try:
        names = sorted(os.listdir(app_dir()), key=str.lower)
    except OSError:
        return found
    for fn in names:
        if not fn.lower().endswith(".json") or fn == CONFIG_FILE:
            continue
        path = os.path.join(app_dir(), fn)
        try:
            name, _p, _f = load_language_file(path)
        except Exception:
            continue
        if builtin_shadow_file(path, name):
            continue   # turkish.json / english.json: reached through the built-in entry itself
        display = f"{name} (json)"
        while display in found:
            display = f"{name} (json: {fn})"
        found[display] = path
    return found


def load_language_file(path):
    """Read a language JSON (see schema above). Returns
    (name, phonemes, phoneme_formants); raises ValueError on a bad file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "phonemes" not in data or "phoneme_formants" not in data:
        raise ValueError("expected keys 'phonemes' and 'phoneme_formants'")
    phon = {cat: [str(x) for x in (data["phonemes"].get(cat) or [])] for cat in PHONEME_CATEGORIES}
    forms = {}
    for sym, vals in data["phoneme_formants"].items():
        vals = [int(round(float(v))) for v in list(vals)[:NUM_FORMANTS]]
        vals += [0] * (NUM_FORMANTS - len(vals))
        forms[str(sym)] = vals
    name = str(data.get("language") or os.path.splitext(os.path.basename(path))[0])
    return name, phon, forms


def save_language_file(path, language, phonemes, phoneme_formants):
    data = {
        "language": language,
        "_note": "formants_mak language file: phoneme inventory per category and expected F1-F5 (Hz) "
                 "per symbol. Edit freely, then load it with Language -> External JSON...",
        "phonemes": {cat: list(phonemes.get(cat, [])) for cat in PHONEME_CATEGORIES},
        "phoneme_formants": {sym: [int(v) for v in vals] for sym, vals in phoneme_formants.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------- NEW LANGUAGE FORM (xlsx) ----------------
# The only way to add a language (2026-09-15, user decision: one place, not
# two): Target Sounds -> "New Language" -> "Create Blank Form" writes
# new_language.xlsx next to the app (never overwrites an existing one), the
# user fills it in Excel/LibreOffice, "Read Form" turns it into
# <name lower case>.json (same schema as above), which is then selected.
# F1 and F2 are required per sound; empty F3-F5 cells get the mean of that
# category over the built-in presets (category_mean_formants).
NEW_LANGUAGE_FORM = "new_language.xlsx"
FORM_LANGUAGE_LABEL = "Language name"
FORM_HEADER = ["No", "IPA", "F1 (required)", "F2 (required)", "F3", "F4", "F5"]
FORM_IPA_COL = 1   # 0-based index of "IPA" in FORM_HEADER; F1..F5 follow it


def new_language_form_path():
    return os.path.join(app_dir(), NEW_LANGUAGE_FORM)


def category_mean_formants(cat):
    """Mean F1-F5 (Hz, rounded to 10) of every built-in sound in `cat`;
    used to fill F3-F5 left empty in the form."""
    sums = [0.0] * NUM_FORMANTS
    n = 0
    for preset in LANGUAGE_PRESETS.values():
        for sym in preset["phonemes"].get(cat, []):
            vals = preset["phoneme_formants"].get(sym)
            if not vals:
                continue
            for i in range(NUM_FORMANTS):
                sums[i] += vals[i]
            n += 1
    if n == 0:
        return [500, 1500, 2500, 3300, 4200]
    return [int(round(s / n / 10.0) * 10) for s in sums]


def create_language_form(path):
    """Write the blank new-language form. Raises FileExistsError if `path`
    exists (the user may have filled it already) and ImportError without
    openpyxl."""
    if os.path.exists(path):
        raise FileExistsError(path)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "new language"
    bold = Font(name="Arial", bold=True)
    normal = Font(name="Arial")
    dim = Font(name="Arial", color="808080")
    note_font = Font(name="Arial", italic=True, color="555555")
    input_fill = PatternFill("solid", fgColor="FFF9DB")   # light yellow = type here
    head_fill = PatternFill("solid", fgColor="E7E6E6")
    thin = Side(style="thin", color="999999")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    ncols = len(FORM_HEADER)

    ws["A1"] = FORM_LANGUAGE_LABEL
    ws["A1"].font = bold
    ws["B1"].fill = input_fill
    ws["B1"].font = normal
    ws["B1"].border = grid
    ws["A2"] = ("Type the IPA symbol of each sound and its F1 and F2 (Hz); a row is added to the "
                "language only if IPA, F1 and F2 are all filled in. F3-F5 are optional: an empty cell "
                "gets a typical value for that category. Leave unused rows empty. "
                "Save, then in formants_mak use New Language -> Read Form.")
    ws["A2"].font = note_font
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    ws.row_dimensions[2].height = 48

    row = 4
    for cat in PHONEME_CATEGORIES:
        limit = MAX_PHONEMES_PER_CATEGORY[cat]
        ws.cell(row=row, column=1, value=f"{cat} (max {limit})").font = bold
        row += 1
        for col, head in enumerate(FORM_HEADER, start=1):
            c = ws.cell(row=row, column=col, value=head)
            c.font = bold
            c.fill = head_fill
            c.border = grid
            c.alignment = center
        row += 1
        for i in range(limit):
            for col in range(1, ncols + 1):
                c = ws.cell(row=row, column=col)
                c.border = grid
                c.alignment = center
                if col == 1:                     # "No" column: numbered, not for typing
                    c.value = i + 1
                    c.font = dim
                else:
                    c.fill = input_fill
                    c.font = normal
            row += 1
        row += 1   # blank line between categories

    ws.column_dimensions["A"].width = 17          # also holds the category titles
    ws.column_dimensions["B"].width = 12
    for col in range(3, ncols + 1):
        ws.column_dimensions[get_column_letter(col)].width = 14
    ws.freeze_panes = "A3"
    wb.save(path)


def _form_number(value):
    """Cell -> float or None (empty). Accepts numbers and numeric strings
    (comma decimal too); raises ValueError on other text."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    return float(s)


def read_language_form(path):
    """Parse the filled form. Only rows with IPA, F1 and F2 all present (and
    numeric, positive) are taken; every other non-empty row is skipped and
    listed in `notes` (so are duplicates, category overflow and the F3-F5
    values filled in from the category means). Returns (name, phonemes,
    phoneme_formants, notes); raises ValueError only when the language name
    is empty or no usable row exists. The columns are located from the
    header row that contains "IPA" (F1..F5 are the cells right of it), so a
    form without the "No" column reads the same way."""
    import unicodedata
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb.active
    name = ""
    phon = {cat: [] for cat in PHONEME_CATEGORIES}
    forms = {}
    skipped = []
    filled = []
    notes = []
    current_cat = None
    ipa_col = FORM_IPA_COL
    cat_by_label = {cat.lower(): cat for cat in PHONEME_CATEGORIES}
    ipa_label = FORM_HEADER[FORM_IPA_COL].lower()

    def _txt(cell):
        v = cell.value
        return str(v).strip() if v is not None else ""

    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        r = row[0].row
        a_txt = _txt(row[0])
        if a_txt.lower() == FORM_LANGUAGE_LABEL.lower():
            name = _txt(row[1]) if len(row) > 1 else ""
            continue
        head = a_txt.split("(")[0].strip().lower()
        if head in cat_by_label:
            current_cat = cat_by_label[head]
            continue
        labels = [_txt(c).lower() for c in row]
        if ipa_label in labels:                  # column header row -> learn the layout
            ipa_col = labels.index(ipa_label)
            continue
        if current_cat is None:
            continue
        cells = list(row) + [None] * (ipa_col + 1 + NUM_FORMANTS - len(row))
        sym = _txt(cells[ipa_col]) if cells[ipa_col] is not None else ""
        raw = [c.value if c is not None else None for c in cells[ipa_col + 1: ipa_col + 1 + NUM_FORMANTS]]
        row_used = bool(sym) or any(v is not None and str(v).strip() != "" for v in raw)
        if not row_used:
            continue                             # empty row (only the "No" cell) -> nothing to add
        sym = unicodedata.normalize("NFC", sym)
        label = sym if sym else "(no IPA)"
        try:
            vals = [_form_number(v) for v in raw]
        except ValueError:
            skipped.append(f"row {r} {label}: F1-F5 must be numbers")
            continue
        if not sym or vals[0] is None or vals[1] is None:
            skipped.append(f"row {r} {label}: IPA, F1 and F2 are all required")
            continue
        if any(v is not None and v <= 0 for v in vals):
            skipped.append(f"row {r} {label}: formant values must be positive")
            continue
        if sym in forms:
            skipped.append(f"row {r} {label}: duplicate symbol, first occurrence kept")
            continue
        if len(phon[current_cat]) >= MAX_PHONEMES_PER_CATEGORY[current_cat]:
            skipped.append(f"row {r} {label}: {current_cat} limit {MAX_PHONEMES_PER_CATEGORY[current_cat]} reached")
            continue
        means = category_mean_formants(current_cat)
        out = []
        missing = []
        for i in range(NUM_FORMANTS):
            if vals[i] is None:
                out.append(means[i])
                missing.append(f"F{i + 1}")
            else:
                out.append(int(round(vals[i])))
        if missing:
            filled.append(f"{sym}: {', '.join(missing)}")
        phon[current_cat].append(sym)
        forms[sym] = out

    if not name:
        raise ValueError(f"The \"{FORM_LANGUAGE_LABEL}\" cell (B1) is empty.")
    if not forms:
        raise ValueError("No usable row: fill in IPA, F1 and F2 for at least one sound."
                         + ("\n\nSkipped:\n" + "\n".join(skipped) if skipped else ""))
    if skipped:
        notes.append("Skipped (not added):\n  " + "\n  ".join(skipped))
    if filled:
        notes.append("Typical category values used for: " + "; ".join(filled))
    return name, phon, forms, notes


def _infer_range_preset(presets, custom_label, lo, hi):
    for name, (plo, phi) in presets.items():
        if lo == plo and hi == phi:
            return name
    return custom_label


def _infer_single_preset(presets, custom_label, value):
    for name, v in presets.items():
        if value == v:
            return name
    return custom_label

default_config = {
    "f0_min": 75,
    "f0_max": 600,
    "update_interval": 100,
    "plot_duration": 5.0,
    "rms_threshold_db": -50.0,
    "font_main": "Arial",
    "font_size_label": 12,
    "font_size_button": 14,
    "font_size_title": 14,
    "window_width": 1600,
    "window_height": 800,
    "input_device_index": None,   # None = system default input (same keys as rtime/resonance/harmonicity)
    "hostapi_index": None,        # None = default host API / driver
    "samplerate": 44100,
    "formant_ymin": 0,
    "formant_ymax": 5500,
    "formant_alpha": 1.0,
    "formant_markersize": 4,
    "bandwidth_alpha": 0.6,
    "bandwidth_markersize": 6,
    "bandwidth_ymax": 300,
    "trace_colors": dict(DEFAULT_TRACE_COLORS),
    "trace_widths": dict(DEFAULT_TRACE_WIDTHS),
    "expected_formants": [500, 1500, 2500, 3500, 4500],
    "formant_band_fraction": FORMANT_BAND_FRACTION,
    "fidelity_presets": dict(FIDELITY_PRESETS),
    # Language = phoneme inventory + expected formants (see LANGUAGES above).
    # "language": a built-in preset name or "external"; "language_file": the
    # JSON path when external. (Older configs carried "phonemes"/
    # "phoneme_formants" directly -- those keys are now ignored.)
    "language": LANGUAGE_DEFAULT,
    "language_file": "",
}


def _resource_path(filename):
    """Absolute path to a bundled resource -- works both running from
    source and from a PyInstaller onefile exe (sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)


def load_config():
    cfg = default_config.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    for stale in ("audio_device", "audio_driver", "phonemes", "phoneme_formants"):
        cfg.pop(stale, None)
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_audio_devices():
    """Input devices as [{'index','name','hostapi','is_default'}], default index
    (None when PortAudio reports no default input). Same as the other modules."""
    try:
        devices = sd.query_devices()
        default_input = sd.default.device[0]
    except Exception:
        return [], None
    if default_input is None or default_input < 0:
        default_input = None
    input_devices = []
    for i, dev in enumerate(devices):
        if dev.get('max_input_channels', 0) > 0:
            input_devices.append({'index': i, 'name': dev['name'], 'hostapi': dev['hostapi'],
                                  'is_default': (i == default_input)})
    return input_devices, default_input


def get_hostapis():
    """Host APIs / drivers as [{'index','name','is_default'}], default index."""
    try:
        apis = sd.query_hostapis()
        default_api = sd.default.hostapi
    except Exception:
        return [], None
    return [{'index': i, 'name': api['name'], 'is_default': (i == default_api)}
            for i, api in enumerate(apis)], default_api


def _reassign_by_expected(raw_values, raw_bandwidths, expected, band_fraction=FORMANT_BAND_FRACTION):
    """Re-slot raw Praat formant candidates (already frequency-ordered by
    Praat, but prone to mis-assignment -- a spurious low peak can bump every
    later formant up a slot) by nearest-distance matching against the
    user-configured 'expected' targets, instead of trusting Praat's own
    slot order blindly. Both sequences are monotonically increasing, so this
    is a no-op for a normal reading and only kicks in to fix a crossover.

    'expected' is treated as the CENTER of a plausibility band
    (+/-band_fraction), not an exact target -- a candidate outside a slot's
    band is never assigned to it, even if it's the closest one available.
    Without this, a slot with no genuine formant nearby (e.g. F0 bleeding
    through where F1 should be) would still get force-matched to whatever
    raw value happened to be least-bad, which is how F0/F1 confusion was
    slipping through the plain nearest-match version of this function."""
    n = len(expected)
    valid_idx = [i for i, v in enumerate(raw_values) if v > 0]
    candidates = sorted(
        (abs(raw_values[i] - expected[slot]), slot, i)
        for slot in range(n) for i in valid_idx
        if abs(raw_values[i] - expected[slot]) <= band_fraction * expected[slot]
    )
    result_f = [0.0] * n
    result_b = [0.0] * n
    used_slots, used_raw = set(), set()
    for _dist, slot, i in candidates:
        if slot in used_slots or i in used_raw:
            continue
        result_f[slot] = raw_values[i]
        result_b[slot] = raw_bandwidths[i] if i < len(raw_bandwidths) else 0.0
        used_slots.add(slot)
        used_raw.add(i)
        if len(used_slots) == n:
            break
    return result_f, result_b


def get_formants_praat(signal, sr=44100, num_formants=NUM_FORMANTS, expected=None, f0_hz=0.0,
                        band_fraction=FORMANT_BAND_FRACTION):
    """Burg formant analysis on a signal buffer that's long enough to hold
    Praat's analysis window (default 25ms) -- see module docstring.

    expected: optional list of target Hz values (one per formant slot) used
    to correct mis-assigned candidates via _reassign_by_expected() -- see
    the "Target Sounds" dialog.
    f0_hz: current pitch estimate (if any); a raw candidate that's really
    just F0 bleeding through as a spurious low-frequency LPC root (the most
    common cause of F0/F1 confusion) is discarded before reassignment.
    band_fraction: "Fidelity" -- how far (as a fraction of the expected
    value) a candidate may drift from its expected slot and still be
    accepted; see FIDELITY_PRESETS."""
    try:
        sound = parselmouth.Sound(signal, sampling_frequency=sr)
        formants = sound.to_formant_burg(max_number_of_formants=5.5, maximum_formant=5500.0,
                                          time_step=0.01, window_length=0.025)

        mid_time = sound.get_total_duration() / 2
        formant_values = [formants.get_value_at_time(i + 1, mid_time) for i in range(num_formants)]
        bandwidth_values = [formants.get_bandwidth_at_time(i + 1, mid_time) for i in range(num_formants)]

        formant_values = [f if not np.isnan(f) else 0.0 for f in formant_values]
        bandwidth_values = [b if not np.isnan(b) else 0.0 for b in bandwidth_values]

        if f0_hz and f0_hz > 0:
            tolerance = max(30.0, 0.15 * f0_hz)
            for i, v in enumerate(formant_values):
                if v > 0 and abs(v - f0_hz) < tolerance:
                    formant_values[i] = 0.0
                    bandwidth_values[i] = 0.0

        if expected:
            formant_values, bandwidth_values = _reassign_by_expected(
                formant_values, bandwidth_values, expected, band_fraction)

        return formant_values, bandwidth_values
    except Exception:
        return [0.0] * num_formants, [0.0] * num_formants


class YinPitchDetector:
    """Realtime F0 estimator using the YIN algorithm (de Cheveigne & Kawahara,
    2002) on an internally-held sliding window -- a self-contained numpy
    replacement for aubio.pitch("default", buf_size, hop_size, samplerate).
    Call with a hop_size-length block each time; keeps the same buf_size/
    hop_size windowing and the same callable/set_unit/set_silence interface
    as the aubio object it replaces, so callers don't need to change."""

    def __init__(self, buf_size, hop_size, samplerate, silence_db=-40.0, threshold=0.15):
        self.buf_size = buf_size
        self.hop_size = hop_size
        self.samplerate = samplerate
        self.silence_db = silence_db
        self.threshold = threshold
        # d(tau) needs buf_size - tau samples to stay meaningful; capping tau
        # this way also bounds the per-call cost to O(buf_size * tau_max).
        self.tau_max = min(buf_size - 1, int(samplerate / 40))
        self._buffer = np.zeros(buf_size, dtype=np.float32)

    def set_unit(self, unit):
        pass  # this implementation only ever produces Hz

    def set_silence(self, db):
        self.silence_db = db

    def __call__(self, block):
        block = np.asarray(block, dtype=np.float32)
        self._buffer = np.roll(self._buffer, -len(block))
        self._buffer[-len(block):] = block

        rms = np.sqrt(np.mean(self._buffer ** 2))
        if rms <= 0 or 20 * np.log10(rms + 1e-12) < self.silence_db:
            return [0.0]

        tau = self._estimate_tau(self._buffer)
        if tau is None or tau <= 0:
            return [0.0]
        return [self.samplerate / tau]

    def _estimate_tau(self, x):
        tau_max = self.tau_max

        diff = np.empty(tau_max + 1)
        diff[0] = 0.0
        for tau in range(1, tau_max + 1):
            d = x[:-tau] - x[tau:]
            diff[tau] = np.dot(d, d)

        cmnd = np.ones(tau_max + 1)
        running_sum = 0.0
        for tau in range(1, tau_max + 1):
            running_sum += diff[tau]
            cmnd[tau] = diff[tau] * tau / running_sum if running_sum > 0 else 1.0

        tau_estimate = None
        for tau in range(2, tau_max):
            if cmnd[tau] < self.threshold:
                while tau + 1 < tau_max and cmnd[tau + 1] < cmnd[tau]:
                    tau += 1
                tau_estimate = tau
                break
        if tau_estimate is None:
            tau_estimate = int(np.argmin(cmnd[2:])) + 2

        return self._parabolic_interpolation(cmnd, tau_estimate)

    @staticmethod
    def _parabolic_interpolation(cmnd, tau):
        x0 = tau - 1 if tau > 0 else tau
        x2 = tau + 1 if tau + 1 < len(cmnd) else tau
        if x0 == tau or x2 == tau:
            return float(tau)
        s0, s1, s2 = cmnd[x0], cmnd[tau], cmnd[x2]
        denom = s0 - 2 * s1 + s2
        if denom == 0:
            return float(tau)
        return tau + (s0 - s2) / (2 * denom)


def formants_ui(config):
    f0_min = config["f0_min"]
    f0_max = config["f0_max"]
    update_interval = config["update_interval"]
    plot_duration = config["plot_duration"]
    rms_threshold_db = config["rms_threshold_db"]
    formant_ymin = config["formant_ymin"]
    formant_ymax = config["formant_ymax"]
    samplerate = config.get("samplerate", 44100)
    # Audio device / driver: stored as PortAudio indices (None = default),
    # validated against the current device list at start-up.
    audio_devices, default_input_device = get_audio_devices()
    hostapis, default_hostapi = get_hostapis()
    input_device_index = config.get("input_device_index")
    if input_device_index is None or not any(d['index'] == input_device_index for d in audio_devices):
        input_device_index = default_input_device
    hostapi_index = config.get("hostapi_index")
    if hostapi_index is None or not any(a['index'] == hostapi_index for a in hostapis):
        hostapi_index = default_hostapi
    audio_dirty = [False]   # device/driver/rate changed while running -> reopen stream on next Start
    trace_colors = dict(DEFAULT_TRACE_COLORS)
    trace_colors.update(config.get("trace_colors", {}))
    trace_widths = dict(DEFAULT_TRACE_WIDTHS)
    trace_widths.update(config.get("trace_widths", {}))
    expected_formants = list(config.get("expected_formants", [500, 1500, 2500, 3500, 4500]))
    formant_band_fraction = float(config.get("formant_band_fraction", FORMANT_BAND_FRACTION))
    fidelity_presets = dict(FIDELITY_PRESETS)
    fidelity_presets.update(config.get("fidelity_presets", {}))

    # --- language (phoneme inventory + expected formants) ---
    phonemes = {cat: [] for cat in PHONEME_CATEGORIES}
    phoneme_formants = {}
    language_state = {"name": LANGUAGE_DEFAULT, "external_path": "", "display": LANGUAGE_DEFAULT}

    def _set_language_data(phon, forms):
        phonemes.clear()
        phonemes.update({cat: list(phon.get(cat, [])) for cat in PHONEME_CATEGORIES})
        phoneme_formants.clear()
        phoneme_formants.update({k: list(v) for k, v in forms.items()})

    def _load_builtin_language(name):
        """Built-in entry: <name>.json next to the app takes priority; the
        embedded preset is used only when the file is absent or unreadable."""
        path = language_file_path(name)
        if os.path.exists(path):
            try:
                _load_external_language(path)
                language_state["builtin"] = name
                return
            except Exception as e:
                print(f"[LANGUAGE] {os.path.basename(path)} could not be read ({e}) -- using the embedded {name}")
        preset = LANGUAGE_PRESETS[name]
        _set_language_data(preset["phonemes"], preset["phoneme_formants"])
        language_state.update({"name": name, "external_path": "", "display": name, "builtin": name})

    user_languages = {}   # display name -> path, refreshed by _rescan_user_languages()

    def _rescan_user_languages():
        user_languages.clear()
        user_languages.update(scan_language_files())

    def _display_for_path(path, name=None):
        """List label of a file-based language: '<name> (json)' (the entry of
        the scan when the file lives next to the app)."""
        _rescan_user_languages()
        for display, p in user_languages.items():
            if os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(path)):
                return display
        if name is None:
            try:
                name = load_language_file(path)[0]
            except Exception:
                name = os.path.splitext(os.path.basename(path))[0]
        display = f"{name} (json)"
        if display in user_languages:          # a different file with the same name
            display = f"{name} (json: {os.path.basename(path)})"
        return display

    def _file_mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _load_external_language(path):
        name, phon, forms = load_language_file(path)
        _set_language_data(phon, forms)
        language_state["mtime"] = _file_mtime(path)
        language_state.update({"name": name, "external_path": path,
                               "display": _display_for_path(path, name),
                               "builtin": name if builtin_shadow_file(path, name) else None})

    _cfg_lang = config.get("language", LANGUAGE_DEFAULT)
    if _cfg_lang == "external" and config.get("language_file"):
        try:
            _load_external_language(config["language_file"])
        except Exception as e:
            print(f"[LANGUAGE] could not load {config.get('language_file')}: {e} -- falling back to {LANGUAGE_DEFAULT}")
            _load_builtin_language(LANGUAGE_DEFAULT)
    elif _cfg_lang in LANGUAGE_PRESETS:
        _load_builtin_language(_cfg_lang)
    else:
        _load_builtin_language(LANGUAGE_DEFAULT)
    phoneme_vars = {}
    active_phoneme_symbol = [None]
    editor_open = [False]     # Edit dialog open -> file watcher pauses
    lang_watch_id = None

    formant_data = [[] for _ in range(NUM_FORMANTS)]
    bandwidth_data = [[] for _ in range(NUM_FORMANTS)]
    f0_data = []
    time_values = []
    start_time = time.time()

    stream = None
    after_id = None
    is_plotting = False
    audio_queue = queue.Queue()
    analysis_buffer = np.zeros(int(ANALYSIS_BUFFER_SECONDS * samplerate), dtype=np.float32)

    pitch_o = YinPitchDetector(1024, 512, samplerate)
    pitch_o.set_unit("Hz")
    pitch_o.set_silence(-40)

    def audio_callback(indata, frames, time_info, status):
        if not is_plotting:
            return
        samples = np.mean(indata, axis=1).astype(np.float32)
        audio_queue.put(samples)

    def process_audio():
        """Runs in a daemon thread (one per Start click, exits on Stop) --
        rolls incoming blocks into analysis_buffer and runs the F0 gate +
        Praat formant analysis off the realtime audio thread."""
        nonlocal analysis_buffer, start_time
        while is_plotting:
            try:
                block = audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            analysis_buffer = np.roll(analysis_buffer, -len(block))
            analysis_buffer[-len(block):] = block

            rms = np.sqrt(np.mean(block ** 2))
            rms_db = 20 * np.log10(rms + 1e-12)

            formants = [0.0] * NUM_FORMANTS
            bandwidths = [0.0] * NUM_FORMANTS
            f0_value = 0.0
            if rms_db > rms_threshold_db:
                pitch = pitch_o(block)[0]
                if f0_min < pitch < f0_max:
                    f0_value = pitch
                    formants, bandwidths = get_formants_praat(analysis_buffer, sr=samplerate, num_formants=NUM_FORMANTS,
                                                               expected=expected_formants, f0_hz=pitch,
                                                               band_fraction=formant_band_fraction)

            time_values.append(time.time() - start_time)
            f0_data.append(f0_value)
            for i in range(NUM_FORMANTS):
                formant_data[i].append(formants[i])
                bandwidth_data[i].append(bandwidths[i])

            # Retain a bit more than what's currently displayed
            # (plot_duration) rather than a fixed point count -- a fixed
            # count doesn't scale with samplerate/blocksize, which is what
            # caused the plotted trace to be clipped to a fraction of a
            # second (see update_plot's time-based windowing above).
            retention_seconds = min(max(plot_duration + 2.0, 3.0), 60.0)
            while len(time_values) > 1 and (time_values[-1] - time_values[0]) > retention_seconds:
                time_values.pop(0)
                f0_data.pop(0)
                for i in range(NUM_FORMANTS):
                    formant_data[i].pop(0)
                    bandwidth_data[i].pop(0)

    def update_plot():
        nonlocal after_id

        if not is_plotting:
            return

        t_local = list(time_values)
        formant_data_local = [list(d) for d in formant_data]
        bandwidth_data_local = [list(d) for d in bandwidth_data]
        f0_data_local = list(f0_data)

        # Window the plotted data by elapsed TIME (plot_duration), not by
        # point count -- the analysis thread appends one point per audio
        # block (its rate depends on samplerate/blocksize), so a fixed
        # point-count slice would show only a fraction of a second at high
        # sample rates even though the x-axis is scaled for plot_duration.
        xmax = t_local[-1] if t_local else 0
        xmin = max(0, xmax - plot_duration)
        if xmax <= 0:
            xmin, xmax = 0, max(plot_duration, 1e-3)
        elif xmax == xmin:
            xmax = xmin + 1e-3

        start_idx = len(t_local)
        for idx, tv in enumerate(t_local):
            if tv >= xmin:
                start_idx = idx
                break
        t = t_local[start_idx:]

        ax.clear()
        ax2.clear()

        try:
            formant_min = float(formant_min_entry.get())
            formant_max = float(formant_max_entry.get())
        except ValueError:
            formant_min = formant_ymin
            formant_max = formant_ymax

        bandwidth_min = 0
        try:
            bandwidth_max = float(bandwidth_max_entry.get())
        except ValueError:
            bandwidth_max = config["bandwidth_ymax"]

        if t:
            for i in range(NUM_FORMANTS):
                if formant_checkboxes[i].get():
                    key = f'F{i+1}'
                    offset = len(formant_data_local[i]) - len(t)
                    data = formant_data_local[i][offset:]
                    data_plot = [val if val > 0 else np.nan for val in data]
                    ax.plot(t, data_plot, color=trace_colors[key],
                            linewidth=trace_widths[key],
                            marker='o',
                            markersize=config["formant_markersize"],
                            label=key,
                            alpha=config["formant_alpha"])

            for i in range(NUM_FORMANTS):
                if bandwidth_checkboxes[i].get():
                    key = f'B{i+1}'
                    offset = len(bandwidth_data_local[i]) - len(t)
                    data = bandwidth_data_local[i][offset:]
                    # Values above the configured Bandwidth Limit are left
                    # as gaps (NaN) instead of being drawn -- matplotlib's
                    # axis clipping would otherwise flatten an out-of-range
                    # spike against the ceiling, which reads as a real
                    # plateau instead of an excluded value.
                    data_plot = [val if 0 < val <= bandwidth_max else np.nan for val in data]
                    ax2.plot(t, data_plot, color=trace_colors[key],
                             linewidth=trace_widths[key],
                             marker='',
                             markersize=config["bandwidth_markersize"],
                             label=key,
                             alpha=config["bandwidth_alpha"])

        ax.set_ylim(formant_min, formant_max)
        ax2.set_ylim(bandwidth_min, bandwidth_max)

        ax.set_xlim(xmin, xmax)
        ax2.set_xlim(xmin, xmax)

        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Formant Center (Hz)", color='gray', fontsize=12, weight='bold')
        ax.tick_params(axis='y', labelcolor='black')
        ax2.tick_params(axis='y', labelcolor='gray')
        ax.grid(True, alpha=0.3)

        if t:
            lines1, labels1 = ax.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax.legend(lines1 + lines2, labels1 + labels2, loc='upper right')
            ax2.text(1.06, 0.5, 'Bandwidth (Hz)', transform=ax2.transAxes,
                      rotation=90, va='center', ha='center',
                      fontsize=12, color='gray', weight='bold')

        if f0_var.get():
            f0_val = f0_data_local[-1] if f0_data_local else 0.0
            ax.text(0.02, 0.97, f"{f0_val:.0f} Hz", transform=ax.transAxes,
                    color='red', fontsize=14, weight='bold', ha='left', va='top')

        canvas.draw()

        for i in range(NUM_FORMANTS):
            if formant_checkboxes[i].get():
                f_val = formant_data_local[i][-1] if formant_data_local[i] else 0.0
                formant_labels[i*2].config(text=f"F{i+1}: {f_val:.0f} Hz")
            else:
                formant_labels[i*2].config(text=f"F{i+1}:")

            if bandwidth_checkboxes[i].get():
                b_val = bandwidth_data_local[i][-1] if bandwidth_data_local[i] else 0.0
                formant_labels[i*2+1].config(text=f"B{i+1}: {b_val:.0f} Hz")
            else:
                formant_labels[i*2+1].config(text=f"B{i+1}:")

        after_id = root.after(update_interval, update_plot)

    def close():
        """Template close: stop audio, cancel timers, leave the Tk loop; the
        process ends in __main__ with os._exit (no lingering process)."""
        nonlocal after_id
        stop_plotting()
        for tid in (after_id, lang_watch_id):
            if tid:
                try:
                    root.after_cancel(tid)
                except Exception:
                    pass
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        try:
            save_config(config)
        except Exception:
            pass
        try:
            root.quit()
            root.destroy()
        except Exception:
            pass

    def _apply_samplerate(new_rate):
        """Switch the analysis chain (YIN detector + buffer) to a new rate."""
        nonlocal samplerate, pitch_o, analysis_buffer
        if new_rate == samplerate:
            return
        samplerate = new_rate
        pitch_o = YinPitchDetector(1024, 512, samplerate)
        pitch_o.set_unit("Hz")
        pitch_o.set_silence(-40)
        analysis_buffer = np.zeros(int(ANALYSIS_BUFFER_SECONDS * samplerate), dtype=np.float32)

    def apply_settings():
        nonlocal f0_min, f0_max, update_interval, plot_duration, rms_threshold_db, formant_band_fraction
        try:
            f0_min = int(f0_min_entry.get())
            f0_max = int(f0_max_entry.get())
            update_interval = int(update_interval_entry.get())
            plot_duration = float(plot_duration_entry.get())
            rms_threshold_db = float(rms_threshold_entry.get())
            # Fidelity (moved here from the Edit dialog 2026-09-15): preset or custom ±%
            selected_fidelity = fidelity_var.get()
            if selected_fidelity in fidelity_presets:
                formant_band_fraction = fidelity_presets[selected_fidelity]
            else:
                formant_band_fraction = max(0.01, float(fidelity_custom_entry.get()) / 100.0)
            config.update({
                "f0_min": f0_min,
                "f0_max": f0_max,
                "update_interval": update_interval,
                "plot_duration": plot_duration,
                "rms_threshold_db": rms_threshold_db,
                "formant_band_fraction": formant_band_fraction,
            })
            save_config(config)
        except ValueError:
            pass

    def start_plotting():
        nonlocal stream, is_plotting
        if is_plotting:
            return
        is_plotting = True
        if stream is not None and audio_dirty[0]:
            try:
                stream.close()
            except Exception:
                pass
            stream = None
        if stream is None:
            audio_dirty[0] = False
            try:
                _apply_samplerate(int(samplerate_var.get()))
            except ValueError:
                pass
            try:
                stream = sd.InputStream(
                    callback=audio_callback,
                    channels=1,
                    samplerate=samplerate,
                    blocksize=512,
                    device=input_device_index   # None -> PortAudio default input
                )
            except Exception as e:
                is_plotting = False
                messagebox.showerror("Audio Settings", f"Could not open the input device:\n{e}")
                return
        stream.start()
        threading.Thread(target=process_audio, daemon=True).start()
        update_plot()

    def stop_plotting():
        nonlocal stream, is_plotting
        if not is_plotting:
            return
        is_plotting = False
        if stream:
            stream.stop()

    def reset_plotting():
        nonlocal start_time
        stop_plotting()
        time_values.clear()
        f0_data.clear()
        for i in range(NUM_FORMANTS):
            formant_data[i].clear()
            bandwidth_data[i].clear()
        analysis_buffer[:] = 0
        start_time = time.time()

        for i in range(NUM_FORMANTS):
            formant_labels[i*2].config(text=f"F{i+1}:")
            formant_labels[i*2+1].config(text=f"B{i+1}:")

        ax.clear()
        ax2.clear()
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Formant Center (Hz)", color='gray', fontsize=12, weight='bold')
        ax.tick_params(axis='y', labelcolor='black')
        ax2.tick_params(axis='y', labelcolor='gray')
        ax.grid(True, alpha=0.3)
        try:
            ax.set_ylim(float(formant_min_entry.get()), float(formant_max_entry.get()))
        except ValueError:
            ax.set_ylim(formant_ymin, formant_ymax)
        try:
            ax2.set_ylim(0, float(bandwidth_max_entry.get()))
        except ValueError:
            ax2.set_ylim(0, config["bandwidth_ymax"])
        ax.set_xlim(0, max(plot_duration, 1e-3))
        ax2.set_xlim(0, max(plot_duration, 1e-3))
        canvas.draw()

    def save_results():
        t_snapshot = list(time_values)
        if not t_snapshot:
            messagebox.showwarning("Save Results", "No data to save.")
            return
        f_snapshot = [list(d) for d in formant_data]
        b_snapshot = [list(d) for d in bandwidth_data]
        f0_snapshot = list(f0_data)
        n = min(len(t_snapshot), len(f0_snapshot),
                *(len(d) for d in f_snapshot), *(len(d) for d in b_snapshot))

        default_filename = f"formants_mak_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        file_path = filedialog.asksaveasfilename(
            title="Save Results",
            defaultextension=".csv",
            initialfile=default_filename,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not file_path:
            return
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["Time (s)"]
                    + [f"F{i+1} (Hz)" for i in range(NUM_FORMANTS)]
                    + [f"B{i+1} (Hz)" for i in range(NUM_FORMANTS)]
                    + ["F0 (Hz)"]
                )
                for idx in range(n):
                    row = [f"{t_snapshot[idx]:.3f}"]
                    row += [f"{f_snapshot[i][idx]:.0f}" for i in range(NUM_FORMANTS)]
                    row += [f"{b_snapshot[i][idx]:.0f}" for i in range(NUM_FORMANTS)]
                    row.append(f"{f0_snapshot[idx]:.0f}")
                    writer.writerow(row)
            messagebox.showinfo("Save Results", f"Results saved to:\n{file_path}")
        except Exception as e:
            messagebox.showerror("Save Results", f"Could not save the file:\n{e}")

    def on_audio_settings_change(event=None):
        """Device / Driver / Rate combobox changed (same behaviour as the other
        modules): remember the PortAudio indices, save, and reopen the stream
        on the next Start. While running, ask the user to stop and restart."""
        nonlocal input_device_index, hostapi_index
        name = device_var.get()
        for d in audio_devices:
            if d['name'] == name:
                input_device_index = d['index']
                break
        dname = driver_var.get()
        for a in hostapis:
            if a['name'] == dname:
                hostapi_index = a['index']
                break
        try:
            new_rate = int(samplerate_var.get())
        except ValueError:
            new_rate = samplerate
        config.update({"input_device_index": input_device_index,
                       "hostapi_index": hostapi_index,
                       "samplerate": new_rate})
        save_config(config)
        audio_dirty[0] = True
        if is_plotting:
            messagebox.showinfo("Audio Settings Changed",
                                "Please stop and restart the analysis for audio settings to take effect.")

    def on_window_size_change(*_args):
        try:
            w = int(width_var.get())
            h = int(height_var.get())
        except ValueError:
            return
        w = max(800, min(3840, w))
        h = max(600, min(2160, h))
        root.geometry(f"{w}x{h}")
        config["window_width"] = w
        config["window_height"] = h
        save_config(config)

    def on_phoneme_toggle(symbol):
        if phoneme_vars[symbol].get():
            for s, var in phoneme_vars.items():
                if s != symbol:
                    var.set(False)
            active_phoneme_symbol[0] = symbol
            target = phoneme_formants.get(symbol)
            if target:
                for idx in range(min(NUM_FORMANTS, len(target))):
                    expected_formants[idx] = target[idx]
        elif active_phoneme_symbol[0] == symbol:
            active_phoneme_symbol[0] = None

    def rebuild_phoneme_bar():
        for child in phoneme_bar_frame.winfo_children():
            child.destroy()
        phoneme_vars.clear()
        for cat in PHONEME_CATEGORIES:
            symbols = phonemes.get(cat, [])
            cat_frame = ttk.LabelFrame(phoneme_bar_frame, text=cat)
            cat_frame.pack(side=tk.LEFT, padx=(0, 6), fill=tk.Y)
            cols = max(4, -(-len(symbols) // 2)) if cat == "Vowels" else 2   # vowels: 2 rows
            if not symbols:
                ttk.Label(cat_frame, text="(none)", foreground="gray").grid(row=0, column=0, padx=6, pady=4)
                continue
            for idx, sym in enumerate(symbols):
                var = tk.BooleanVar(value=(active_phoneme_symbol[0] == sym))
                phoneme_vars[sym] = var
                cb = tk.Checkbutton(cat_frame, text=sym, variable=var,
                                     command=lambda s=sym: on_phoneme_toggle(s))
                cb.grid(row=idx // cols, column=idx % cols, sticky="w", padx=2)

    def open_formant_settings_dialog():
        """Edit: the selected language's sound table, laid out exactly like
        the new_language.xlsx form (2026-09-15 simplification): per category
        a fixed number of numbered rows (Vowels 8, others 4), IPA + F1-F5
        entries; empty rows are ignored, rows missing IPA/F1/F2 are skipped
        (listed afterwards), empty F3-F5 get the category means. No add/
        delete buttons, no Fidelity (that moved to the Settings panel)."""
        import unicodedata
        dialog = tk.Toplevel(root)
        lang_label = language_state["display"]
        if language_state.get("builtin") and not language_state["external_path"]:
            dialog.title(f"Edit language: {lang_label} (built-in; Apply creates {language_file_name(language_state['name'])})")
        else:
            dialog.title(f"Edit language: {lang_label}")
        dialog.resizable(True, True)
        dialog.transient(root)
        editor_open[0] = True
        dialog.bind("<Destroy>", lambda e: editor_open.__setitem__(0, False) if e.widget is dialog else None)

        outer = tk.Frame(dialog)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        scroll_canvas = tk.Canvas(outer, width=470, height=520, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=vsb.set)
        scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        inner = tk.Frame(scroll_canvas)
        canvas_window = scroll_canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all")))
        scroll_canvas.bind("<Configure>", lambda e: scroll_canvas.itemconfig(canvas_window, width=e.width))
        dialog.bind("<MouseWheel>", lambda e: scroll_canvas.yview_scroll(int(-e.delta / 120), "units"))

        inner.grid_columnconfigure(0, minsize=30)   # No
        inner.grid_columnconfigure(1, minsize=48)   # IPA
        for col in range(2, 2 + NUM_FORMANTS):
            inner.grid_columnconfigure(col, uniform="fcol", minsize=56)

        ttk.Label(inner, text="Empty rows are ignored. IPA, F1 and F2 are required; F3-F5 optional (Hz).",
                  justify=tk.LEFT, font=("Arial", 9)).grid(row=0, column=0, columnspan=7, sticky="w", padx=4, pady=(2, 6))

        table = {cat: [] for cat in PHONEME_CATEGORIES}   # cat -> [(sym_var, [f_vars])]
        r = 1
        for cat in PHONEME_CATEGORIES:
            limit = MAX_PHONEMES_PER_CATEGORY[cat]
            ttk.Label(inner, text=f"{cat} (max {limit})", font=("Arial", 10, "bold")).grid(
                row=r, column=0, columnspan=7, sticky="w", padx=4, pady=(8, 2))
            r += 1
            for ci, htext in enumerate(["No", "IPA", "F1", "F2", "F3", "F4", "F5"]):
                ttk.Label(inner, text=htext, font=("Arial", 8, "bold")).grid(row=r, column=ci, padx=2)
            r += 1
            syms = phonemes.get(cat, [])
            # a language file may carry more symbols than the limit (e.g. an
            # old 12-vowel file): show them all so Apply never drops any
            for i in range(max(limit, len(syms))):
                sym = syms[i] if i < len(syms) else ""
                vals = phoneme_formants.get(sym) if sym else None
                ttk.Label(inner, text=str(i + 1), foreground="gray", font=("Arial", 8)).grid(row=r, column=0, padx=2)
                sym_var = tk.StringVar(value=sym)
                ttk.Entry(inner, textvariable=sym_var, width=5, justify="center").grid(row=r, column=1, padx=2, pady=1)
                f_vars = []
                for fi in range(NUM_FORMANTS):
                    fv = tk.StringVar(value=(str(int(round(vals[fi]))) if vals and fi < len(vals) and vals[fi] else ""))
                    f_vars.append(fv)
                    ttk.Entry(inner, textvariable=fv, width=6, justify="right").grid(row=r, column=fi + 2, padx=2, pady=1)
                table[cat].append((sym_var, f_vars))
                r += 1

        def apply_and_close():
            new_phonemes = {cat: [] for cat in PHONEME_CATEGORIES}
            new_formants = {}
            skipped, filled = [], []
            for cat in PHONEME_CATEGORIES:
                for idx, (sym_var, f_vars) in enumerate(table[cat], start=1):
                    sym = unicodedata.normalize("NFC", sym_var.get().strip())
                    raw = [v.get().strip() for v in f_vars]
                    if not sym and not any(raw):
                        continue                                   # untouched row
                    label = f"{cat} row {idx} {sym if sym else '(no IPA)'}"
                    try:
                        vals = [_form_number(x) if x else None for x in raw]
                    except ValueError:
                        skipped.append(f"{label}: F1-F5 must be numbers")
                        continue
                    if not sym or vals[0] is None or vals[1] is None:
                        skipped.append(f"{label}: IPA, F1 and F2 are all required")
                        continue
                    if any(v is not None and v <= 0 for v in vals):
                        skipped.append(f"{label}: formant values must be positive")
                        continue
                    if sym in new_formants:
                        skipped.append(f"{label}: duplicate symbol, first occurrence kept")
                        continue
                    means = category_mean_formants(cat)
                    out, missing = [], []
                    for fi in range(NUM_FORMANTS):
                        if vals[fi] is None:
                            out.append(means[fi])
                            missing.append(f"F{fi + 1}")
                        else:
                            out.append(int(round(vals[fi])))
                    if missing:
                        filled.append(f"{sym}: {', '.join(missing)}")
                    new_phonemes[cat].append(sym)
                    new_formants[sym] = out
            phonemes.clear()
            phonemes.update(new_phonemes)
            phoneme_formants.clear()
            phoneme_formants.update(new_formants)
            active_phoneme_symbol[0] = None

            # Persist the edited inventory to a language JSON file: an
            # external file is updated in place; a built-in preset is never
            # modified -- the edits are saved as <language>.json next to the
            # app and the app switches to that external file.
            was_file = bool(language_state["external_path"])
            target_name = language_state["name"]
            target_path = language_state["external_path"] if was_file else language_file_path(target_name)
            try:
                save_language_file(target_path, target_name, phonemes, phoneme_formants)
                language_state["mtime"] = _file_mtime(target_path)
                if language_state.get("builtin"):
                    # Built-in language: its file next to the app now holds
                    # the values and takes priority over the embedded preset.
                    if not was_file:
                        _load_builtin_language(target_name)
                        messagebox.showinfo("Language",
                                            f"Your changes were saved to\n{target_path}\n\n"
                                            f"From now on \"{target_name}\" loads this file (listed as "
                                            f"\"{language_state['display']}\"); delete the file to return to "
                                            f"the built-in values.")
                        _refresh_language_combo()
                    config["language"] = target_name
                    config["language_file"] = ""
                else:
                    config["language"] = "external"
                    config["language_file"] = language_state["external_path"]
            except Exception as e:
                messagebox.showerror("Language", f"Could not save language file:\n{e}")
            save_config(config)
            rebuild_phoneme_bar()
            dialog.destroy()
            if skipped or filled:
                n = sum(len(v) for v in new_phonemes.values())
                msg = f"{n} sound(s) saved to {os.path.basename(target_path)}."
                if skipped:
                    msg += "\n\nSkipped (not saved):\n  " + "\n  ".join(skipped)
                if filled:
                    msg += "\n\nTypical category values used for: " + "; ".join(filled)
                messagebox.showinfo("Edit language", msg)

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=8)
        ttk.Button(btn_frame, text="Apply", command=apply_and_close).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
        dialog.bind("<Escape>", lambda e: dialog.destroy())

    def open_trace_settings_dialog():
        dialog = tk.Toplevel(root)
        dialog.title("Trace Colors & Widths")
        dialog.resizable(False, False)
        dialog.transient(root)

        ttk.Label(dialog, text="Parameter", font=("Arial", 10, "bold")).grid(
            row=0, column=0, padx=8, pady=(8, 4))
        ttk.Label(dialog, text="Color", font=("Arial", 10, "bold")).grid(
            row=0, column=1, padx=8, pady=(8, 4))
        ttk.Label(dialog, text="Width", font=("Arial", 10, "bold")).grid(
            row=0, column=2, padx=8, pady=(8, 4))

        width_vars = {}

        def pick_color(key, swatch):
            _, hex_color = colorchooser.askcolor(color=trace_colors[key], title=f"Color for {key}")
            if hex_color:
                trace_colors[key] = hex_color
                swatch.config(bg=hex_color)

        for idx, key in enumerate(TRACE_KEYS, start=1):
            ttk.Label(dialog, text=key).grid(row=idx, column=0, padx=8, pady=3, sticky="w")

            swatch = tk.Button(dialog, bg=trace_colors[key], width=4, relief=tk.RIDGE)
            swatch.config(command=lambda k=key, b=swatch: pick_color(k, b))
            swatch.grid(row=idx, column=1, padx=8, pady=3)

            wvar = tk.StringVar(value=str(trace_widths[key]))
            width_vars[key] = wvar
            ttk.Entry(dialog, textvariable=wvar, width=6).grid(row=idx, column=2, padx=8, pady=3)

        def apply_and_close():
            for key in TRACE_KEYS:
                try:
                    trace_widths[key] = float(width_vars[key].get())
                except ValueError:
                    pass
            config["trace_colors"] = dict(trace_colors)
            config["trace_widths"] = dict(trace_widths)
            save_config(config)
            canvas.draw()
            dialog.destroy()

        btn_row = len(TRACE_KEYS) + 1
        btn_frame = tk.Frame(dialog)
        btn_frame.grid(row=btn_row, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="Apply", command=apply_and_close).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

    # --- UI Setup ---
    root = tk.Tk()
    root.title(TITLE)
    # Screen fit (template): never open larger than the screen
    _sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
    config["window_width"] = min(int(config["window_width"]), _sw)
    config["window_height"] = min(int(config["window_height"]), _sh - 70)
    root.geometry(f"{config['window_width']}x{config['window_height']}")
    try:
        root.iconbitmap(_resource_path("formants_icon.ico"))
    except Exception:
        pass

    formant_checkboxes = [tk.BooleanVar(value=False) for _ in range(NUM_FORMANTS)]
    bandwidth_checkboxes = [tk.BooleanVar(value=False) for _ in range(NUM_FORMANTS)]

    # --- Top control bar (Audio Settings + Window Size, side by side) ---
    top_control_frame = tk.Frame(root)
    top_control_frame.pack(fill=tk.X, padx=10, pady=(10, 0))

    # 1. Audio Settings (Device | Driver | Rate) -- same widgets/behaviour as
    #    the other modules: real device names, host APIs, indices persisted.
    audio_frm = ttk.LabelFrame(top_control_frame, text="Audio Settings", padding=5)
    audio_frm.pack(side=tk.LEFT, padx=(0, 10))
    audio_inner = ttk.Frame(audio_frm)
    audio_inner.pack()

    ttk.Label(audio_inner, text="Device:", font=("Arial", 9)).grid(row=0, column=0, sticky='w', padx=(0, 3))
    device_var = tk.StringVar()
    device_names = [d['name'] for d in audio_devices]
    if device_names:
        default_dev_idx = 0
        for i, d in enumerate(audio_devices):
            if d['index'] == input_device_index:
                default_dev_idx = i
                break
        device_var.set(device_names[default_dev_idx])
    device_combo = ttk.Combobox(audio_inner, textvariable=device_var,
                                values=device_names, state='readonly', width=44)
    device_combo.grid(row=0, column=1, padx=3)
    device_combo.bind('<<ComboboxSelected>>', on_audio_settings_change)

    ttk.Label(audio_inner, text="Driver:", font=("Arial", 9)).grid(row=0, column=2, sticky='w', padx=(5, 3))
    driver_var = tk.StringVar()
    driver_names = [a['name'] for a in hostapis]
    if driver_names:
        default_api_idx = 0
        for i, a in enumerate(hostapis):
            if a['index'] == hostapi_index:
                default_api_idx = i
                break
        driver_var.set(driver_names[default_api_idx])
    driver_combo = ttk.Combobox(audio_inner, textvariable=driver_var,
                                values=driver_names, state='readonly', width=18)
    driver_combo.grid(row=0, column=3, padx=3)
    driver_combo.bind('<<ComboboxSelected>>', on_audio_settings_change)

    ttk.Label(audio_inner, text="Rate:", font=("Arial", 9)).grid(row=0, column=4, sticky='w', padx=(5, 3))
    samplerate_var = tk.StringVar(value=str(samplerate))
    samplerate_combo = ttk.Combobox(audio_inner, textvariable=samplerate_var,
                                    values=['48000', '44100', '16000', '11025'], state='readonly', width=10)
    samplerate_combo.grid(row=0, column=5, padx=3)
    samplerate_combo.bind('<<ComboboxSelected>>', on_audio_settings_change)

    window_size_frm = ttk.LabelFrame(top_control_frame, text="Window Size")
    window_size_frm.pack(side=tk.LEFT)

    tk.Label(window_size_frm, text="W:").grid(row=0, column=0, padx=5, pady=8, sticky="w")
    width_var = tk.StringVar(value=str(config["window_width"]))
    width_entry = tk.Entry(window_size_frm, width=6, textvariable=width_var)
    width_entry.grid(row=0, column=1, padx=5, pady=8, sticky="w")

    tk.Label(window_size_frm, text="H:").grid(row=0, column=2, padx=5, pady=8, sticky="w")
    height_var = tk.StringVar(value=str(config["window_height"]))
    height_entry = tk.Entry(window_size_frm, width=6, textvariable=height_var)
    height_entry.grid(row=0, column=3, padx=5, pady=8, sticky="w")

    width_var.trace_add('write', on_window_size_change)
    height_var.trace_add('write', on_window_size_change)

    # --- Language: which phoneme inventory / formant targets to use ---
    language_frm = ttk.LabelFrame(top_control_frame, text="Language")
    language_frm.pack(side=tk.LEFT, padx=(10, 0), fill=tk.Y)
    language_var = tk.StringVar(value=language_state["display"])
    language_combo = ttk.Combobox(language_frm, textvariable=language_var, state="readonly", width=18)
    language_combo.grid(row=0, column=0, padx=5, pady=(8, 2), sticky="w")

    def _refresh_language_combo():
        _rescan_user_languages()
        values = [builtin_label(n) for n in LANGUAGE_PRESETS] + list(user_languages.keys())
        if language_state["external_path"] and language_state["display"] not in values:
            values.append(language_state["display"])   # a file outside the app folder
        values += [LANGUAGE_EXTERNAL]
        language_combo["values"] = values
        language_var.set(language_state["display"])
        if language_state["external_path"]:
            save_language_button.grid_remove()
            open_file_button.grid()
            reload_button.grid()
        else:
            open_file_button.grid_remove()
            reload_button.grid_remove()
            save_language_button.grid()

    def _use_language_file(path):
        """Load a language file and make it the active language (config too)."""
        _load_external_language(path)
        config["language"] = "external"
        config["language_file"] = path

    def _open_in_default_app(path):
        """Open a file with the system's default program (Excel for the
        form, the default editor for a .json)."""
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def open_new_language_dialog():
        """Target Sounds -> New Language: the one place to add a language.
        'Create Blank Form' writes new_language.xlsx next to the app (refuses
        if it already exists) and opens it; 'Read Form' turns the filled
        form into <name lower case>.json, selects it and lists it under
        Language. Editing afterwards is done with Edit, like any file-based
        language."""
        dlg = tk.Toplevel(root)
        dlg.title("New Language")
        dlg.resizable(False, False)
        dlg.transient(root)
        dlg.grab_set()
        form_path = new_language_form_path()

        ttk.Label(dlg, justify=tk.LEFT, font=("Arial", 9), text=(
            f"1. Create Blank Form writes {NEW_LANGUAGE_FORM} next to the app and opens it.\n"
            "2. Fill it in: language name, then for each sound its IPA symbol and F1, F2 (Hz);\n"
            "    F3-F5 are optional (empty = typical value for the category). Only rows with IPA,\n"
            "    F1 and F2 all filled in are added; the rest are skipped. Save and close it.\n"
            "3. Read Form creates <language name>.json, selects it and lists it under Language.\n"
            "    Use Edit afterwards to adjust symbols or values.")).pack(padx=14, pady=(12, 6), anchor="w")
        status = ttk.Label(dlg, foreground="gray", font=("Arial", 9))
        status.pack(padx=14, anchor="w")

        def _status():
            state = "present" if os.path.exists(form_path) else "not created yet"
            status.config(text=f"{NEW_LANGUAGE_FORM}: {state}   (folder: {app_dir()})")
        _status()

        def create_form():
            if os.path.exists(form_path):
                messagebox.showwarning("New Language",
                                       f"{NEW_LANGUAGE_FORM} already exists in\n{app_dir()}\n\n"
                                       "Fill it in and use Read Form, or rename/delete it to start a new one.",
                                       parent=dlg)
                return
            try:
                create_language_form(form_path)
            except ImportError:
                messagebox.showerror("New Language", "The openpyxl package is missing (pip install openpyxl).", parent=dlg)
                return
            except Exception as e:
                messagebox.showerror("New Language", f"Could not create the form:\n{e}", parent=dlg)
                return
            _status()
            try:
                _open_in_default_app(form_path)
            except Exception as e:
                messagebox.showinfo("New Language", f"Created\n{form_path}\n\n(could not open it automatically: {e})", parent=dlg)

        def read_form():
            if not os.path.exists(form_path):
                messagebox.showwarning("New Language",
                                       f"{NEW_LANGUAGE_FORM} not found in\n{app_dir()}\n\nUse Create Blank Form first.",
                                       parent=dlg)
                return
            try:
                name, new_phon, new_forms, notes = read_language_form(form_path)
            except ImportError:
                messagebox.showerror("New Language", "The openpyxl package is missing (pip install openpyxl).", parent=dlg)
                return
            except Exception as e:
                messagebox.showerror("New Language", f"Could not read {NEW_LANGUAGE_FORM}:\n\n{e}", parent=dlg)
                return
            if name in LANGUAGE_PRESETS or name == LANGUAGE_EXTERNAL:
                messagebox.showwarning("New Language",
                                       f"\"{name}\" is a built-in entry; choose another language name in the form.",
                                       parent=dlg)
                return
            path = language_file_path(name)
            if os.path.exists(path):
                messagebox.showwarning("New Language",
                                       f"{os.path.basename(path)} already exists.\n"
                                       "Select it in the Language list and use Edit, or choose another name in the form.",
                                       parent=dlg)
                return
            try:
                save_language_file(path, name, new_phon, new_forms)
                _use_language_file(path)
            except Exception as e:
                messagebox.showerror("New Language", f"Could not create the language file:\n{e}", parent=dlg)
                return
            active_phoneme_symbol[0] = None
            save_config(config)
            _refresh_language_combo()
            rebuild_phoneme_bar()
            n = sum(len(v) for v in new_phon.values())
            msg = (f"Created {os.path.basename(path)} with {n} sound(s); "
                   f"\"{language_state['display']}\" is now selected.")
            if notes:
                msg += "\n\n" + "\n".join(notes)
            msg += "\n\nUse Edit to adjust symbols or values."
            dlg.destroy()
            messagebox.showinfo("New Language", msg)

        btns = ttk.Frame(dlg)
        btns.pack(pady=(10, 12))
        ttk.Button(btns, text="Create Blank Form", width=18, command=create_form).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Read Form", width=14, command=read_form).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Close", width=10, command=dlg.destroy).pack(side=tk.LEFT, padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dlg.winfo_width()) // 2
        y = root.winfo_rooty() + max(0, (root.winfo_height() - dlg.winfo_height()) // 3)
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

    def on_language_change(event=None):
        selected = language_var.get()
        builtin_by_label = {builtin_label(n): n for n in LANGUAGE_PRESETS}
        if selected in builtin_by_label:
            name = builtin_by_label[selected]
            _load_builtin_language(name)          # <name>.json next to the app if present, else embedded
            config["language"] = name
            config["language_file"] = ""
        elif selected in user_languages:
            try:
                _use_language_file(user_languages[selected])
            except Exception as e:
                messagebox.showerror("Language", f"Could not load\n{user_languages[selected]}\n\n{e}")
                language_var.set(language_state["display"])
                return
        elif selected == LANGUAGE_EXTERNAL:
            path = filedialog.askopenfilename(
                title="Load language JSON", initialdir=os.getcwd(),
                filetypes=[("Language JSON", "*.json"), ("All files", "*.*")])
            if not path:
                language_var.set(language_state["display"])
                return
            try:
                _load_external_language(path)
            except Exception as e:
                messagebox.showerror("Language", f"Could not load\n{path}\n\n{e}")
                language_var.set(language_state["display"])
                return
            config["language"] = "external"
            config["language_file"] = path
        else:
            return  # current entry re-selected -> nothing to do
        active_phoneme_symbol[0] = None
        save_config(config)
        _refresh_language_combo()
        rebuild_phoneme_bar()

    def reload_language_json(quiet=False):
        """Re-read the active language file (after editing it by hand)."""
        path = language_state["external_path"]
        if not path:
            return
        try:
            _load_external_language(path)
            active_phoneme_symbol[0] = None
            _refresh_language_combo()
            rebuild_phoneme_bar()
            if not quiet:
                messagebox.showinfo("Language", f"Reloaded {os.path.basename(path)}.")
        except Exception as e:
            language_state["mtime"] = _file_mtime(path)   # don't nag every tick on a half-saved file
            if not quiet:
                messagebox.showerror("Language", f"Could not reload:\n{e}")
            else:
                print(f"[LANGUAGE] {os.path.basename(path)} changed on disk but could not be read: {e}")

    def open_language_file():
        """Open the active language JSON in the system's default editor."""
        path = language_state["external_path"]
        if not path:
            return
        try:
            _open_in_default_app(path)
        except Exception as e:
            messagebox.showerror("Language", f"Could not open\n{path}\n\n{e}")

    def _watch_language_file():
        """Every 2 s: if the active language file was modified outside the app
        (hand edit, other program), reload it so the file stays the source of
        truth. Skipped while the Edit dialog is open."""
        nonlocal lang_watch_id
        path = language_state["external_path"]
        if path and not editor_open[0]:
            m = _file_mtime(path)
            if m is not None and m != language_state.get("mtime"):
                print(f"[LANGUAGE] {os.path.basename(path)} changed on disk -> reloading")
                reload_language_json(quiet=True)
        lang_watch_id = root.after(2000, _watch_language_file)

    def save_language_json():
        """Built-in language without a file yet: write <language>.json next
        to the app. From then on the built-in entry loads that file (priority
        rule), so the user can edit Turkish/English by hand or with Edit."""
        name = language_state["name"]
        path = language_file_path(name)
        if os.path.exists(path) and not messagebox.askyesno("Language", f"{os.path.basename(path)} already exists. Overwrite?"):
            return
        try:
            save_language_file(path, name, phonemes, phoneme_formants)
            _load_builtin_language(name)
            config["language"] = name
            config["language_file"] = ""
            save_config(config)
            _refresh_language_combo()
            rebuild_phoneme_bar()
            messagebox.showinfo("Language", f"Saved to\n{path}\n\n\"{name}\" now loads this file (listed as "
                                            f"\"{language_state['display']}\"). Edit it by hand (Open file) or with Edit; "
                                            "delete the file to return to the built-in values.")
        except Exception as e:
            messagebox.showerror("Language", f"Could not save:\n{e}")

    language_combo.bind("<<ComboboxSelected>>", on_language_change)
    lang_btns = ttk.Frame(language_frm)
    lang_btns.grid(row=1, column=0, padx=5, pady=(0, 6), sticky="w")
    save_language_button = ttk.Button(lang_btns, text="Save as JSON", command=save_language_json, width=14)
    save_language_button.grid(row=0, column=0, sticky="w")
    open_file_button = ttk.Button(lang_btns, text="Open file", command=open_language_file, width=9)
    open_file_button.grid(row=0, column=1, sticky="w")
    reload_button = ttk.Button(lang_btns, text="Reload", command=reload_language_json, width=7)
    reload_button.grid(row=0, column=2, sticky="w", padx=(3, 0))
    _refresh_language_combo()
    lang_watch_id = root.after(2000, _watch_language_file)

    # --- Phoneme bar: pick a target speech sound to auto-fill the expected
    # formant values used by _reassign_by_expected(). Edit the symbol list
    # and their formants via the "Target Sounds" dialog.
    target_sounds_group = ttk.LabelFrame(top_control_frame, text="Target Sounds")
    target_sounds_group.pack(side=tk.LEFT, padx=(10, 0), fill=tk.Y)
    phoneme_bar_frame = tk.Frame(target_sounds_group)
    phoneme_bar_frame.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)
    rebuild_phoneme_bar()

    # Edit (selected language's symbols/values) with New Language (form-based
    # creation of a language file) right under it, same width.
    target_btns = ttk.Frame(target_sounds_group)
    target_btns.pack(side=tk.LEFT, padx=(12, 14), pady=4, expand=True)
    # width 15 = the 14 that fits "New Language" + ~10% (user request 2026-09-15)
    target_sounds_button = ttk.Button(target_btns, text="Edit", width=15,
                                       command=open_formant_settings_dialog)
    target_sounds_button.pack(ipady=3)
    new_language_button = ttk.Button(target_btns, text="New Language", width=15,
                                     command=open_new_language_dialog)
    new_language_button.pack(ipady=3, pady=(4, 0))

    # Bottom button row is packed BEFORE the expanding content (template):
    # it always stays visible, the graph gives way on small screens.
    button_frame = tk.Frame(root)
    button_frame.pack(side=tk.BOTTOM, pady=10)

    main_frame = tk.Frame(root)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=(0, 10), pady=10)

    plot_frame = tk.Frame(main_frame)
    plot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    fig = Figure(figsize=(7, 3.5))
    ax = fig.add_subplot(111)
    ax2 = ax.twinx()
    canvas = FigureCanvasTkAgg(fig, master=plot_frame)
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    start_button = tk.Button(button_frame, text="Start", command=start_plotting,
                              width=16, height=1)
    start_button.pack(side=tk.LEFT, padx=5)

    stop_button = tk.Button(button_frame, text="Stop", command=stop_plotting,
                             width=16, height=1)
    stop_button.pack(side=tk.LEFT, padx=5)

    reset_button = tk.Button(button_frame, text="Reset", command=reset_plotting,
                              width=16, height=1)
    reset_button.pack(side=tk.LEFT, padx=5)

    save_button = tk.Button(button_frame, text="Save Results", command=save_results,
                             width=16, height=1)
    save_button.pack(side=tk.LEFT, padx=5)

    close_button = tk.Button(button_frame, text="Close", command=close,
                              width=16, height=1)
    close_button.pack(side=tk.LEFT, padx=5)

    controls_frame = tk.Frame(main_frame, width=379)
    controls_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
    controls_frame.pack_propagate(False)

    # --- Settings (top part mirrors rtime_mak: Show F0 checkbox, then
    # "Graph Axes", then "Analysis Parameters", all in one grid) ---
    settings_frame = ttk.LabelFrame(controls_frame, text="Settings", padding=5)
    settings_frame.pack(fill=tk.X, pady=(0, 6))

    row = 0
    f0_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(settings_frame, text="Show F0 (Hz)", variable=f0_var).grid(
        row=row, column=0, sticky="w", pady=2)
    # Apply Settings next to Show F0 (user request 2026-09-15); it used to sit at
    # the bottom of the right column, where the 800 px window clipped it off.
    ttk.Button(settings_frame, text="Apply Settings",
               command=lambda: [apply_settings(), canvas.draw()]).grid(row=row, column=1, sticky="w", pady=2)
    row += 1

    ttk.Separator(settings_frame, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
    row += 1

    ttk.Label(settings_frame, text="Graph Axes", font=("Arial", 12, "bold")).grid(
        row=row, column=0, columnspan=2, pady=1)
    row += 1

    ttk.Label(settings_frame, text="Formant Range:").grid(row=row, column=0, sticky="w", pady=1)
    formant_range_var = tk.StringVar(
        value=_infer_range_preset(FORMANT_RANGE_PRESETS, FORMANT_RANGE_CUSTOM, formant_ymin, formant_ymax))
    formant_range_combo = ttk.Combobox(
        settings_frame, textvariable=formant_range_var, state="readonly", width=13,
        values=list(FORMANT_RANGE_PRESETS.keys()) + [FORMANT_RANGE_CUSTOM])
    formant_range_combo.grid(row=row, column=1, pady=1, sticky="w")
    row += 1

    ttk.Label(settings_frame, text="Formant Min/Max (Hz):").grid(row=row, column=0, sticky="w", pady=1)
    formant_minmax_frame = tk.Frame(settings_frame)
    formant_minmax_frame.grid(row=row, column=1, pady=2, sticky="w")
    formant_min_entry = ttk.Entry(formant_minmax_frame, width=5)
    formant_min_entry.insert(0, str(formant_ymin))
    formant_min_entry.pack(side=tk.LEFT)
    formant_max_entry = ttk.Entry(formant_minmax_frame, width=5)
    formant_max_entry.insert(0, str(formant_ymax))
    formant_max_entry.pack(side=tk.LEFT, padx=(4, 0))
    row += 1

    def on_formant_range_change(event=None):
        selected = formant_range_var.get()
        if selected in FORMANT_RANGE_PRESETS:
            lo, hi = FORMANT_RANGE_PRESETS[selected]
            formant_min_entry.config(state="normal")
            formant_min_entry.delete(0, tk.END)
            formant_min_entry.insert(0, str(lo))
            formant_min_entry.config(state="disabled")
            formant_max_entry.config(state="normal")
            formant_max_entry.delete(0, tk.END)
            formant_max_entry.insert(0, str(hi))
            formant_max_entry.config(state="disabled")
        else:
            formant_min_entry.config(state="normal")
            formant_max_entry.config(state="normal")
        canvas.draw()

    formant_range_combo.bind("<<ComboboxSelected>>", on_formant_range_change)
    on_formant_range_change()

    ttk.Label(settings_frame, text="Bandwidth Limit (Hz):").grid(row=row, column=0, sticky="w", pady=1)
    bandwidth_limit_var = tk.StringVar(
        value=_infer_single_preset(BANDWIDTH_LIMIT_PRESETS, BANDWIDTH_LIMIT_CUSTOM, config["bandwidth_ymax"]))
    bandwidth_limit_combo = ttk.Combobox(
        settings_frame, textvariable=bandwidth_limit_var, state="readonly", width=13,
        values=list(BANDWIDTH_LIMIT_PRESETS.keys()) + [BANDWIDTH_LIMIT_CUSTOM])
    bandwidth_limit_combo.grid(row=row, column=1, pady=1, sticky="w")
    row += 1

    bandwidth_max_entry = ttk.Entry(settings_frame, width=5)
    bandwidth_max_entry.insert(0, str(config["bandwidth_ymax"]))
    bandwidth_max_entry.grid(row=row, column=1, pady=2, sticky="w")
    row += 1

    def on_bandwidth_limit_change(event=None):
        selected = bandwidth_limit_var.get()
        if selected in BANDWIDTH_LIMIT_PRESETS:
            val = BANDWIDTH_LIMIT_PRESETS[selected]
            bandwidth_max_entry.config(state="normal")
            bandwidth_max_entry.delete(0, tk.END)
            bandwidth_max_entry.insert(0, str(val))
            bandwidth_max_entry.config(state="disabled")
        else:
            bandwidth_max_entry.config(state="normal")
        canvas.draw()

    bandwidth_limit_combo.bind("<<ComboboxSelected>>", on_bandwidth_limit_change)
    on_bandwidth_limit_change()

    ttk.Label(settings_frame, text="Duration (s):").grid(row=row, column=0, sticky="w", pady=1)
    plot_duration_entry = ttk.Entry(settings_frame, width=10)
    plot_duration_entry.insert(0, str(plot_duration))
    plot_duration_entry.grid(row=row, column=1, pady=1, sticky="w")
    row += 1

    ttk.Separator(settings_frame, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
    row += 1

    ttk.Label(settings_frame, text="Analysis Parameters", font=("Arial", 12, "bold")).grid(
        row=row, column=0, columnspan=2, pady=1)
    row += 1

    ttk.Label(settings_frame, text="F0 Range:").grid(row=row, column=0, sticky="w", pady=1)
    f0_range_var = tk.StringVar(
        value=_infer_range_preset(F0_RANGE_PRESETS, F0_RANGE_CUSTOM, f0_min, f0_max))
    f0_range_combo = ttk.Combobox(
        settings_frame, textvariable=f0_range_var, state="readonly", width=13,
        values=list(F0_RANGE_PRESETS.keys()) + [F0_RANGE_CUSTOM])
    f0_range_combo.grid(row=row, column=1, pady=1, sticky="w")
    row += 1

    ttk.Label(settings_frame, text="F0 Min/Max (Hz):").grid(row=row, column=0, sticky="w", pady=1)
    f0_minmax_frame = tk.Frame(settings_frame)
    f0_minmax_frame.grid(row=row, column=1, pady=2, sticky="w")
    f0_min_entry = ttk.Entry(f0_minmax_frame, width=5)
    f0_min_entry.insert(0, str(f0_min))
    f0_min_entry.pack(side=tk.LEFT)
    f0_max_entry = ttk.Entry(f0_minmax_frame, width=5)
    f0_max_entry.insert(0, str(f0_max))
    f0_max_entry.pack(side=tk.LEFT, padx=(4, 0))
    row += 1

    def on_f0_range_change(event=None):
        selected = f0_range_var.get()
        if selected in F0_RANGE_PRESETS:
            lo, hi = F0_RANGE_PRESETS[selected]
            f0_min_entry.config(state="normal")
            f0_min_entry.delete(0, tk.END)
            f0_min_entry.insert(0, str(lo))
            f0_min_entry.config(state="disabled")
            f0_max_entry.config(state="normal")
            f0_max_entry.delete(0, tk.END)
            f0_max_entry.insert(0, str(hi))
            f0_max_entry.config(state="disabled")
        else:
            f0_min_entry.config(state="normal")
            f0_max_entry.config(state="normal")

    f0_range_combo.bind("<<ComboboxSelected>>", on_f0_range_change)
    on_f0_range_change()

    ttk.Label(settings_frame, text="dB Threshold:").grid(row=row, column=0, sticky="w", pady=1)
    rms_threshold_entry = ttk.Entry(settings_frame, width=10)
    rms_threshold_entry.insert(0, str(rms_threshold_db))
    rms_threshold_entry.grid(row=row, column=1, pady=1, sticky="w")
    row += 1

    ttk.Label(settings_frame, text="Update (ms):").grid(row=row, column=0, sticky="w", pady=1)
    update_interval_entry = ttk.Entry(settings_frame, width=10)
    update_interval_entry.insert(0, str(update_interval))
    update_interval_entry.grid(row=row, column=1, pady=1, sticky="w")
    row += 1

    # Fidelity (2026-09-15: moved here from the Edit dialog -- it is an
    # analysis setting, not part of the sound table): how far a reading may
    # drift from its expected value and still be accepted. Preset combobox
    # + ±% entry, same pattern as F0 Range / Bandwidth Limit.
    # One row (a second row pushed Live Values F4/F5 off the 800 px window):
    # "Fidelity:" | [preset combobox] [±%] entry
    ttk.Label(settings_frame, text="Fidelity (±%):").grid(row=row, column=0, sticky="w", pady=1)
    fidelity_frame = tk.Frame(settings_frame)
    fidelity_frame.grid(row=row, column=1, pady=1, sticky="w")
    fidelity_var = tk.StringVar(value=_infer_single_preset(fidelity_presets, FIDELITY_CUSTOM, formant_band_fraction))
    fidelity_combo = ttk.Combobox(fidelity_frame, textvariable=fidelity_var, state="readonly", width=8,
                                   values=list(fidelity_presets.keys()) + [FIDELITY_CUSTOM])
    fidelity_combo.pack(side=tk.LEFT)
    fidelity_custom_entry = ttk.Entry(fidelity_frame, width=4, justify="right")
    fidelity_custom_entry.insert(0, str(round(formant_band_fraction * 100)))
    fidelity_custom_entry.pack(side=tk.LEFT, padx=(4, 0))
    row += 1

    def on_fidelity_change(event=None):
        selected = fidelity_var.get()
        if selected in fidelity_presets:
            fidelity_custom_entry.config(state="normal")
            fidelity_custom_entry.delete(0, tk.END)
            fidelity_custom_entry.insert(0, str(round(fidelity_presets[selected] * 100)))
            fidelity_custom_entry.config(state="disabled")
        else:
            fidelity_custom_entry.config(state="normal")

    fidelity_combo.bind("<<ComboboxSelected>>", on_fidelity_change)
    on_fidelity_change()

    trace_settings_button = ttk.Button(controls_frame, text="Trace Colors & Widths...",
                                        command=open_trace_settings_dialog)
    trace_settings_button.pack(fill=tk.X, pady=(0, 4))

    # --- Live Values: two columns, Formants | Bandwidths ---
    values_frame = ttk.LabelFrame(controls_frame, text="Live Values")
    values_frame.pack(fill=tk.X, pady=(0, 4))
    formant_labels = []

    columns_frame = tk.Frame(values_frame)
    columns_frame.pack(fill=tk.X, padx=5, pady=5)

    f_column = tk.Frame(columns_frame)
    f_column.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Label(f_column, text="Formants", font=(config['font_main'], config['font_size_label'], 'bold')).pack()

    b_column = tk.Frame(columns_frame)
    b_column.pack(side=tk.RIGHT, fill=tk.X, expand=True)
    tk.Label(b_column, text="Bandwidths", font=(config['font_main'], config['font_size_label'], 'bold')).pack()

    for i in range(NUM_FORMANTS):
        row_frame = tk.Frame(values_frame)
        row_frame.pack(fill=tk.X, padx=5, pady=1)

        f_frm = tk.Frame(row_frame)
        f_frm.pack(side=tk.LEFT, fill=tk.X, expand=True)
        f_cb = tk.Checkbutton(f_frm, variable=formant_checkboxes[i], text="")
        f_cb.pack(side=tk.LEFT)
        f_lbl = tk.Label(f_frm, text=f"F{i+1}:", font=(config['font_main'], config['font_size_title']),
                         width=12, anchor="w")
        f_lbl.pack(side=tk.LEFT, padx=(5, 0))
        formant_labels.append(f_lbl)

        b_frm = tk.Frame(row_frame)
        b_frm.pack(side=tk.RIGHT, fill=tk.X, expand=True)
        b_cb = tk.Checkbutton(b_frm, variable=bandwidth_checkboxes[i], text="")
        b_cb.pack(side=tk.LEFT)
        b_lbl = tk.Label(b_frm, text=f"B{i+1}:", font=(config['font_main'], config['font_size_title']),
                         width=12, anchor="w")
        b_lbl.pack(side=tk.LEFT, padx=(5, 0))
        formant_labels.append(b_lbl)

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    config = load_config()
    try:
        formants_ui(config)
    except Exception:
        import traceback
        traceback.print_exc()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)
