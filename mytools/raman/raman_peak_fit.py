"""
raman_peak_fit.py
==================

A small, reusable pipeline for Raman spectra exported from HORIBA LabSpec6 (.txt files):

    1. Load one or more LabSpec .txt exports (skips the '#...' metadata header automatically).
    2. Detect candidate peaks (scipy.signal.find_peaks, prominence-based).
    3. Group peaks that overlap / sit on the same shoulder into a single fitting window,
       and fit them ALL SIMULTANEOUSLY as a sum of peak shapes + a linear background.
       (This is what a single find_peaks call or a naive one-peak-at-a-time fit gets wrong
        on shoulders like the one in your data around ~90-150 cm-1.)
    4. Report center, height, area and FWHM (with fit uncertainty) for every peak.
    5. Save one annotated PNG per spectrum + one combined CSV with every peak from every file.

Usage
-----
    python raman_peak_fit.py file1.txt file2.txt ...
    python raman_peak_fit.py *.txt

Or import the functions and use them from your own script / Jupyter notebook.

Requirements: numpy, pandas, scipy, lmfit, matplotlib
    pip install numpy pandas scipy lmfit matplotlib
"""

import sys
import glob
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.signal import find_peaks, peak_widths
from lmfit.models import GaussianModel, LorentzianModel, VoigtModel, LinearModel
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


# ---------------------------------------------------------------------------
# 1. Loading
# ---------------------------------------------------------------------------

def load_labspec_txt(path):
    """Load a two-column (wavenumber, intensity) LabSpec6 ASCII export.

    Lines starting with '#' (the acquisition metadata block) are ignored.
    Works with either '.' or ',' decimal separators as long as columns are
    tab-separated (LabSpec's default export).
    """
    rows = []
    with open(path, 'r', encoding='latin-1') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.replace(',', '.').split('\t')
            if len(parts) == 2:
                try:
                    x, y = float(parts[0]), float(parts[1])
                    rows.append((x, y))
                except ValueError:
                    continue
    df = pd.DataFrame(rows, columns=['wavenumber', 'intensity']).sort_values('wavenumber')
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. Peak detection + grouping into fitting windows
# ---------------------------------------------------------------------------

def detect_peaks(x, y, prominence=30, min_distance_pts=3, rel_height=0.5):
    """Find candidate peaks and an initial FWHM estimate for each."""
    idx, props = find_peaks(y, prominence=prominence, distance=min_distance_pts)
    widths_pts, width_heights, left_ips, right_ips = peak_widths(y, idx, rel_height=rel_height)

    # convert width from index-space to x-space (assumes ~uniform spacing, true for LabSpec)
    dx = np.median(np.diff(x))
    widths_x = widths_pts * dx

    peaks = pd.DataFrame({
        'index': idx,
        'position': x[idx],
        'height': y[idx],
        'prominence': props['prominences'],
        'init_fwhm': widths_x,
    })
    return peaks


def group_peaks(peaks, x, merge_gap=25, pad=20):
    """Cluster peaks that are close together into shared fitting windows.

    Two peaks are merged into the same group if they are within `merge_gap`
    (cm-1) of each other. Each group's fitting window extends `pad` cm-1
    beyond the outermost peaks (clipped to the data range).
    """
    peaks = peaks.sort_values('position').reset_index(drop=True)
    groups = []
    current = [0]
    for i in range(1, len(peaks)):
        if peaks.loc[i, 'position'] - peaks.loc[i - 1, 'position'] <= merge_gap:
            current.append(i)
        else:
            groups.append(current)
            current = [i]
    groups.append(current)

    xmin, xmax = x.min(), x.max()
    windows = []
    for g in groups:
        sub = peaks.loc[g]
        lo = max(xmin, sub['position'].min() - pad)
        hi = min(xmax, sub['position'].max() + pad)
        windows.append((lo, hi, sub))
    return windows


# ---------------------------------------------------------------------------
# 3. Joint multi-peak fitting
# ---------------------------------------------------------------------------

SHAPE_MODELS = {
    'gaussian': GaussianModel,
    'lorentzian': LorentzianModel,
    'voigt': VoigtModel,
}


