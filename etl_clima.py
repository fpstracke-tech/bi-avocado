#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
etl_clima.py — previsão do tempo nas regiões produtoras  →  Supabase `clima`
============================================================================
BI Avocado — TFruits

Substitui as consultas `Weather_Avocado` e `Weather_Local` do Power BI, que
chamavam OpenWeather (previsão 5 dias em blocos de 3 h) e HG Brasil direto de
dentro do relatório, com a chave de API gravada na URL da query. As cidades e
regiões são as MESMAS do relatório, inclusive o rótulo exibido (`Cidade_visual`
no PBI, `regiao` aqui): Rancagua aparece como O'Higgins, Netanya como Sharon
Plain, e assim por diante.

Fonte: Open-Meteo (api.open-meteo.com). Sem chave, sem cadastro, sem cota que
vença: é o que deixa este coletor sem dependência manual. Previsão diária de
7 dias por coordenada, numa chamada só para todos os pontos.

O que muda em relação ao PBI, de propósito:
  * Coordenada fixa em vez de busca por nome. "q=Michoacan,MX" ou
    "q=Western Cape,ZA" no OpenWeather caía no ponto que o geocoder escolhesse;
    aqui cada região aponta para o polo produtor (Uruapan, Tzaneen, George...).
  * Diário em vez de 3 h: o painel só mostra acumulado de 7 dias, máxima e
    chuva por dia; 3 h não acrescentava leitura nenhuma.
  * Histórico acumula: cada rodada regrava os 7 dias à frente (upsert por
    data + cidade), e os dias que passaram ficam com a última previsão feita.

Uso:
    python etl_clima.py
    python etl_clima.py --dry-run
    python etl_clima.py --dias 10
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import requests

import supabase_upsert

API = "https://api.open-meteo.com/v1/forecast"
FONTE = "Open-Meteo"

