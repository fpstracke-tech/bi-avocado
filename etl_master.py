"""
ETL Master — BI Avocado TFruits
===============================
Orquestra os ETLs em sequência. Cada um grava no Supabase por conta própria.

Mesmo contrato do BI Limão: um ETL marcado como `optional` pode falhar sem
derrubar a execução inteira; se um obrigatório falhar, sai com código 1 para o
GitHub Actions marcar o run como vermelho.

Uso:
    python etl_master.py                 # roda todos
    python etl_master.py --skip precos   # pula por tag

ETLs ativos:
    1. etl_brasil_precos.py  -> brasil_precos

Conforme as próximas abas forem auditadas, acrescente aqui:
    etl_chile_precos.py      -> chile_precos
    etl_argentina_precos.py  -> argentina_precos
    etl_cambio.py            -> cambio
    etl_news.py              -> news
    etl_clima.py             -> clima_forecast
    etl_comexstat.py         -> comexstat_exportacoes
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent

ETLS = [
    {"name": "Preços Brasil", "script": "etl_brasil_precos.py", "tag": "precos"},
]


def run_etl(etl: dict) -> dict:
    script = BASE_DIR / etl["script"]
    if not script.exists():
        return {"name": etl["name"], "status": "SKIP", "msg": "script não encontrado", "elapsed": 0}

    inicio = time.time()
    r = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(BASE_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    elapsed = round(time.time() - inicio, 1)

    if r.returncode == 0:
        linhas = [l for l in r.stdout.splitlines() if "Supabase" in l]
        return {"name": etl["name"], "status": "OK",
                "msg": linhas[-1].strip() if linhas else "OK", "elapsed": elapsed}

    erro = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "erro desconhecido"
    return {"name": etl["name"], "status": "ERRO", "msg": erro[:120], "elapsed": elapsed}


def main() -> None:
    skip = set()
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--skip" and i < len(sys.argv) - 1:
            skip.add(sys.argv[i + 1])
        elif arg.startswith("--skip="):
            skip.add(arg.split("=", 1)[1])

    print("=" * 62)
    print("ETL MASTER — BI Avocado TFruits")
    print(f"Início: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 62)

    resultados = []
    for etl in ETLS:
        if etl["tag"] in skip:
            print(f"\n  --  {etl['name']} — pulado")
            resultados.append({"name": etl["name"], "status": "SKIP", "msg": "", "elapsed": 0})
            continue
        print(f"\n{'-'*62}\n  >>  {etl['name']} ({etl['script']})")
        r = run_etl(etl)
        resultados.append(r)
        print(f"  {r['status']} | {r['elapsed']}s | {r['msg']}")

    ok   = sum(1 for r in resultados if r["status"] == "OK")
    err  = sum(1 for r in resultados if r["status"] == "ERRO")
    skp  = sum(1 for r in resultados if r["status"] == "SKIP")
    print(f"\n{'='*62}\nRESUMO — OK: {ok} | Erros: {err} | Pulados: {skp}")
    print(f"Fim: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if err:
        print("\nDetalhes:")
        criticos = 0
        for r in resultados:
            if r["status"] != "ERRO":
                continue
            d = next((e for e in ETLS if e["name"] == r["name"]), {})
            opcional = d.get("optional")
            print(f"  ERRO {r['name']}{' (opcional)' if opcional else ''}: {r['msg']}")
            if not opcional:
                criticos += 1
        if criticos:
            sys.exit(1)


if __name__ == "__main__":
    main()
