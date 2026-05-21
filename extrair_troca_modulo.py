#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extrair_troca_modulo.py - Extrai datas de troca do conjunto maintacker do IW38.

O conjunto maintacker pode ser servido de duas formas — ambas reiniciam o ciclo
de vida e entram no mesmo troca_modulo.csv:

  revestimento  (~R$ 3.000) — revestimento completo do rolo emborrachado
  retificacao   (~R$   800) — retificacao do revestimento existente

Gera:
  troca_modulo.csv       -> datas confirmadas (ALTA) com coluna "tipo"
  troca_modulo_audit.csv -> tabela completa com confianca, tipo e razao

Uso:
  python extrair_troca_modulo.py --iw38 iw38.csv [opcoes]

Opcoes:
  --iw38          CSV exportado pelo SAP IW38              (default: iw38.csv)
  --maquina       prefixo do Local de instalacao           (default: 7320-IC-I1-15)
  --out           pasta de saida                           (default: .)
  --precos        precos revestimento, virgula              (default: 2400,2500,3000)
  --tolerancia    pct tolerancia precos revestimento        (default: 20)
  --precos-retif  precos retificacao, virgula               (default: 800)
  --tolerancia-retif pct tolerancia precos retificacao      (default: 25)
  --incluir-media inclui MEDIA em troca_modulo.csv

Logica de confianca:
  ALTA  = "MAINTACKER" no Texto breve E verbo de troca (TROCA/SUBSTITUIR/RETIFIC...)
  MEDIA = "MAINTACKER" em algum campo sem verbo de troca claro
  BAIXA = apenas price match, sem MAINTACKER

Coluna "tipo" no CSV de saida:
  revestimento = custo na faixa de revestimento (default ~3000) ou keyword REVESTIMENTO
  retificacao  = custo na faixa de retificacao  (default ~800)  ou keyword RETIFIC
  indefinido   = nao foi possivel determinar pelo custo ou texto
