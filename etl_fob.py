#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
etl_fob.py — preço de exportação FOB/FOT mensal  →  `fob_mensal`
===============================================================
BI Avocado — TFruits

Alimenta o bloco "Preço de exportação" da página Panorama Mundial. O pedido
da TFruits foi comparar os mercados no mesmo incoterm; os preços semanais do
BI são de atacado e importação, então o preço de exportação vem de bases de
comércio exterior, que são mensais:

1. Brasil (Comexstat/MDIC, sem chave)
   Exportação de NCM 0804.40.00 por via de transporte, valor FOB em US$ e kg.
   Grava uma linha por via e uma linha TODAS. Via rodoviária sai como FOT:
   o valor declarado é o mesmo conceito, com a mercadoria entregue no
   caminhão na fronteira (Argentina, Uruguai, Paraguai).

2. Concorrentes (US Census, chave gratuita em CENSUS_API_KEY)
   Importações dos EUA de HS 080440 por país de origem. O "customs value"
   exclui frete, seguro e tarifas americanas, isto é, é o valor no porto de
   origem: o FOB do exportador. Dá México, Peru, Chile, Colômbia, República
   Dominicana e os demais que embarcam para os EUA.
   Sem a chave, essa parte é pulada com aviso e o job segue verde.

Ano corrente e anterior sempre (as duas bases revisam meses recentes);
--desde AAAA faz backfill.

Uso:
    python etl_fob.py
    python etl_fob.py --desde 2024
    python etl_fob.py --dry-run
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timezone

import requests

import supabase_upsert

COMEX_API = "https://api-comexstat.mdic.gov.br/general"
NCM = "08044000"
CENSUS_API = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
HS6 = "080440"

# Origens que valem para o painel, nome do Census -> nome em português.
# Lista fechada de propósito: o Census também devolve agregados (regiões,
# blocos, "TOTAL FOR ALL COUNTRIES") que não podem virar país.
CENSUS_PAISES = {
    "MEXICO": "México",
    "PERU": "Peru",
    "CHILE": "Chile",
    "COLOMBIA": "Colômbia",
    "DOMINICAN REPUBLIC": "República Dominicana",
    "ECUADOR": "Equador",
    "GUATEMALA": "Guatemala",
    "NEW ZEALAND": "Nova Zelândia",
    "ISRAEL": "Israel",
    "SOUTH AFRICA": "África do Sul",
    "KENYA": "Quênia",
    "MOROCCO": "Marrocos",
    "BRAZIL": "Brasil",
}

VIAS = {"MARITIMA": "FOB", "AEREA": "FOB", "RODOVIARIA": "FOT"}


# ── BRASIL · COMEXSTAT ───────────────────────────────────────────────────────

def comex_ano(ano: int, tentativas: int = 6) -> list[dict]:
    corpo = {
        "flow": "export", "monthDetail": True,
        "period": {"from": f"{ano}-01", "to": f"{ano}-12"},
        "filters": [{"filter": "ncm", "values": [NCM]}],
        "details": ["via"],
        "metrics": ["metricKG", "metricFOB"],
    }
    ultimo = None
    for t in range(tentativas):
        try:
            r = requests.post(COMEX_API, json=corpo, timeout=120)
            j = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
            if r.status_code == 200 and isinstance(j.get("data"), dict):
                return j["data"].get("list") or []
            ultimo = f"HTTP {r.status_code}: {(j.get('reason') or r.text)[:160]}"
        except (requests.RequestException, ValueError) as e:
            ultimo = str(e)[:160]
        time.sleep(10 * (t + 1))
    raise RuntimeError(f"Comexstat {ano}: {ultimo}")


def linhas_brasil(desde: int, ate: int, agora: str) -> list[dict]:
    saida = []
    for ano in range(desde, ate + 1):
        bruto = comex_ano(ano)
        total: dict[int, list[float]] = {}
        for x in bruto:
            mes = int(x["monthNumber"])
            kg = float(x.get("metricKG") or 0)
            fob = float(x.get("metricFOB") or 0)
            t = total.setdefault(mes, [0.0, 0.0])
            t[0] += kg
            t[1] += fob
            via = (x.get("via") or "").upper()
            if via not in VIAS:          # VIA NAO DECLARADA etc.: só entra no total
                continue
            saida.append({"fonte": "comexstat", "origem": "Brasil", "destino": "Mundo",
                          "via": via, "incoterm": VIAS[via], "ano": ano, "mes": mes,
                          "peso_kg": round(kg, 2), "valor_usd": round(fob, 2),
                          "extracted_at": agora})
        for mes, (kg, fob) in total.items():
            saida.append({"fonte": "comexstat", "origem": "Brasil", "destino": "Mundo",
                          "via": "TODAS", "incoterm": "FOB", "ano": ano, "mes": mes,
                          "peso_kg": round(kg, 2), "valor_usd": round(fob, 2),
                          "extracted_at": agora})
        meses = sorted(total)
        print(f"  Comexstat {ano}: {len(bruto)} linhas via×mês · meses "
              f"{meses[:1]}..{meses[-1:]}", flush=True)
        time.sleep(3)
    return saida