def fit_peak_group(x, y, peaks_in_group, shape='gaussian'):
    """Fit N overlapping peaks simultaneously + a linear background.

    Returns the lmfit ModelResult and the list of component prefixes.
    """
    ModelClass = SHAPE_MODELS[shape]
    model = LinearModel(prefix='bg_')
    params = model.make_params(slope=0, intercept=min(y))

    prefixes = []
    for i, (_, row) in enumerate(peaks_in_group.iterrows()):
        pref = f'p{i}_'
        prefixes.append(pref)
        comp = ModelClass(prefix=pref)
        model = model + comp
        comp_params = comp.make_params()
        init_sigma = max(row['init_fwhm'] / 2.355, 1.0)  # rough Gaussian sigma guess
        comp_params[f'{pref}center'].set(value=row['position'],
                                          min=row['position'] - 15, max=row['position'] + 15)
        comp_params[f'{pref}sigma'].set(value=init_sigma, min=0.5, max=init_sigma * 6)
        comp_params[f'{pref}amplitude'].set(value=row['height'] * init_sigma * 2.5, min=0)
        params.update(comp_params)

    result = model.fit(y, params, x=x)
    return result, prefixes


# ---------------------------------------------------------------------------
# 4. Full pipeline for one file
# ---------------------------------------------------------------------------

def process_file(path, shape='gaussian', prominence=30, merge_gap=25, pad=20, plot=True, outdir='.'):
    df = load_labspec_txt(path)
    x, y = df['wavenumber'].values, df['intensity'].values

    peaks = detect_peaks(x, y, prominence=prominence)
    windows = group_peaks(peaks, x, merge_gap=merge_gap, pad=pad)

    all_results = []
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(x, y, 'k-', lw=1, label='Raw spectrum')

    for lo, hi, sub in windows:
        mask = (x >= lo) & (x <= hi)
        xf, yf = x[mask], y[mask]
        if len(xf) < 5:
            continue

        result, prefixes = fit_peak_group(xf, yf, sub, shape=shape)
        ax.plot(xf, result.best_fit, 'b-', lw=2)

        for pref in prefixes:
            center = result.params[f'{pref}center'].value
            center_err = result.params[f'{pref}center'].stderr
            fwhm = result.params[f'{pref}fwhm'].value
            fwhm_err = result.params[f'{pref}fwhm'].stderr
            height = result.params[f'{pref}height'].value
            amplitude = result.params[f'{pref}amplitude'].value

            all_results.append({
                'file': path,
                'shape': shape,
                'center_cm-1': center,
                'center_stderr': center_err,
                'fwhm_cm-1': fwhm,
                'fwhm_stderr': fwhm_err,
                'height': height,
                'amplitude_area': amplitude,
                'window_lo': lo,
                'window_hi': hi,
                'fit_r_squared': result.rsquared,
            })
            ax.axvline(center, color='r', ls='--', lw=0.7, alpha=0.6)

    res_df = pd.DataFrame(all_results).sort_values('center_cm-1').reset_index(drop=True)

    stem = Path(path).stem

    ax.set_xlabel('Raman shift (cm$^{-1}$)')
    ax.set_ylabel('Intensity (counts)')
    ax.set_title(Path(path).name)
    ax.legend(['Raw spectrum', 'Fit'])
    plt.tight_layout()
    if plot:
        outpng = str(Path(outdir) / f"{stem}_fit.png")
        plt.savefig(outpng, dpi=150)
        plt.close(fig)
        print(f"  saved plot -> {outpng}")
    else:
        plt.close(fig)

    return res_df

