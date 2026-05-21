#!/usr/bin/env python3
"""
Run once after cloning to configure your data source.
Creates config.yaml with workbook ID, the two sealing-force signals,
the two SKU signals (bag1/bag2), and time window.

Usage:
    python init_project.py

Two search modes for sealing-force signals:
  [1] Search by name — finds workbooks shared with you (content_filter="all")
  [2] Browse by folder — navigates your personal folder tree, then selects
      worksheet and searches signals via spy.search(worksheet.url)

SKU signals:
  Always configured by pasting the full worksheet URL that contains the
  product-code signals. spy.search() reads only metadata (IDs/names) —
  does NOT pull data, so corrupted PI signals na mesma worksheet não causam erro.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import yaml
from pathlib import Path

CONFIG_PATH = Path("config.yaml")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


# ── Seeq connection ───────────────────────────────────────────────────────────

def connect_seeq():
    try:
        from seeq import spy
    except ImportError:
        print("ERROR: seeq-spy not installed.  Run: pip install seeq")
        sys.exit(1)

    print("\n--- Seeq Connection ---")
    print("Leave blank if already authenticated (e.g. running inside Seeq Data Lab).")
    url = ask("Seeq server URL (e.g. https://your-server.seeq.host)")

    if url:
        username = ask("Username")
        password = ask("Password")
        spy.login(url=url, username=username, password=password)

    return spy


# ── Mode A: search shared workbooks by name ───────────────────────────────────

def search_workbook_by_name(spy):
    """Search across all shared workbooks by name keyword."""
    print("\n--- Workbook Selection (by name) ---")
    search_term = ask("Search workbook by name")

    print("Searching...")
    results = spy.workbooks.search({"Name": search_term}, content_filter="all")

    if results is None or results.empty:
        print("No workbooks found.  Try a different search term.")
        sys.exit(1)

    cols = ["ID", "Name", "Owner Name", "Updated At"]
    display_cols = [c for c in cols if c in results.columns]
    print("\n" + results[display_cols].to_string())

    print("\nEnter the row number (0-indexed) or paste the full Workbook ID:")
    selection = ask("> ").strip()

    try:
        workbook_id = results.iloc[int(selection)]["ID"]
    except (ValueError, IndexError):
        workbook_id = selection

    print(f"\nSelected workbook: {workbook_id}")
    return workbook_id


def select_signals_from_workbook(spy, workbook_id):
    """Pick signals from the first worksheet of a workbook via display_items."""
    print("\n--- Signal Selection ---")
    print("Fetching signals from workbook...")

    wbs      = spy.workbooks.search({"ID": workbook_id}, content_filter="all")
    obj      = spy.workbooks.pull(wbs)
    items_df = obj[0].worksheets[0].display_items

    signals_df = items_df[items_df["Type"] == "Signal"].reset_index(drop=True)

    if signals_df.empty:
        print("No Signal-type items found in this workbook.")
        sys.exit(1)

    print("\nSignals found:")
    for i, row in signals_df.iterrows():
        print(f"  [{i}]  {str(row['Name'])[:80]}")

    return _pick_two_signals(signals_df)


# ── Mode B: browse personal folder tree ──────────────────────────────────────

def select_folder(spy):
    print("\n--- Folder Selection ---")
    print("Fetching available folders (this might take a moment)...")
    df = spy.workbooks.search({}, content_filter="all")
    if df is None or df.empty:
        print("No workbooks/folders found.")
        sys.exit(1)

    paths = sorted({p for p in df["Path"].dropna() if p.strip()})

    print("\nAvailable Folders:")
    for i, p in enumerate(paths):
        print(f"  [{i}] {p}")

    print("\nEnter the row number, or type the folder path directly:")
    selection = ask("> ").strip()

    try:
        folder = paths[int(selection)]
    except (ValueError, IndexError):
        folder = selection

    print(f"\nSelected folder: {folder}")
    return folder


def select_workbook_in_folder(spy, folder):
    print(f"\n--- Workbook Selection (in '{folder}') ---")
    df = spy.workbooks.search({"Path": folder}, content_filter="all")
    if df is None or df.empty:
        print("No workbooks found in this folder.")
        sys.exit(1)

    cols = ["ID", "Name", "Updated At"]
    display_cols = [c for c in cols if c in df.columns]
    print("\n" + df[display_cols].to_string())

    print("\nEnter the row number (0-indexed) or paste the full Workbook ID:")
    selection = ask("> ").strip()

    try:
        workbook_id = df.iloc[int(selection)]["ID"]
    except (ValueError, IndexError):
        workbook_id = selection

    print(f"\nSelected workbook ID: {workbook_id}")
    return workbook_id


def select_worksheet(spy, workbook_id):
    print("\n--- Worksheet Selection ---")
    wbs = spy.workbooks.search({"ID": workbook_id})
    if wbs is None or wbs.empty:
        print(f"Workbook '{workbook_id}' not found.")
        sys.exit(1)

    obj        = spy.workbooks.pull(wbs)
    worksheets = obj[0].worksheets

    print("\nAvailable Worksheets:")
    for i, ws in enumerate(worksheets):
        print(f"  [{i}] {ws.name}")

    print("\nEnter the row number, or type the worksheet name directly:")
    selection = ask("> ").strip()

    selected_ws = None
    try:
        selected_ws = worksheets[int(selection)]
    except (ValueError, IndexError):
        for ws in worksheets:
            if ws.name == selection:
                selected_ws = ws
                break

    if not selected_ws:
        print("Worksheet not found.")
        sys.exit(1)

    print(f"\nSelected worksheet: {selected_ws.name}")
    return selected_ws


def select_signals_from_worksheet(spy, worksheet):
    """Pick signals via spy.search(worksheet.url) — works for personal workbooks."""
    print("\n--- Signal Selection ---")
    print(f"Fetching signals from worksheet '{worksheet.name}'...")

    items_df = spy.search(worksheet.url)

    if items_df is None or items_df.empty:
        print("No items found in this worksheet.")
        sys.exit(1)

    signals_df = items_df[
        items_df["Type"].isin(["Signal", "StoredSignal"])
    ].reset_index(drop=True)

    if signals_df.empty:
        print("No Signal-type items found in this worksheet.")
        sys.exit(1)

    print("\nSignals found:")
    for i, row in signals_df.iterrows():
        print(f"  [{i}]  {str(row['Name'])[:80]}")

    return _pick_two_signals(signals_df)


# ── Shared signal picker ──────────────────────────────────────────────────────

def _pick_two_signals(signals_df):
    """Ask the user to pick Forca_A and Forca_B from a signals DataFrame."""
    print()
    print("Select the TWO sealing-force signals (index numbers from the list above).")
    print("Order does not matter — the pipeline uses (A + B) / 2 for the mean.")

    def pick(label):
        while True:
            idx = ask(f"  Index for {label}")
            try:
                row = signals_df.iloc[int(idx)]
                print(f"    → {row['Name']}")
                return row
            except (ValueError, IndexError):
                print("    Invalid index, try again.")

    row_a = pick("Sealing Force A (Forca_A)")
    row_b = pick("Sealing Force B (Forca_B)")

    return [
        {"id": row_a["ID"], "name": "Forca_A", "original_name": row_a["Name"], "type": "Signal"},
        {"id": row_b["ID"], "name": "Forca_B", "original_name": row_b["Name"], "type": "Signal"},
    ]


# ── SKU signal picker ────────────────────────────────────────────────────────

def select_sku_signals(spy):
    """Pede a URL da worksheet de SKU, lista os sinais via spy.search() e
    pede ao usuário para identificar bag1 e bag2.

    Usa spy.search() (só metadados) — nunca spy.pull() — para evitar
    PIException de outros sinais corrompidos na mesma worksheet.
    """
    print("\n--- SKU Signal Selection ---")
    print("Cole a URL completa da worksheet que contém os sinais de produto (bag1/bag2).")
    print("Exemplo: https://kcc.seeq.site/workbook/WORKBOOK-ID/worksheet/WORKSHEET-ID")
    url = ask("URL da worksheet de SKU").strip()

    print("\nBuscando sinais (apenas metadados, sem pull)...")
    import pandas as pd
    items_df = spy.search(url)

    if items_df is None or items_df.empty:
        print("Nenhum item encontrado nesta worksheet. Verifique a URL e tente novamente.")
        sys.exit(1)

    signals_df = items_df[
        items_df["Type"].isin(["Signal", "StoredSignal", "CalculatedSignal"])
    ].reset_index(drop=True)

    if signals_df.empty:
        print("Nenhum sinal encontrado nesta worksheet.")
        sys.exit(1)

    print(f"\n{len(signals_df)} sinais encontrados:")
    for i, row in signals_df.iterrows():
        print(f"  [{i}]  {str(row['Name'])[:80]}")

    def pick(label):
        while True:
            idx = ask(f"\n  Índice do sinal para {label}")
            try:
                row = signals_df.iloc[int(idx)]
                print(f"    -> {row['Name']}")
                return row
            except (ValueError, IndexError):
                print("    Índice inválido, tente novamente.")

    print()
    print("Identifique qual sinal representa cada bagger.")
    print("(Procure por nomes como B1_Product, Bagger1, bag1, etc.)")
    row_b1 = pick("Bagger 1 — SKU do produto (bag1_sku)")
    row_b2 = pick("Bagger 2 — SKU do produto (bag2_sku)")

    return [
        {"id": row_b1["ID"], "name": "bag1_sku", "original_name": row_b1["Name"], "type": "Signal"},
        {"id": row_b2["ID"], "name": "bag2_sku", "original_name": row_b2["Name"], "type": "Signal"},
    ]


# ── Time window & IQR ────────────────────────────────────────────────────────

def ask_time_delta():
    print("\n--- Time Window ---")
    print("How many days back from today should data be pulled?")
    print("  365 = 1 year  |  730 = 2 years  |  1460 = 4 years")
    days = ask("Days", default="1460")
    return int(days)


def ask_iqr_multiplier():
    print("\n--- Outlier Filter ---")
    print("The IQR filter removes readings where |Forca_A - Forca_B| is an outlier.")
    print("  1.0 = aggressive (removes more)  |  2.0 = conservative (removes less)")
    mult = ask("IQR multiplier", default="1.0")
    return float(mult)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  Sealing-Force Project — Setup")
    print("=" * 52)

    if CONFIG_PATH.exists():
        overwrite = ask("\nconfig.yaml already exists. Overwrite? (y/n)", default="n")
        if overwrite.lower() != "y":
            print("Aborted.")
            return

    spy = connect_seeq()

    # ── Modo de busca ─────────────────────────────────────────────────────────
    print("\n--- Search Mode ---")
    print("  [1] Search by name   — workbooks shared with you")
    print("  [2] Browse by folder — your personal workbooks (folder → workbook → worksheet)")
    mode = ask("Mode", default="1").strip()

    worksheet_name = None

    if mode == "2":
        folder         = select_folder(spy)
        workbook_id    = select_workbook_in_folder(spy, folder)
        worksheet      = select_worksheet(spy, workbook_id)
        worksheet_name = worksheet.name
        signals        = select_signals_from_worksheet(spy, worksheet)
    else:
        workbook_id = search_workbook_by_name(spy)
        signals     = select_signals_from_workbook(spy, workbook_id)

    sku_signals = select_sku_signals(spy)
    time_delta  = ask_time_delta()
    iqr_mult    = ask_iqr_multiplier()

    # ── Montar config ─────────────────────────────────────────────────────────
    project_section = {"workbook_id": workbook_id, "time_delta_days": time_delta}
    if worksheet_name:
        project_section["worksheet_name"] = worksheet_name

    config = {
        "project": project_section,
        "signals": signals,
        "sku_signals": sku_signals,
        "preprocessing": {
            "delta_col_a":    "Forca_A",
            "delta_col_b":    "Forca_B",
            "iqr_multiplier": iqr_mult,
        },
        # Parâmetros do motor de gatilho v2.2 — calibrados sobre 26 ciclos FB14.
        # Ajuste apenas se houver evidência estatística (backtest) que justifique.
        "trigger": {
            # Weibull — não alterar sem refit sobre novos ciclos históricos
            "weibull_beta":           1.181,
            "weibull_eta_h":          1297.0,   # horas = 54.05 dias
            # p_risk = age_risk + (1 - age_risk) × signal_score × boost_sinal
            "boost_sinal":            0.65,
            # RED: todas as condições simultâneas (C1 AND C2 AND C3 AND C4)
            "limiar_p_risk":          0.48,     # C1
            "limiar_signal_score":    0.22,     # C2
            "idade_minima_dias":      15,       # C3 — acomodamento inicial do rolo
            "proj_48h_limiar":        800.0,    # C4 — gate de força projetada (N)
            "sustentacao_proj_dias":  2,        # C4 — mínimo de dias abaixo (janela 5d)
            "cooldown_h":             48,
            "snooze_dias":            5,
            # AMARELO — aviso precoce (C1_am OR C2_am)
            "amarelo_p_risk":         0.35,
            "amarelo_signal":         0.15,
            "amarelo_cooldown_h":     72,
            # EMERGÊNCIA — chequemate de força crítica
            "forca_min_emergencia":   800.0,    # N — mínimo nos últimos 3 dias
            "emergencia_cooldown_h":  48,
            # REVISÃO — marcos automáticos de ciclo
            "revisao_marcos_dias":    [20, 25, 35],
        },
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print("\nconfig.yaml saved successfully.")
    print("\nNext steps:")
    print("  1. python extrair_troca_modulo.py --iw38 iw38.csv  — gera troca_modulo.csv (obrigatório)")
    print("  2. notebooks/pipeline_producao.ipynb               — pipeline completo de produção")
    print("  3. notebooks/00_gerar_hour_prev.ipynb              — extração e análise exploratória")


if __name__ == "__main__":
    main()
