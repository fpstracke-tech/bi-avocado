"""
etl_brasil_precos.py — Preço diário do abacate Hass no atacado brasileiro
=========================================================================
BI Avocado — TFruits

Fonte:  noticiasagricolas.com.br/cotacoes/frutas/abacate-ceasas
Alvo:   Supabase, tabela `brasil_precos` (upsert por data+mercado+produto)
        e, opcionalmente, um CSV local de backup.

Adaptado do `abacate_brasil_hass_collector.py`. A lógica de scraping e o
filtro de produto são os mesmos — o que muda é o destino.

Duas diferenças de comportamento em relação ao coletor antigo, ambas de
propósito:

  1. Não é mais append-only com dedupe por chave. Agora é UPSERT. Se a fonte
     corrigir o preço de um dia já gravado, a correção entra. O modelo antigo
     ignorava qualquer alteração posterior — foi o que produziu a divergência
     da S23/2026 (relatório com 6,52 quando o máximo real da semana é 6,60).

  2. O caminho de saída não é mais absoluto na máquina do Phil. Roda no
     GitHub Actions sem alteração.

Uso:
    python etl_brasil_precos.py              # scrape + upsert no Supabase
    python etl_brasil_precos.py --csv        # também grava/atualiza o CSV local
    python etl_brasil_precos.py --dry-run    # só mostra o que faria
"""

import csv
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

import supabase_upsert

URL = "https://www.noticiasagricolas.com.br/cotacoes/frutas/abacate-ceasas"

BASE_DIR        = Path(__file__).parent
CSV_PATH        = BASE_DIR / "abacate_preco_brasil_agrolink.csv"
DEBUG_HTML_PATH = BASE_DIR / "_debug_noticias_agricolas_abacate.html"

TARGET_EXACT = "avocado/ hass/ fuerte a"     # comparação normalizada
PRODUTO_CANON = "Avocado/ Hass/ Fuerte A"
FONTE = "noticiasagricolas.com.br"
TABELA = "brasil_precos"


# ── PARSING (mesma lógica do coletor original) ────────────────────────────────
def norm(s: str) -> str:
    s = (s or "").replace(" ", " ").strip().lower()
    return re.sub(r"\s+", " ", s)


def parse_price_ptbr(x: str) -> Optional[float]:
    x = (x or "").strip()
    if not x:
        return None
    x = x.replace(".", "").replace(",", ".")
    try:
        return float(x)
    except ValueError:
        return None


def fetch_html() -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    r = requests.get(URL, headers=headers, timeout=40)
    r.raise_for_status()
    return r.text


def html_to_lines(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    return [
        ln.replace(" ", " ").strip()
        for ln in soup.get_text("\n").splitlines()
        if ln.replace(" ", " ").strip()
    ]


def extract_records(lines: List[str]) -> List[Dict]:
    """
    Captura SOMENTE Avocado/ Hass/ Fuerte A. Suporta os dois layouts da página:
      A) "Produto<TAB>13,14"
      B) "Produto" numa linha e "13,14" na linha seguinte
    """
    records: List[Dict] = []
    current_date: Optional[str] = None
    current_ceasa: Optional[str] = None

    re_fech        = re.compile(r"^Fechamento:\s*(\d{2}/\d{2}/\d{4})\s*$", re.I)
    re_ceasa       = re.compile(r"^(Ceasa\s+.+\/[A-Z]{2}|Ceagesp\/[A-Z]{2})\b", re.I)
    re_price_inline = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})")
    re_price_only   = re.compile(r"^\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*$")

    i = 0
    while i < len(lines):
        ln = lines[i]

        m = re_fech.match(ln)
        if m:
            current_date, current_ceasa = m.group(1), None
            i += 1
            continue

        if re_ceasa.match(ln):
            current_ceasa = ln.replace("*", "").strip()
            i += 1
            continue

        if not current_date or not current_ceasa or "s/ cotação" in ln.lower():
            i += 1
            continue

        # Caso A
        mp = re_price_inline.search(ln)
        if mp:
            produto = ln[: mp.start(1)].strip()
            preco = parse_price_ptbr(mp.group(1))
            if preco is not None and norm(produto) == TARGET_EXACT:
                records.append({"data_br": current_date, "mercado": current_ceasa, "preco": preco})
                i += 1
                continue

        # Caso B
        if norm(ln) == TARGET_EXACT and i + 1 < len(lines):
            mp2 = re_price_only.match(lines[i + 1])
            if mp2:
                preco = parse_price_ptbr(mp2.group(1))
                if preco is not None:
                    records.append({"data_br": current_date, "mercado": current_ceasa, "preco": preco})
                    i += 2
                    continue

        i += 1

    return records