# (continente, país, cidade no PBI, rótulo exibido, lat, lon, grupo)
# grupo "produtor" = Weather_Avocado · "brasil" = Weather_Local
PONTOS = [
    ("América do Sul", "Peru", "Ica", "Ica", -14.07, -75.73, "produtor"),
    ("América do Sul", "Peru", "Lima", "Lima", -12.05, -77.04, "produtor"),
    ("América do Sul", "Peru", "La Libertad", "La Libertad", -8.11, -79.03, "produtor"),
    ("África", "Marrocos", "Agadir", "Souss-Massa", 30.43, -9.60, "produtor"),
    ("África", "Marrocos", "Kenitra", "Gharb", 34.26, -6.58, "produtor"),
    ("África", "Marrocos", "Larache", "Loukkos", 35.19, -6.16, "produtor"),
    ("Oriente Médio", "Israel", "Netanya", "Sharon Plain", 32.33, 34.86, "produtor"),
    ("Oriente Médio", "Israel", "Hadera", "Zikhron Yaakov", 32.57, 34.95, "produtor"),
    ("Oriente Médio", "Israel", "Nahariya", "Western Galilee", 33.01, 35.10, "produtor"),
    ("Oriente Médio", "Israel", "Tiberias", "Beit Shean", 32.50, 35.50, "produtor"),
    ("América do Sul", "Colômbia", "Antioquia", "Antioquia", 6.15, -75.37, "produtor"),
    ("América do Sul", "Colômbia", "Ibagué", "Tolima", 4.44, -75.23, "produtor"),
    ("América do Sul", "Colômbia", "Caldas", "Caldas", 5.07, -75.52, "produtor"),
    ("América do Sul", "Colômbia", "Santander", "Santander", 7.12, -73.12, "produtor"),
    ("América do Sul", "Chile", "Valparaiso", "Valparaiso", -32.88, -71.25, "produtor"),
    ("América do Sul", "Chile", "Santiago", "Santiago", -33.45, -70.67, "produtor"),
    ("América do Sul", "Chile", "Coquimbo", "Coquimbo", -29.95, -71.34, "produtor"),
    ("América do Sul", "Chile", "Rancagua", "O’Higgins", -34.17, -70.74, "produtor"),
    ("América do Norte", "México", "Michoacan", "Michoacan", 19.42, -102.06, "produtor"),
    ("América do Norte", "México", "Jalisco", "Jalisco", 19.70, -103.46, "produtor"),
    ("América do Sul", "Argentina", "Tucuman", "Tucuman", -26.81, -65.22, "produtor"),
    ("América do Sul", "Argentina", "Salta", "Salta", -24.79, -65.41, "produtor"),
    ("América do Sul", "Argentina", "San Salvador de Jujuy", "San Salvador de Jujuy", -24.19, -65.30, "produtor"),
    ("Europa", "Espanha", "Malaga", "Malaga", 36.72, -4.42, "produtor"),
    ("Europa", "Espanha", "Granada", "Granada", 36.75, -3.52, "produtor"),
    ("Europa", "Espanha", "Cadiz", "Cadiz", 36.53, -6.29, "produtor"),
    ("Europa", "Portugal", "Faro", "Algarve", 37.02, -7.93, "produtor"),
    ("Europa", "Portugal", "Alentejo", "Alentejo", 38.02, -7.87, "produtor"),
    ("África", "África do Sul", "Limpopo", "Limpopo", -23.83, 30.16, "produtor"),
    ("África", "África do Sul", "Mpumalanga", "Mpumalanga", -25.47, 30.97, "produtor"),
    ("África", "África do Sul", "Durban", "Kwazulu Natal", -29.86, 31.02, "produtor"),
    ("África", "África do Sul", "Western Cape", "Western Cape", -33.96, 22.46, "produtor"),
    ("África", "Tanzânia", "Njombe", "Njombe", -9.33, 34.77, "produtor"),
    ("África", "Tanzânia", "Mbeya", "Mbeya", -8.90, 33.46, "produtor"),
    ("África", "Tanzânia", "Songwe", "Songwe", -9.11, 32.93, "produtor"),
    ("África", "Tanzânia", "Iringa", "Iringa", -7.77, 35.69, "produtor"),
    ("África", "Tanzânia", "Kilimanjaro", "Kilimanjaro", -3.35, 37.34, "produtor"),
    ("África", "Quênia", "Muranga", "Muranga", -0.72, 37.15, "produtor"),
    ("África", "Quênia", "Kiambu", "Kiambu", -1.17, 36.83, "produtor"),
    ("África", "Quênia", "Nyeri", "Nyeri", -0.42, 36.95, "produtor"),
    ("África", "Quênia", "Meru", "Meru", 0.05, 37.65, "produtor"),
    # Weather_Local (página Newsletter Brasil)
    ("América do Sul", "Brasil", "Matão", "Matão (SP)", -21.60, -48.37, "brasil"),
    ("América do Sul", "Brasil", "Bernardino de Campos", "Bernardino de Campos (SP)", -23.01, -49.47, "brasil"),
    ("América do Sul", "Brasil", "Bauru", "Bauru (SP)", -22.31, -49.06, "brasil"),
    ("América do Sul", "Brasil", "Ibiá", "Ibiá (MG)", -19.48, -46.54, "brasil"),
    ("América do Sul", "Brasil", "São Gotardo", "São Gotardo (MG)", -19.31, -46.05, "brasil"),
    ("América do Sul", "Brasil", "Sete Lagoas", "Sete Lagoas (MG)", -19.47, -44.25, "brasil"),
]

# Código WMO → descrição curta em português
WMO = {
    0: "Céu limpo", 1: "Predomínio de sol", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Nevoeiro", 48: "Nevoeiro com geada",
    51: "Garoa fraca", 53: "Garoa", 55: "Garoa forte", 56: "Garoa congelante", 57: "Garoa congelante",
    61: "Chuva fraca", 63: "Chuva", 65: "Chuva forte", 66: "Chuva congelante", 67: "Chuva congelante",
    71: "Neve fraca", 73: "Neve", 75: "Neve forte", 77: "Grãos de neve",
    80: "Pancadas fracas", 81: "Pancadas de chuva", 82: "Pancadas fortes",
    85: "Pancadas de neve", 86: "Pancadas de neve",
    95: "Trovoada", 96: "Trovoada com granizo", 99: "Trovoada com granizo",
}


