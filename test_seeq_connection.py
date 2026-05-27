"""
Teste de conexão remota + spy.pull replicando o notebook 00_gerar_hour_prev.ipynb
"""

import pandas as pd
from seeq import spy

AUTH_TOKEN = "9EokNq8IKzxunWpMhQ3OoA"

WORKBOOK_ID    = '0F1373E4-EE13-EC80-9487-F11E5446FD54'
ANOS_HISTORICO = 4
SIGNAL_IDS = {
    'Forca_A': '951157FA-D4BB-4696-A534-AEE4B48532CB',
    'Forca_B': '8D9E2FE1-6000-438C-B293-0EDDAA182851',
}

# ── Login ─────────────────────────────────────────────────────────────────────
print("Conectando ao Seeq...")
spy.login(url="https://kcc.seeq.site", auth_token=AUTH_TOKEN, quiet=True)
print(f"OK Conectado como: {spy.user.name}")

# ── Pull ──────────────────────────────────────────────────────────────────────
user_tz    = spy.utils.get_user_timezone(spy.session)
end_time   = pd.Timestamp.now(tz=user_tz)
start_time = end_time - pd.DateOffset(years=ANOS_HISTORICO)

items_df = pd.DataFrame([
    {'ID': sid, 'Type': 'Signal'}
    for sid in SIGNAL_IDS.values()
])

print(f"Periodo: {start_time.date()} ate {end_time.date()}")
print("Puxando dados do Seeq...")

data_raw = spy.pull(
    items_df,
    start=start_time.isoformat(),
    end=end_time.isoformat(),
    grid=None,
    header='ID',
)

id_to_name = {v: k for k, v in SIGNAL_IDS.items()}
data_raw = data_raw.rename(columns=id_to_name)

print(f"OK Dados recebidos: {len(data_raw):,} leituras")
print(data_raw.head())
