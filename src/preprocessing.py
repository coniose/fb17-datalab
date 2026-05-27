import numpy as np
import pandas as pd


def filter_delta_outliers(df, col_a, col_b, multiplier=1.0):
    """Remove rows where |col_a - col_b| is an IQR outlier."""
    delta = (df[col_a] - df[col_b]).abs()
    Q1 = delta.quantile(0.25)
    Q3 = delta.quantile(0.75)
    threshold = Q3 + multiplier * (Q3 - Q1)
    clean = df[delta <= threshold].copy()
    clean["Delta_AB"] = delta[delta <= threshold]
    return clean, float(threshold)


def add_media(df, col_a, col_b, out_col="Media"):
    df = df.copy()
    df[out_col] = (df[col_a] + df[col_b]) / 2
    return df