def buscar(dias: int, tentativas: int = 5) -> list[dict]:
    params = {
        "latitude": ",".join(str(p[4]) for p in PONTOS),
        "longitude": ",".join(str(p[5]) for p in PONTOS),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                 "weather_code,relative_humidity_2m_mean",
        "forecast_days": dias,
        "timezone": "auto",
    }
    ultimo = None
    for t in range(tentativas):
        try:
            r = requests.get(API, params=params, timeout=90)
            if r.status_code == 200:
                j = r.json()
                return j if isinstance(j, list) else [j]
            ultimo = f"HTTP {r.status_code}: {r.text[:160]}"
        except requests.RequestException as e:
            ultimo = str(e)[:160]
        # "The service is overloaded" acontece; espera e tenta de novo
        time.sleep(5 * (t + 1))
    raise RuntimeError(f"Open-Meteo não respondeu: {ultimo}")


def num(v, casas=2):
    return None if v is None else round(float(v), casas)


def main() -> int:
    dry = "--dry-run" in sys.argv
    dias = 7
    if "--dias" in sys.argv:
        dias = int(sys.argv[sys.argv.index("--dias") + 1])

    print(f"ETL Clima — {len(PONTOS)} pontos · {dias} dias · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)

    respostas = buscar(dias)
    if len(respostas) != len(PONTOS):
        print(f"  ERRO: {len(respostas)} respostas para {len(PONTOS)} pontos")
        return 1

    agora = datetime.now(timezone.utc).isoformat()
    linhas, sem_dado = [], []
    for p, r in zip(PONTOS, respostas):
        cont, pais, cidade, regiao, lat, lon, grupo = p
        d = r.get("daily") or {}
        datas = d.get("time") or []
        if not datas:
            sem_dado.append(cidade)
            continue
        for i, dt in enumerate(datas):
            code = (d.get("weather_code") or [None] * len(datas))[i]
            linhas.append({
                "data": dt, "pais": pais, "cidade": cidade, "regiao": regiao,
                "continente": cont, "grupo": grupo, "lat": lat, "lon": lon,
                "temp_max": num(d["temperature_2m_max"][i]),
                "temp_min": num(d["temperature_2m_min"][i]),
                "chuva_mm": num(d["precipitation_sum"][i]),
                "umidade_pct": num((d.get("relative_humidity_2m_mean") or [None] * len(datas))[i], 0),
                "weather_code": code,
                "descricao": WMO.get(code, None) if code is not None else None,
                "fonte": FONTE, "extracted_at": agora,
            })

    if sem_dado:
        print(f"  AVISO: sem previsão para {', '.join(sem_dado)}")
    if not linhas:
        print("  ERRO: nenhuma linha de previsão")
        return 1

    # resumo: acumulado 7 dias por região, como a tabela do PBI
    acum = {}
    for l in linhas:
        acum.setdefault((l["pais"], l["regiao"]), 0.0)
        acum[(l["pais"], l["regiao"])] += l["chuva_mm"] or 0
    fortes = sorted(((v, k) for k, v in acum.items() if v >= 10), reverse=True)
    print(f"  {len(linhas)} linhas · {len(PONTOS) - len(sem_dado)} pontos")
    print(f"  chuva >= 10 mm em 7 dias: " +
          (", ".join(f"{k[1]} ({k[0]}) {v:.0f} mm" for v, k in fortes[:8]) or "nenhuma"))

    if dry:
        print("  --dry-run: nada foi gravado.")
        return 0

    r = supabase_upsert.upsert("clima", linhas, on_conflict="data,cidade")
    if r.get("errors"):
        if "PGRST204" in str(r["errors"]):
            print("  ERRO: a tabela clima ainda não tem as colunas da v5. Rodar "
                  "supabase_schema_v5_clima_comex.sql no SQL Editor do Supabase.")
        else:
            print(f"  ERRO no upsert: {r['errors']}")
        return 1
    print(f"  OK: {r.get('inserted', len(linhas))} registros")
    return 0


if __name__ == "__main__":
    sys.exit(main())
