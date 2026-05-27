import numpy as np
import pandas as pd


def _rolling_slope(series, window_days):
    ts_ns = series.index.view(np.int64)
    y = series.values
    n = len(series)
    length_ns = int(window_days * 24 * 3600 * 1e9)
    slopes = np.empty(n)
    for i in range(n):
        j = np.searchsorted(ts_ns, ts_ns[i] - length_ns, side="left")
        x = (ts_ns[j : i + 1] - ts_ns[j]) / 1e9 / 3600 / 24
        ys = y[j : i + 1]
        if len(x) < 2:
            slopes[i] = 0.0
        else:
            xm, ym = x.mean(), ys.mean()
            denom = ((x - xm) ** 2).sum()
            slopes[i] = 0.0 if denom == 0 else ((x - xm) * (ys - ym)).sum() / denom
    return slopes


def build_features(df, signal_cols, windows=(7, 14)):
    """Rolling mean, std, and slope for each signal across each window size."""
    features = pd.DataFrame(index=df.index)
    for win in windows:
        w = f"{win}D"
        for col in signal_cols:
            features[f"mean_{col}_{win}d"] = df[col].rolling(w, min_periods=1).mean()
            features[f"std_{col}_{win}d"] = df[col].rolling(w, min_periods=1).std().fillna(0)
            features[f"slope_{col}_{win}d"] = _rolling_slope(df[col], window_days=win)
    return features