def analyse_raman_folder(path_raman_folder, shape='gaussian', prominence=30,
                        merge_gap=25, pad=20, outdir=None):
    """Fit every Raman spectrum in a folder and combine the results into one CSV.

    Scans ``path_raman_folder`` for HORIBA LabSpec6 ``.txt`` exports and runs
    the full peak-fitting pipeline (:func:`load_labspec_txt`,
    :func:`detect_peaks`, :func:`group_peaks`, :func:`fit_peak_group`) on each
    one via :func:`process_file`. Peaks that overlap or sit on the same
    shoulder are fitted simultaneously rather than one at a time. One
    annotated PNG is saved per spectrum, and every fitted peak from every
    file (center, height, area, FWHM and their fit uncertainties) is combined
    into a single CSV.

    Args:
        path_raman_folder (str): Path to the folder containing the Raman
            ``.txt`` files to analyse (HORIBA LabSpec6 ASCII exports).
        shape (str): Peak line shape used for the fit. One of ``"gaussian"``,
            ``"lorentzian"`` or ``"voigt"``. Defaults to ``"gaussian"``.
        prominence (float): Minimum peak prominence used for peak detection
            (see ``scipy.signal.find_peaks``). Defaults to 30.
        merge_gap (float): Peaks within this many cm-1 of each other are
            merged into the same fitting window. Defaults to 25.
        pad (float): Extra cm-1 padding added around each fitting window,
            clipped to the data range. Defaults to 20.
        outdir (str, optional): Directory where the annotated plots and the
            combined CSV are written. Defaults to ``path_raman_folder`` when
            not given.

    Returns:
        str: Path to the combined CSV file (``peak_fit_results.csv``)
        containing the fitted peaks for every spectrum found in
        ``path_raman_folder``.

    Raises:
        FileNotFoundError: If ``path_raman_folder`` does not exist or is not
            a directory.
        ValueError: If no ``.txt`` file is found in ``path_raman_folder``.
    """
    folder = Path(path_raman_folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"Raman folder not found: {folder}")

    outdir = str(outdir) if outdir is not None else str(folder)
    Path(outdir).mkdir(parents=True, exist_ok=True)

    files = sorted(str(p) for p in folder.glob('*.txt'))
    if not files:
        raise ValueError(f"No .txt file found in {folder}")

    all_dfs = []
    for f in files:
        print(f"Processing {f} ...")
        df = process_file(f, shape=shape, prominence=prominence,
                           merge_gap=merge_gap, pad=pad, outdir=outdir)
        all_dfs.append(df)

    combined = pd.concat(all_dfs, ignore_index=True)
    csv_file_path = str(Path(outdir) / "peak_fit_results.csv")
    combined.to_csv(csv_file_path, index=False)
    print(f"\nCombined results for {len(files)} file(s) saved -> {csv_file_path}")

    return csv_file_path

# ---------------------------------------------------------------------------
# 5. CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Multi-peak Gaussian/Lorentzian/Voigt fitting for LabSpec6 Raman .txt exports.")
    parser.add_argument('files', nargs='+', help="LabSpec .txt file(s) or glob pattern(s)")
    parser.add_argument('--shape', default='gaussian', choices=list(SHAPE_MODELS.keys()), help="Peak line shape (default: gaussian)")
    parser.add_argument('--prominence', type=float, default=30, help="Minimum peak prominence for detection (default: 30)")
    parser.add_argument('--merge-gap', type=float, default=25, help="Merge peaks within this many cm-1 into one fit window (default: 25)")
    parser.add_argument('--pad', type=float, default=20, help="Extra cm-1 padding added around each fit window (default: 20)")
    parser.add_argument('--outdir', default='.', help="Output directory for plots/CSV (default: current dir)")
    args = parser.parse_args()

    files = []
    for pat in args.files:
        files.extend(glob.glob(pat))
    files = sorted(set(files))
    if not files:
        print("No files matched.")
        sys.exit(1)

    all_dfs = []
    for f in files:
        print(f"Processing {f} ...")
        df = process_file(f, shape=args.shape, prominence=args.prominence,
                           merge_gap=args.merge_gap, pad=args.pad, outdir=args.outdir)
        all_dfs.append(df)

    combined = pd.concat(all_dfs, ignore_index=True)
    combined.to_csv(f"{args.outdir}/peak_fit_results.csv", index=False)
    print(f"\nCombined results for {len(files)} file(s) saved -> {args.outdir}/peak_fit_results.csv")
    print(combined.to_string(index=False))