# ── CSV DE BACKUP (opcional) ──────────────────────────────────────────────────
def sync_csv(rows: List[Dict], agora: str) -> int:
    """
    Mantém o CSV histórico no mesmo formato de sempre
    (Data, CEASA, Produto, Unidade, Preco, DataColeta), mas com semântica de
    upsert: uma chave já existente tem o preço ATUALIZADO em vez de ignorada.
    """
    campos = ["Data", "CEASA", "Produto", "Unidade", "Preco", "DataColeta"]
    existentes: Dict[tuple, Dict] = {}
    ordem: List[tuple] = []

    if CSV_PATH.exists():
        with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                k = ((r.get("Data") or "").strip(),
                     (r.get("CEASA") or "").strip(),
                     (r.get("Produto") or "").strip())
                if all(k):
                    if k not in existentes:
                        ordem.append(k)
                    existentes[k] = r

    mudou = 0
    for r in rows:
        k = (r["data_br"], r["mercado"], PRODUTO_CANON)
        novo = {"Data": r["data_br"], "CEASA": r["mercado"], "Produto": PRODUTO_CANON,
                "Unidade": "kg", "Preco": r["preco"], "DataColeta": agora}
        anterior = existentes.get(k)
        if anterior is None:
            ordem.append(k)
            existentes[k] = novo
            mudou += 1
        elif str(anterior.get("Preco", "")).strip() != str(r["preco"]):
            existentes[k] = novo
            mudou += 1

    if mudou:
        with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            w.writeheader()
            for k in ordem:
                w.writerow({c: existentes[k].get(c, "") for c in campos})
    return mudou


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main() -> int:
    dry = "--dry-run" in sys.argv
    com_csv = "--csv" in sys.argv

    print(f"ETL Preços Brasil — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    html = fetch_html()
    if os.environ.get("SALVAR_DEBUG_HTML"):
        DEBUG_HTML_PATH.write_text(html, encoding="utf-8")

    scraped = extract_records(html_to_lines(html))
    print(f"  Hass/Fuerte A encontrados na página: {len(scraped)}")
    if not scraped:
        print("  ERRO: nenhum registro. A página provavelmente mudou de layout.")
        return 1

    # extracted_at vai NO PAYLOAD de propósito. Sem ele, o upsert
    # merge-duplicates só atualiza as colunas enviadas e o extracted_at fica
    # congelado na primeira carga — a view v_ultima_atualizacao passa a mentir
    # e um cron morto fica indistinguível de um cron que rodou sem novidade.
    agora = datetime.now(timezone.utc).isoformat()

    registros = []
    for r in scraped:
        d, m, a = r["data_br"].split("/")
        registros.append({
            "data":         f"{a}-{m}-{d}",
            "mercado":      r["mercado"],
            "produto":      PRODUTO_CANON,
            "unidade":      "kg",
            "preco_kg":     r["preco"],
            "fonte":        FONTE,
            "extracted_at": agora,
        })

    datas = sorted({x["data"] for x in registros})
    print(f"  Datas cobertas: {datas[0]} a {datas[-1]} ({len(datas)} dias)")
    print(f"  Mercados: {', '.join(sorted({x['mercado'] for x in registros}))}")

    if dry:
        for x in registros:
            print(f"    {x['data']}  {x['mercado']:26s}  R$ {x['preco_kg']:.2f}")
        print("  --dry-run: nada foi gravado.")
        return 0

    res = supabase_upsert.upsert(TABELA, registros, on_conflict="data,mercado,produto")
    print(f"  Supabase {TABELA}: {res['inserted']} registros enviados (extracted_at={agora})")
    if res["errors"]:
        for e in res["errors"]:
            print(f"  ERRO no lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1

    if com_csv:
        n = sync_csv(scraped, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print(f"  CSV de backup: {n} linhas criadas ou atualizadas")

    return 0


if __name__ == "__main__":
    sys.exit(main())