# ── CONCORRENTES · US CENSUS ─────────────────────────────────────────────────

def census_periodo(desde: int, chave: str, tentativas: int = 4) -> list[list[str]]:
    params = {
        "get": "CTY_CODE,CTY_NAME,GEN_VAL_MO,GEN_QY1_MO,UNIT_QY1",
        "I_COMMODITY": HS6, "COMM_LVL": "HS6",
        "time": f"from {desde}-01", "key": chave,
    }
    ultimo = None
    for t in range(tentativas):
        try:
            r = requests.get(CENSUS_API, params=params, timeout=120, allow_redirects=False)
            if r.status_code == 204:
                return []
            if r.status_code in (301, 302) or "missing_key" in r.headers.get("Location", ""):
                raise PermissionError("Census recusou a chave (CENSUS_API_KEY inválida ou não ativada)")
            if r.status_code == 200:
                return r.json()
            ultimo = f"HTTP {r.status_code}: {r.text[:160]}"
        except PermissionError:
            raise
        except (requests.RequestException, ValueError) as e:
            ultimo = str(e)[:160]
        time.sleep(5 * (t + 1))
    raise RuntimeError(f"Census: {ultimo}")


def linhas_census(desde: int, agora: str) -> list[dict]:
    chave = (os.environ.get("CENSUS_API_KEY") or "").strip()
    if not chave:
        print("  AVISO: CENSUS_API_KEY ausente, FOB dos concorrentes não atualizado", flush=True)
        return []

    tabela = census_periodo(desde, chave)
    if not tabela:
        print("  AVISO: Census sem dados no período", flush=True)
        return []
    cab, dados = tabela[0], tabela[1:]
    ix = {c: i for i, c in enumerate(cab)}

    saida, unidades = [], set()
    for row in dados:
        nome = (row[ix["CTY_NAME"]] or "").strip().upper()
        if nome not in CENSUS_PAISES:
            continue
        unidades.add(row[ix["UNIT_QY1"]])
        try:
            ano, mes = (int(p) for p in row[ix["time"]].split("-"))
            valor = float(row[ix["GEN_VAL_MO"]] or 0)
            kg = float(row[ix["GEN_QY1_MO"]] or 0)
        except (ValueError, KeyError):
            continue
        saida.append({"fonte": "census", "origem": CENSUS_PAISES[nome], "destino": "EUA",
                      "via": "TODAS", "incoterm": "FOB", "ano": ano, "mes": mes,
                      "peso_kg": round(kg, 2), "valor_usd": round(valor, 2),
                      "extracted_at": agora})

    # o preço por kg só faz sentido se a quantidade vier em kg
    if unidades - {"KG"}:
        raise RuntimeError(f"Census: unidade de quantidade inesperada {sorted(unidades)}")
    meses = sorted({(l["ano"], l["mes"]) for l in saida})
    print(f"  Census: {len(saida)} linhas país×mês · "
          f"{len({l['origem'] for l in saida})} origens · "
          f"{meses[0][0]}-{meses[0][1]:02d} a {meses[-1][0]}-{meses[-1][1]:02d}"
          if meses else "  Census: nenhuma origem da lista", flush=True)
    return saida


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> int:
    dry = "--dry-run" in sys.argv
    hoje = date.today()
    desde = hoje.year - 1
    if "--desde" in sys.argv:
        desde = int(sys.argv[sys.argv.index("--desde") + 1])

    agora = datetime.now(timezone.utc).isoformat()
    print(f"ETL FOB/FOT — {desde} a {hoje.year} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)

    linhas = linhas_brasil(desde, hoje.year, agora)
    if hoje.month > 2 and not any(l["ano"] == hoje.year for l in linhas):
        print(f"  ERRO: Comexstat sem nenhum mês de {hoje.year}")
        return 1

    erro_census = None
    try:
        linhas += linhas_census(desde, agora)
    except Exception as e:                       # noqa: BLE001
        erro_census = str(e)[:200]
        print(f"  ERRO Census: {erro_census}", flush=True)

    for l in sorted((l for l in linhas if l["via"] == "TODAS" or l["incoterm"] == "FOT"),
                    key=lambda l: (l["origem"], l["incoterm"], l["ano"], l["mes"]))[-6:]:
        if l["peso_kg"]:
            print(f"    {l['origem']:<22} {l['incoterm']} {l['ano']}-{l['mes']:02d} "
                  f"{l['valor_usd']/l['peso_kg']:.2f} US$/kg", flush=True)

    if dry:
        print(f"  --dry-run: {len(linhas)} linhas, nada foi gravado.")
        return 1 if erro_census else 0

    r = supabase_upsert.upsert("fob_mensal", linhas,
                               on_conflict="fonte,origem,destino,via,ano,mes")
    if r.get("errors"):
        print(f"  ERRO no upsert: {r['errors']}")
        return 1
    print(f"  OK: {r.get('inserted', len(linhas))} registros")
    # Brasil gravado; falha do Census ainda derruba o job para abrir issue
    return 1 if erro_census else 0


if __name__ == "__main__":
    sys.exit(main())
