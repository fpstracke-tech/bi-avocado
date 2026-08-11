"""
import_historico_brasil.py — carga inicial do histórico 2024–2025
=================================================================
BI Avocado — TFruits

Lê o `Brasil_historico.xlsx` (colunas Data, Preco_Medio_Diario) e carrega em
`brasil_precos`. Roda UMA vez, na montagem do banco — depois é só o ETL diário.

O arquivo não traz produto nem praça. O Phil confirmou em 11/08/2026 que é
Hass / Fuerte A no Ceagesp/SP, a mesma série que o coletor pega hoje — por isso
os registros entram com esse produto e mercado, e a curva 2024→2026 é contínua.
Se essa premissa cair, é aqui que se corrige.

Carrega também o histórico de 2026 a partir do `abacate_preco_brasil_agrolink.csv`,
porque a página da Notícias Agrícolas mantém apenas os ~10 últimos fechamentos —
o ETL diário nunca recuperaria fevereiro a agosto de 2026 sozinho. Só as linhas
de Hass entram; as 23 linhas legadas de `Abacate 1` de janeiro ficam de fora.

Uso:
    python import_historico_brasil.py --arquivo Brasil_historico.xlsx
    python import_historico_brasil.py --csv abacate_preco_brasil_agrolink.csv
    python import_historico_brasil.py --arquivo ... --csv ...     # os dois
    python import_historico_brasil.py --arquivo ... --dry-run
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import supabase_upsert

TABELA   = "brasil_precos"
MERCADO  = "Ceagesp/SP"
PRODUTO  = "Avocado/ Hass/ Fuerte A"
FONTE    = "Brasil_historico.xlsx"

# Âncoras da auditoria de 11/08/2026 (semana no padrão WEEKNUM do Excel).
# Se alguma não fechar, a planilha mudou ou está sendo lida errado.
ANCORAS = [
    ("2024-11-10", 23.33, "pico de 2024, S46 no relatório"),
    ("2025-04-07", 21.64, "pico de 2025, S15"),
    ("2024-12-05", 18.57, "S49/2024"),
    ("2024-12-10", 16.81, "S50/2024"),
]


def weeknum_excel(d: pd.Timestamp) -> int:
    """Mesma regra da função weeknum_excel() no Postgres: semana começa no domingo."""
    dow_jan1 = (pd.Timestamp(d.year, 1, 1).weekday() + 1) % 7   # 0 = domingo
    return int((d.timetuple().tm_yday - 1 + dow_jan1) // 7) + 1


def carrega_csv_2026(caminho: Path, dry: bool) -> int:
    """Backfill de 2026 a partir do CSV do coletor. Só Hass."""
    import csv as _csv
    print(f"\n2026 — {caminho.name}")
    linhas = []
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        for r in _csv.DictReader(f):
            produto = (r.get("Produto") or "").strip()
            if "hass" not in produto.lower():        # descarta as legadas de janeiro
                continue
            d, m, a = r["Data"].split("/")
            linhas.append({
                "data": f"{a}-{m}-{d}",
                "mercado": (r.get("CEASA") or "").strip(),
                "produto": produto,
                "unidade": (r.get("Unidade") or "kg").strip(),
                "preco_kg": round(float(r["Preco"]), 4),
                "fonte": "noticiasagricolas.com.br (backfill do CSV)",
            })
    if not linhas:
        print("  nenhuma linha de Hass no CSV.")
        return 0
    datas = sorted(x["data"] for x in linhas)
    print(f"  {len(linhas)} registros de Hass, de {datas[0]} a {datas[-1]}")
    print(f"  mercados: {', '.join(sorted({x['mercado'] for x in linhas}))}")
    ignoradas = 0
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        ignoradas = sum(1 for r in _csv.DictReader(f) if "hass" not in (r.get("Produto") or "").lower())
    print(f"  {ignoradas} linhas legadas descartadas (não são Hass)")
    if dry:
        print("  --dry-run: nada gravado.")
        return 0
    res = supabase_upsert.upsert(TABELA, linhas, on_conflict="data,mercado,produto")
    print(f"  Supabase {TABELA}: {res['inserted']} de {len(linhas)} registros enviados")
    if res["errors"]:
        for e in res["errors"]:
            print(f"  ERRO no lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1
    return 0


def main() -> int:
    dry = "--dry-run" in sys.argv

    if "--csv" in sys.argv:
        csv_path = Path(sys.argv[sys.argv.index("--csv") + 1])
        if not csv_path.exists():
            print(f"ERRO: não encontrei {csv_path}")
            return 1

    if "--arquivo" not in sys.argv:
        if "--csv" in sys.argv:
            print(f"Carga Brasil — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
            return carrega_csv_2026(Path(sys.argv[sys.argv.index("--csv") + 1]), dry)
        print("ERRO: informe --arquivo (xlsx 2024-2025) e/ou --csv (2026).")
        return 1

    caminho = Path(sys.argv[sys.argv.index("--arquivo") + 1])
    if not caminho.exists():
        print(f"ERRO: não encontrei {caminho}. Passe o caminho com --arquivo.")
        return 1

    print(f"Carga do histórico Brasil — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\n2024-2025 — {caminho.name}")
    df = pd.read_excel(caminho)
    df["dt"] = pd.to_datetime(df["Data"], format="%d-%m-%Y")
    df = df.dropna(subset=["Preco_Medio_Diario"]).sort_values("dt")

    print(f"  {len(df)} dias, de {df['dt'].min().date()} a {df['dt'].max().date()}")
    print(f"  por ano: {df['dt'].dt.year.value_counts().sort_index().to_dict()}")
    print(f"  preço: R$ {df['Preco_Medio_Diario'].min():.2f} a R$ {df['Preco_Medio_Diario'].max():.2f}")

    # ── conferência das âncoras ───────────────────────────────────────────
    print("  conferindo âncoras da auditoria:")
    falhou = False
    for data_iso, esperado, nota in ANCORAS:
        alvo = pd.Timestamp(data_iso)
        linha = df.loc[df["dt"] == alvo]
        if linha.empty:
            print(f"    FALHA  {data_iso}: dia não existe na planilha ({nota})")
            falhou = True
            continue
        got = round(float(linha["Preco_Medio_Diario"].iloc[0]), 2)
        sem = weeknum_excel(alvo)
        ok = abs(got - esperado) < 0.005
        print(f"    {'ok   ' if ok else 'FALHA'}  {data_iso} = R$ {got:.2f} (esperado {esperado:.2f}) "
              f"-> S{sem}  | {nota}")
        if not ok:
            falhou = True
    if falhou:
        print("  Âncoras não fecharam. Abortando para não gravar dado errado.")
        return 1

    registros = [
        {"data": r.dt.strftime("%Y-%m-%d"), "mercado": MERCADO, "produto": PRODUTO,
         "unidade": "kg", "preco_kg": round(float(r.Preco_Medio_Diario), 4), "fonte": FONTE}
        for r in df.itertuples()
    ]

    if dry:
        print(f"  --dry-run: {len(registros)} registros prontos, nada gravado.")
        print(f"  amostra: {registros[:2]}")
        if "--csv" in sys.argv:
            carrega_csv_2026(Path(sys.argv[sys.argv.index("--csv") + 1]), dry)
        return 0

    res = supabase_upsert.upsert(TABELA, registros, on_conflict="data,mercado,produto")
    print(f"  Supabase {TABELA}: {res['inserted']} de {len(registros)} registros enviados")
    if res["errors"]:
        for e in res["errors"]:
            print(f"  ERRO no lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1

    if "--csv" in sys.argv:
        rc = carrega_csv_2026(Path(sys.argv[sys.argv.index("--csv") + 1]), dry)
        if rc:
            return rc

    print("\n  Confira no SQL Editor:")
    print("    SELECT ano, semana, preco_max FROM v_brasil_precos_semanal")
    print("     WHERE (ano, semana) IN ((2024,1),(2024,46),(2024,49),(2025,15)) ORDER BY ano, semana;")
    print("    esperado: 6.54 | 23.33 | 18.57 | 21.64")
    return 0


if __name__ == "__main__":
    sys.exit(main())
