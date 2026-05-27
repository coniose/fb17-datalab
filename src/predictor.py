import numpy as np
import pandas as pd
from pathlib import Path
from src.features import build_features


def load_troca_dates(csv_path=None):
    """
    Carrega as datas confirmadas de troca do rolo maintacker.

    O arquivo troca_modulo.csv contem apenas uma coluna com a data de cada
    substituicao real registrada no SAP (IW38). Essas datas sao o ground truth
    para geracao de targets - substituem completamente a heuristica de salto
    no sinal usada anteriormente.

    Args:
        csv_path: caminho para troca_modulo.csv. Se None, procura em notebooks/.

    Returns:
        Lista de pd.Timestamp (UTC) ordenada cronologicamente.
    """
    if csv_path is None:
        candidates = [
            Path("troca_modulo.csv"),
            Path("notebooks/troca_modulo.csv"),
            Path("../notebooks/troca_modulo.csv"),
        ]
        for p in candidates:
            if p.exists():
                csv_path = p
                break
        else:
            raise FileNotFoundError(
                "troca_modulo.csv nao encontrado. Informe o caminho via csv_path."
            )

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    col = df.columns[0]
    dates = pd.to_datetime(df[col], utc=True).sort_values().tolist()
    return dates


# ---------------------------------------------------------------------------
# Mantido para compatibilidade retroativa - nao usar em novos treinamentos
# ---------------------------------------------------------------------------
def identify_swaps(df, threshold=400, col='Media'):
    """
    [DEPRECATED] Detectava trocas por salto abrupto no sinal (heuristica).
    Use load_troca_dates() com troca_modulo.csv para labels confiaveis.
    """
    df = df.sort_values('Timestamp')
    diff = df[col].diff()
    return df[diff > threshold].index.tolist()


def build_prediction_target(df, lead_time_hours=72, troca_dates=None, csv_path=None):
    """
    Gera target_72h: 1 para leituras dentro da janela de 'lead_time_hours'
    antes de cada troca confirmada, 0 para o restante.

    Quando troca_dates ou csv_path sao fornecidos, usa as datas reais do
    troca_modulo.csv (ground truth). Caso contrario, cai na heuristica legada.

    Args:
        df:               DataFrame com coluna 'Timestamp'.
        lead_time_hours:  Janela de alerta em horas antes da troca (default 72).
        troca_dates:      Lista de pd.Timestamp ja carregada, ou None.
        csv_path:         Caminho para troca_modulo.csv, ou None.

    Returns:
        df com coluna 'target_72h' adicionada/atualizada.
    """
    df = df.copy()
    df['target_72h'] = 0

    ts_col = df['Timestamp']
    if ts_col.dt.tz is None:
        ts_col = ts_col.dt.tz_localize('UTC')

    if troca_dates is not None or csv_path is not None:
        if troca_dates is None:
            troca_dates = load_troca_dates(csv_path)

        for troca_dt in troca_dates:
            window_start = troca_dt - pd.Timedelta(hours=lead_time_hours)
            mask = (ts_col >= window_start) & (ts_col < troca_dt)
            df.loc[mask, 'target_72h'] = 1
    else:
        swap_indices = identify_swaps(df)
        for idx in swap_indices:
            swap_time = df.loc[idx, 'Timestamp']
            window_start = swap_time - pd.Timedelta(hours=lead_time_hours)
            mask = (df['Timestamp'] >= window_start) & (df['Timestamp'] < swap_time)
            df.loc[mask, 'target_72h'] = 1

    return df


def build_rul_target(df, troca_dates=None, csv_path=None):
    """
    Calcula target_rul: horas restantes ate a proxima troca confirmada.

    Para cada leitura de sensor, RUL = (data_proxima_troca - Timestamp) em horas.
    Leituras apos a ultima troca conhecida recebem NaN (periodo atual).

    Args:
        df:           DataFrame com coluna 'Timestamp'.
        troca_dates:  Lista de pd.Timestamp ja carregada, ou None.
        csv_path:     Caminho para troca_modulo.csv, ou None.

    Returns:
        df com coluna 'target_rul' adicionada/atualizada.
    """
    df = df.copy()
    df['target_rul'] = np.nan

    ts_col = df['Timestamp']
    if ts_col.dt.tz is None:
        ts_col = ts_col.dt.tz_localize('UTC')

    if troca_dates is not None or csv_path is not None:
        if troca_dates is None:
            troca_dates = load_troca_dates(csv_path)

        troca_series = pd.Series(troca_dates)

        for i, row_ts in ts_col.items():
            futuras = troca_series[troca_series > row_ts]
            if futuras.empty:
                continue
            prox = futuras.iloc[0]
            df.loc[i, 'target_rul'] = (prox - row_ts).total_seconds() / 3600
    else:
        swap_indices = identify_swaps(df)
        if not swap_indices:
            return df
        last_idx = 0
        for idx in swap_indices:
            swap_time = df.loc[idx, 'Timestamp']
            segment_mask = (df.index >= last_idx) & (df.index < idx)
            df.loc[segment_mask, 'target_rul'] = (
                swap_time - df.loc[segment_mask, 'Timestamp']
            ).dt.total_seconds() / 3600
            last_idx = idx

    return df


def extract_degradation_features(df):
    """
    Extrai as features de degradacao: slope, std e ratio do sinal de selagem.
    """
    if df.index.name != 'Timestamp':
        df_temp = df.set_index('Timestamp')
    else:
        df_temp = df.copy()

    base_features = build_features(df_temp, signal_cols=['Media', 'Delta_AB'], windows=(7, 14))
    base_features['media_ratio_14d'] = df_temp['Media'] / base_features['mean_Media_14d']

    feature_cols = ['slope_Media_14d', 'std_Delta_AB_7d', 'media_ratio_14d']
    result = base_features[feature_cols].copy()
    result.index = df.index
    return result


def classify_troca_dates(df, troca_dates, limiar_prob=0.10, janela_horas=96):
    """
    Classifica cada data de troca em 'genuina' ou 'prematura'.

    Genuina:  o sinal de degradacao (pred_proba) atingiu o limiar nas
              'janela_horas' antes da troca — a peca estava desgastada.

    Prematura: pred_proba ficou abaixo do limiar — a troca foi por
               calendario ou decisao operacional, nao por desgaste real.
               Representa vida util desperdicada.

    Args:
        df:            DataFrame com colunas 'Timestamp' e 'pred_proba'.
        troca_dates:   Lista de pd.Timestamp (ground truth).
        limiar_prob:   Threshold de pred_proba para considerar sinal ativo.
        janela_horas:  Janela de lookback em horas (default 96 = 4 dias).

    Returns:
        (genuinas, prematuras): duas listas de pd.Timestamp.
    """
    ts_col = df['Timestamp']
    if ts_col.dt.tz is None:
        ts_col = ts_col.dt.tz_localize('UTC')

    genuinas, prematuras = [], []
    for dt in troca_dates:
        janela = df[(ts_col >= dt - pd.Timedelta(hours=janela_horas)) & (ts_col < dt)]
        max_prob = janela['pred_proba'].max() if len(janela) > 0 else float('nan')
        if pd.isna(max_prob) or max_prob < limiar_prob:
            prematuras.append(dt)
        else:
            genuinas.append(dt)

    return genuinas, prematuras
