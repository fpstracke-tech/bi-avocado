#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
etl_comexstat.py — exportação brasileira de abacate por destino  →  `comexstat`
==============================================================================
BI Avocado — TFruits

Espelho da query `Comexstat` do Power BI: API pública do MDIC
(api-comexstat.mdic.gov.br/general), fluxo exportação, NCM 0804.40.00
(abacates frescos ou secos), detalhe por país, mês a mês, em kg. Aqui também
entra o FOB em US$, que a mesma chamada devolve sem custo.

Containers seguem a medida do relatório: CEILING(kg / 23.760, 1), ou seja,
contêiner reefer de 40' com 23.760 kg líquidos, arredondado para cima por linha
país × mês (é o que o DAX `Total Containers` faz no nível da célula).

O PBI pedia só o ano corrente, com o período "2026-01 a 2026-12" escrito na
query: em janeiro a página teria virado um ano vazio até alguém editar. Aqui o
ano corrente e o anterior vão sempre (o anterior porque o MDIC revisa números
retroativamente nos primeiros meses), e `--desde AAAA` faz backfill.

Sem chave e sem cadastro. A API devolve "The service is overloaded" com
alguma frequência: retry com espera crescente.

Uso:
    python etl_comexstat.py
    python etl_comexstat.py --desde 2023
    python etl_comexstat.py --dry-run
"""
from __future__ import annotations

import math
import sys
import time
from datetime import date, datetime, timezone

import requests

import supabase_upsert

API = "https://api-comexstat.mdic.gov.br/general"
NCM = "08044000"
KG_POR_CONTAINER = 23760


def consulta_ano(ano: int, tentativas: int = 6) -> list[dict]:
    corpo = {
        "flow": "export", "monthDetail": True,
        "period": {"from": f"{ano}-01", "to": f"{ano}-12"},
        "filters": [{"filter": "ncm", "values": [NCM]}],
        "details": ["country"],
        "metrics": ["metricKG", "metricFOB"],
    }
    ultimo = None
    for t in range(tentativas):
        try:
            r = requests.post(API, json=corpo, timeout=120)
            j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
            if r.status_code == 200 and isinstance(j.get("data"), dict):
                return j["data"].get("list") or []
            ultimo = f"HTTP {r.status_code}: {(j.get('reason') or r.text)[:160]}"
        except (requests.RequestException, ValueError) as e:
            ultimo = str(e)[:160]
        time.sleep(10 * (t + 1))
    raise RuntimeError(f"Comexstat {ano}: {ultimo}")


def main() -> int:
    dry = "--dry-run" in sys.argv
    hoje = date.today()
    desde = hoje.year - 1
    if "--desde" in sys.argv:
        desde = int(sys.argv[sys.argv.index("--desde") + 1])

    print(f"ETL Comexstat — NCM {NCM} · {desde} a {hoje.year} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)

    agora = datetime.now(timezone.utc).isoformat()
    linhas = []
    for ano in range(desde, hoje.year + 1):
        bruto = consulta_ano(ano)
        for x in bruto:
            kg = float(x.get("metricKG") or 0)
            linhas.append({
                "ano": int(x["year"]), "mes": int(x["monthNumber"]),
                "pais_destino": x["country"], "ncm": NCM,
                "peso_kg": round(kg, 2),
                "valor_fob_usd": round(float(x.get("metricFOB") or 0), 2),
                "containers": math.ceil(kg / KG_POR_CONTAINER) if kg > 0 else 0,
                "extracted_at": agora,
            })
        meses = sorted({int(x["monthNumber"]) for x in bruto})
        ctn = sum(l["containers"] for l in linhas if l["ano"] == ano)
        print(f"  {ano}: {len(bruto)} linhas país×mês · meses {meses[:1]}..{meses[-1:]} · "
              f"{ctn} containers", flush=True)
        time.sleep(3)

    if not linhas:
        print("  ERRO: nenhuma linha devolvida")
        return 1

    # o ano corrente sem nenhum mês é sinal de API quebrada, exceto em janeiro
    # (o MDIC publica o mês anterior por volta do dia 5)
    if hoje.month > 2 and not any(l["ano"] == hoje.year for l in linhas):
        print(f"  ERRO: {hoje.year} sem nenhum mês publicado")
        return 1

    if dry:
        print("  --dry-run: nada foi gravado.")
        return 0

    r = supabase_upsert.upsert("comexstat", linhas, on_conflict="ano,mes,pais_destino,ncm")
    if r.get("errors"):
        print(f"  ERRO no upsert: {r['errors']}")
        return 1
    print(f"  OK: {r.get('inserted', len(linhas))} registros")
    return 0


if __name__ == "__main__":
    sys.exit(main())