"""

import argparse
import re
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

import pandas as pd

# ---- Colunas exatas do export IW38 ------------------------------------------
DATE_COL  = "Data-base do íncio"   # detectado dinamicamente
CUSTO_COL = "Custos tot.reais"
LOCAL_COL = "Local de instalação"

TEXTO_COLS_CANDIDATOS = [
    "Texto breve",
    "Denominação do loc.instalação",
    "Campo de ordenação",
    "Denominação do objeto técnico",
]

# ---- Padroes -----------------------------------------------------------------
PAT_TROCA  = re.compile(r"TROCA|TROCAR|SUBSTITUIR|SUBSTITU|RETIFIC", re.IGNORECASE)
PAT_MAINT  = re.compile(r"MAINTACKER|MAINT\s*TACKER", re.IGNORECASE)
PAT_RETIF  = re.compile(r"RETIFIC", re.IGNORECASE)
PAT_REVEST = re.compile(r"REVESTIMENTO|REVESTIR|REVEST", re.IGNORECASE)


def build_ranges(precos_str, tol):
    result = []
    for p in precos_str.split(","):
        p = float(p.strip())
        d = p * tol / 100.0
        result.append((p - d, p + d))
    return result


def price_ok(val, ranges):
    try:
        v = float(val)
    except (TypeError, ValueError):
        return False
    return any(lo <= v <= hi for lo, hi in ranges)


def get_all_text(row, cols):
    parts = []
    for c in cols:
        v = row.get(c, "")
        s = str(v) if (v and str(v) != "nan") else ""
        parts.append(s)
    return " | ".join(parts)


def classify(row, ranges_rev, ranges_retif, text_cols):
    # Prioridade: ALTA > MEDIA > BAIXA. Só o primeiro bloco que casar é retornado.
    # ALTA exige MAINTACKER + verbo de troca ambos no "Texto breve" (campo mais confiável).
    breve   = str(row.get("Texto breve", "") or "")
    all_txt = get_all_text(row, text_cols)

    mt_breve = bool(PAT_MAINT.search(breve))
    mt_any   = bool(PAT_MAINT.search(all_txt))
    troca    = bool(PAT_TROCA.search(breve))
    price_r  = price_ok(row.get(CUSTO_COL), ranges_rev)
    price_rt = price_ok(row.get(CUSTO_COL), ranges_retif)

    if mt_breve and troca:
        m = PAT_TROCA.search(breve)
        return "ALTA", "MAINTACKER + '%s' em Texto breve | custo=%s" % (m.group(), row.get(CUSTO_COL))
    if mt_any:
        return "MEDIA", "MAINTACKER sem verbo de troca | texto='%s' | custo=%s" % (breve[:60], row.get(CUSTO_COL))
    if price_r or price_rt:
        return "BAIXA", "price_match custo=%s | texto='%s'" % (row.get(CUSTO_COL, "?"), breve[:60])
    return "NONE", ""


def inferir_tipo(row, ranges_rev, ranges_retif, texto_real):
    """Retorna 'revestimento', 'retificacao' ou 'indefinido' para cada evento."""
    breve = str(row.get(texto_real, "") or "")
    custo = row.get(CUSTO_COL)

    if bool(PAT_RETIF.search(breve)):
        return "retificacao"
    if bool(PAT_REVEST.search(breve)):
        return "revestimento"
    if price_ok(custo, ranges_retif):
        return "retificacao"
    if price_ok(custo, ranges_rev):
        return "revestimento"
    return "indefinido"


def main():
    p = argparse.ArgumentParser(
        description="Extrai datas de troca do conjunto maintacker do IW38.")
    p.add_argument("--iw38",             default="iw38.csv")
    p.add_argument("--maquina",          default="7320-IC-I1-15")
    p.add_argument("--out",              default=".")
    p.add_argument("--precos",           default="2400,2500,3000")
    p.add_argument("--tolerancia",       type=float, default=20.0)
    p.add_argument("--precos-retif",     default="800")
    p.add_argument("--tolerancia-retif", type=float, default=25.0)
    p.add_argument("--incluir-media",    action="store_true")
    args = p.parse_args()

    out_dir     = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ranges_rev  = build_ranges(args.precos,      args.tolerancia)
    ranges_retif = build_ranges(args.precos_retif, args.tolerancia_retif)

    # 1. Carregar
    print("\n[1/5] Carregando %s ..." % args.iw38)
    try:
        df = pd.read_csv(args.iw38, index_col=0, low_memory=False)
    except FileNotFoundError:
        sys.exit("ERRO: arquivo nao encontrado -> %s" % args.iw38)
    print("      %d registros carregados." % len(df))

    # Detectar colunas reais (tolerante a versoes do export)
    date_real  = next((c for c in df.columns if "data-base" in c.lower()), None)
    local_real = next((c for c in df.columns if "local de inst" in c.lower()), None)
    custo_real = next((c for c in df.columns if "tot.reais" in c.lower()), None)
    texto_real = next((c for c in df.columns if "texto breve" in c.lower()), None)
    denom_loc  = next((c for c in df.columns if "denominacao do loc" in c.lower() or
                       "denomina" in c.lower() and "loc.inst" in c.lower()), None)
    denom_obj  = next((c for c in df.columns if "objeto" in c.lower() and "tecnico" in c.lower() or
                       "objeto t" in c.lower()), None)

    missing = [n for n, v in [("data-base", date_real), ("local de inst.", local_real),
                               ("custos tot.reais", custo_real), ("texto breve", texto_real)] if not v]
    if missing:
        sys.exit("ERRO: colunas nao encontradas: %s\nPresentes: %s" % (missing, list(df.columns)))

    text_cols = [c for c in TEXTO_COLS_CANDIDATOS if c in df.columns]
    if texto_real and texto_real not in text_cols:
        text_cols.insert(0, texto_real)

    # 2. Filtrar por maquina
    print("\n[2/5] Filtrando por maquina: %s" % args.maquina)
    df_maq = df[df[local_real].str.startswith(args.maquina, na=False)].copy()
    print("      %d registros para esta maquina." % len(df_maq))
    if df_maq.empty:
        sys.exit("ERRO: nenhum registro para este prefixo.")

    # 3. Classificar confianca
    print("\n[3/5] Classificando eventos ...")
    resultados = df_maq.apply(lambda r: classify(r, ranges_rev, ranges_retif, text_cols), axis=1)
    df_maq = df_maq.copy()
    df_maq["CONFIANCA"] = [r[0] for r in resultados]
    df_maq["RAZAO"]     = [r[1] for r in resultados]

    df_match = df_maq[df_maq["CONFIANCA"] != "NONE"].copy()
    counts = df_match["CONFIANCA"].value_counts()
    print("      ALTA  : %4d eventos  <- usados em troca_modulo.csv" % counts.get("ALTA", 0))
    print("      MEDIA : %4d eventos  <- auditoria manual recomendada" % counts.get("MEDIA", 0))
    print("      BAIXA : %4d eventos  <- so preco (ruido provavel)" % counts.get("BAIXA", 0))

    # 4. Inferir tipo (revestimento / retificacao)
    print("\n[4/5] Inferindo tipo de servico ...")
    df_match["TIPO"] = df_match.apply(
        lambda r: inferir_tipo(r, ranges_rev, ranges_retif, texto_real), axis=1)
    tipo_counts = df_match["TIPO"].value_counts()
    for tipo, n in tipo_counts.items():
        print("      %-14s: %d eventos" % (tipo, n))

    df_match[date_real] = pd.to_datetime(df_match[date_real], errors="coerce")
    df_match = df_match.sort_values(date_real)

    # 5. Salvar
    print("\n[5/5] Salvando resultados em %s ..." % out_dir)

    niveis = ["ALTA", "MEDIA"] if args.incluir_media else ["ALTA"]
    df_conf = df_match[df_match["CONFIANCA"].isin(niveis)]

    # troca_modulo.csv — data + tipo
    datas = (df_conf[[date_real, "TIPO"]].dropna(subset=[date_real])
             .drop_duplicates(subset=[date_real])
             .sort_values(date_real)
             .assign(**{date_real: lambda x: x[date_real].dt.strftime("%Y-%m-%d")})
             .rename(columns={date_real: "Data-base do inicio", "TIPO": "tipo"})
             .reset_index(drop=True))
    saida_datas = out_dir / "troca_modulo.csv"
    datas.to_csv(saida_datas, index=False)
    print("      [OK] troca_modulo.csv        -> %d datas" % len(datas))

    # troca_modulo_audit.csv
    audit_cols_ok = [c for c in [date_real, "Ordem", texto_real, local_real,
                                  denom_loc, denom_obj, custo_real, "CONFIANCA", "TIPO", "RAZAO"]
                     if c and c in df_match.columns]
    saida_audit = out_dir / "troca_modulo_audit.csv"
    df_match[audit_cols_ok].to_csv(saida_audit, index=True, encoding="utf-8-sig")
    print("      [OK] troca_modulo_audit.csv  -> %d linhas" % len(df_match))

    # Resumo no terminal
    SEP = "=" * 68
    print("\n" + SEP)
    print("  DATAS DE TROCA -- CONFIANCA ALTA  (-> troca_modulo.csv)")
    print(SEP)
    df_alta = df_match[df_match["CONFIANCA"] == "ALTA"]
    print("  %-14s  %-14s  %12s  %s" % ("Data", "Tipo", "Custo (R$)", "Texto breve"))
    print("  %s  %s  %s  %s" % ("-"*14, "-"*14, "-"*12, "-"*30))
    for _, row in df_alta[[date_real, "TIPO", custo_real, texto_real]].iterrows():
        dt = str(row[date_real])[:10] if pd.notna(row[date_real]) else "--"
        tp = str(row["TIPO"])
        try:
            cs = "%12.2f" % float(row[custo_real])
        except (TypeError, ValueError):
            cs = "           --"
        tx = str(row[texto_real])[:30]
        print("  %-14s  %-14s  %s  %s" % (dt, tp, cs, tx))
    print(SEP)

    if counts.get("MEDIA", 0) > 0:
        print("\n  CONFIANCA MEDIA (revisar antes de incluir):")
        df_med = df_match[df_match["CONFIANCA"] == "MEDIA"]
        for _, row in df_med[[date_real, "TIPO", custo_real, texto_real]].iterrows():
            dt = str(row[date_real])[:10] if pd.notna(row[date_real]) else "--"
            tp = str(row["TIPO"])
            try:
                cs = "%10.2f" % float(row[custo_real])
            except (TypeError, ValueError):
                cs = "         --"
            tx = str(row[texto_real])[:36]
            print("    %s  %-14s  %s  %s" % (dt, tp, cs, tx))
        print("  -> Use --incluir-media para incluir no troca_modulo.csv")

    print("\n  Arquivos gerados:")
    print("    %s" % saida_datas)
    print("    %s\n" % saida_audit)


if __name__ == "__main__":
    main()
