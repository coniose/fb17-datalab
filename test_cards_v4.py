"""
Teste cronologico dos 4 tipos de cartao v4.0 com dados sinteticos.

Progressao simulada:
  Dia  8 — RISCO       : leitura isolada de 732 gf, rolo saudavel
  Dia 20 — CRITICO/AVISO      : degradacao precoce detectada
  Dia 30 — CRITICO/CONFIRMADO : forca critica + p_risk elevado
  Dia 49 — CRITICO/FIM_DE_VIDA: dentro da janela de 5 dias do ETA (54d)

Uso:
  python test_cards_v4.py            # imprime resumo + salva JSONs
  python test_cards_v4.py --post     # tenta postar no SharePoint (requer config.yaml)
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.card_formatter import build_risco_card, build_critico_card

POST_TO_SP = "--post" in sys.argv

ETA_DIAS     = 54.0   # Weibull eta da FB14 (~54d)
MAQUINA      = "FB14-TESTE"
DATA_TROCA   = datetime(2026, 4, 17)   # troca simulada

cenarios = [
    {
        "tipo":     "RISCO",
        "dia":      8,
        "sub":      None,
        "card_fn":  "build_risco_card",
        "dados": dict(
            maquina            = MAQUINA,
            idade_dias         = 8,
            forca_min          = 732.0,
            data_forca_min     = "2026-04-25",
            n_abaixo_800_ciclo = 1,
            p_risk             = 0.08,
            media_3d           = 1104.0,
            acao_recomendada   = (
                "Ir ao local e verificar os motivos da forca de selagem abaixo de 800 gf. "
                "Rolo novo — sem degradacao associada."
            ),
            data_disparo       = datetime(2026, 4, 25, 6, 0),
        ),
    },
    {
        "tipo":  "CRITICO",
        "dia":   20,
        "sub":   "AVISO",
        "card_fn": "build_critico_card",
        "dados": dict(
            maquina             = MAQUINA,
            sub_nivel           = "AVISO",
            idade_dias          = 20,
            p_risk              = 0.38,
            slope_7d            = -5.2,
            forca_min_3d        = 920.0,
            proj_48h            = 900.0,
            media_7d            = 940.0,
            media_7d_anterior   = 1050.0,
            forca_min_ciclo     = 732.0,
            eventos_risco_ciclo = 1,
            acao_recomendada    = (
                "Aumentar frequencia de monitoramento do sinal de forca. "
                "Registrar observacoes no proximo turno."
            ),
            data_disparo        = datetime(2026, 5, 7, 6, 0),
            vida_ref_dias       = ETA_DIAS,
        ),
    },
    {
        "tipo":  "CRITICO",
        "dia":   30,
        "sub":   "CONFIRMADO",
        "card_fn": "build_critico_card",
        "dados": dict(
            maquina             = MAQUINA,
            sub_nivel           = "CONFIRMADO",
            idade_dias          = 30,
            p_risk              = 0.61,
            slope_7d            = -11.4,
            forca_min_3d        = 778.0,
            proj_48h            = 755.0,
            media_7d            = 820.0,
            media_7d_anterior   = 960.0,
            forca_min_ciclo     = 732.0,
            eventos_risco_ciclo = 4,
            acao_recomendada    = (
                "Analise aprofundada e planejamento de troca do rolo maintacker. "
                "Forca critica confirmada — agendar intervencao preventiva."
            ),
            data_disparo        = datetime(2026, 5, 17, 6, 0),
            vida_ref_dias       = ETA_DIAS,
        ),
    },
    {
        "tipo":  "CRITICO",
        "dia":   49,
        "sub":   "FIM_DE_VIDA",
        "card_fn": "build_critico_card",
        "dados": dict(
            maquina             = MAQUINA,
            sub_nivel           = "FIM_DE_VIDA",
            idade_dias          = 49,
            p_risk              = 0.83,
            slope_7d            = -14.1,
            forca_min_3d        = 741.0,
            proj_48h            = 715.0,
            media_7d            = 780.0,
            media_7d_anterior   = 850.0,
            forca_min_ciclo     = 698.0,
            eventos_risco_ciclo = 7,
            acao_recomendada    = (
                "Troca imediata do rolo maintacker — vida util projetada atingida. "
                "ETA: 54 dias. Rolo com 49 dias em operacao."
            ),
            data_disparo        = datetime(2026, 6, 5, 6, 0),
            vida_ref_dias       = ETA_DIAS,
        ),
    },
]

print("=" * 60)
print("  TESTE CRONOLOGICO — CARTOES v4.0")
print("=" * 60)

payloads = []

for c in cenarios:
    label = f"{c['tipo']}" + (f"/{c['sub']}" if c['sub'] else "")
    print(f"\n[Dia {c['dia']:>2}] {label}")

    if c["card_fn"] == "build_risco_card":
        json_str = build_risco_card(**c["dados"])
    else:
        json_str = build_critico_card(**c["dados"])

    card = json.loads(json_str)

    # Extrai titulo do cabecalho
    titulo = card["body"][0]["items"][0]["columns"][0]["items"][0]["text"]
    print(f"  Titulo  : {titulo}")

    # Extrai indicadores se houver FactSet
    for bloco in card["body"]:
        for item in bloco.get("items", []):
            if item.get("type") == "FactSet":
                print("  Campos  :")
                for f in item["facts"]:
                    print(f"    {f['title']:<35} {f['value']}")

    out_file = Path(f"card_dia{c['dia']:02d}_{label.replace('/', '_')}.json")
    out_file.write_text(json_str, encoding="utf-8")
    print(f"  Salvo   : {out_file}")
    payloads.append((label, json_str))

print("\n" + "=" * 60)
print("  4 JSONs gerados. Revise os arquivos card_dia*.json")
print("=" * 60)

# ── Opcional: postar no SharePoint ────────────────────────────────────────────
if POST_TO_SP:
    try:
        from dotenv import dotenv_values
        from src.sharepoint_methods import SharePointClient

        ev_path = Path("sharepoint.ev")
        if not ev_path.exists():
            raise FileNotFoundError("sharepoint.ev nao encontrado na raiz do projeto")

        creds     = dotenv_values(ev_path)
        sp        = SharePointClient(creds["SP_URL"], creds["SP_USER"], creds["SP_PASS"])
        list_name = creds.get("SP_LIST", "Gatilhos_Selagem")

        print("\nPostando no SharePoint...")
        ids_criados = []
        for label, json_str in payloads:
            item = {
                "Title":        f"[TESTE] {MAQUINA} | {label}",
                "Maquina":      MAQUINA,
                "TeamsPayload": json_str,
            }
            ids = sp.insert_list_item(list_name, [item])
            ids_criados.append(ids[0])
            print(f"  {label} -> ID {ids[0]}")

        print(f"\nPronto — {len(ids_criados)} itens criados. Verifique o canal do Teams.")
        print(f"Para limpar: IDs {ids_criados}")
    except FileNotFoundError as e:
        print(f"\n{e}")
        print("Crie sharepoint.ev na raiz com SP_URL, SP_USER, SP_PASS (e opcionalmente SP_LIST).")
    except Exception as e:
        print(f"\nErro ao postar no SharePoint: {e}")
