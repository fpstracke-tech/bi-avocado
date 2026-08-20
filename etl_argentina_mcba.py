"""
ETL Preços Argentina — Palta Hass, atacado (Mercado Central de Buenos Aires)
=============================================================================
SUBSTITUI o palta_buenosaires_collector.py. O antigo raspava
preciosdelcentral.com aceitando QUALQUER texto com "$" entre 2.000 e 15.000 em
qualquer <td>, <span> ou <div> da página, e usava min/max do que caísse na
rede. Auditoria de 11/08/2026: em 25 das 57 linhas min == max (achou um valor
só) e em 5 a razão max/min era 8,05. O range de 1,46 a 8,01 USD/kg num único
dia não era spread de mercado, era ruído de página. Fallback de 6.500 ARS
quando não achava nada.

Fonte real: o próprio Mercado Central publica o levantamento diário do
Departamento de Estadísticas y Precios, em ZIP mensal com um arquivo por dia
útil.

    https://mercadocentral.gob.ar/información/precios-mayoristas

Estrutura de cada RF<ddmmyy>.XLS (BIFF antigo, precisa de xlrd):

    ESP | VAR | PROC | ENV | KG | CAL | TAM | GRADO | MA.. | MO.. | MI.. | MAPK | MOPK | MIPK

MA/MO/MI = máximo / moda / mínimo pelo bulto; os *PK são os mesmos por quilo.
Uso os PK. A medida central do Mercado Central é a MODA, não a média — é o
preço mais frequente do dia, e é o que a praça usa como referência.

As linhas com VAR = "Prom.Esp." são subtotais da espécie e ficam fora, senão o
promedio entra duas vezes na conta.

Bônus que a fonte antiga não tinha: a coluna PROC traz a procedência
(BRASIL, CHILE, PERU, JUJUY, TUCUMAN...). Dá para ver o preço do Hass
brasileiro em Buenos Aires. Não estou explodindo por origem agora para manter
o contrato de precos_origem, mas o dado está aqui quando quiser.

Câmbio: `cambio_fx.carregar` — banco primeiro, depois bluelytics, argentinadatos
e dolarapi. Só fontes de BLUE: em Argentina o blue é a taxa economicamente
relevante para comparar preço de importação, e misturar oficial com blue na
mesma série produziria um degrau impossível de explicar depois.

Uso:
    python etl_argentina_mcba.py
    python etl_argentina_mcba.py --dry-run
    python etl_argentina_mcba.py --ano 2025
    python etl_argentina_mcba.py --sem-cache    # ignora o banco, só fonte externa
"""

import io
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone

import requests

import cambio_fx

try:
    import xlrd
except ImportError:                                   # pragma: no cover
    print("ERRO: falta o xlrd (pip install xlrd). Os arquivos do Mercado "
          "Central são .XLS BIFF antigo; openpyxl não abre.")
    raise

TABELA  = "precos_origem"
CHAVE   = "data,pais,cidade,produto"
PAIS    = "Argentina"
CIDADE  = "Buenos Aires"
PRODUTO = "Palta Hass"
PAR     = "USD/ARS blue"
FONTE   = "Mercado Central de Buenos Aires — precios mayoristas (mercadocentral.gob.ar)"

URL_LISTA = "https://mercadocentral.gob.ar/informaci%C3%B3n/precios-mayoristas"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Referer": URL_LISTA,
}

# Os nomes dos ZIP são inconsistentes de propósito nenhum: FRUTAS-MAYO-2026,
# FRUTAS_ABRIL2026_0, "FRUTAS  ENERO-26_0", FRUTA_JUNIO_2026_0,
# FRUTRAS_AGOSTO-26_0 (sic). Não dá para montar o nome — tem que ler a página.
MESES = {"ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
         "JULIO": 7, "AGOSTO": 8, "SEPTIEMBRE": 9, "SETIEMBRE": 9, "OCTUBRE": 10,
         "NOVIEMBRE": 11, "DICIEMBRE": 12}


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def listar_zips(ano: int) -> list[tuple[int, str]]:
    """[(mes, url)] dos ZIP de FRUTAS do ano pedido, lendo os href da página."""
    r = requests.get(URL_LISTA, headers=HEADERS, timeout=120)
    r.raise_for_status()
    achados = {}
    for href in re.findall(r'href="([^"]+precios_mayoristas[^"]+\.zip)"', r.text, re.I):
        nome = requests.utils.unquote(href.rsplit("/", 1)[-1]).upper()
        # HORTALIZA/HORTALIZAS fora; FRUTA, FRUTAS e o typo FRUTRAS dentro
        if "HORTALIZ" in nome or "FRUT" not in nome:
            continue
        mes = next((n for k, n in MESES.items() if k in nome), None)
        if not mes:
            continue
        m = re.search(r"(?:20)?(\d{2})(?!\d)", nome.replace(str(mes), "", 1))
        anos = {a for a in re.findall(r"20\d{2}|(?<!\d)\d{2}(?!\d)", nome)}
        alvo = {str(ano), str(ano)[2:]}
        if not (anos & alvo):
            continue
        # se houver duplicata (…_0), a última da página costuma ser a boa
        achados[mes] = href
    return sorted(achados.items())


def xls_do_zip(conteudo: bytes) -> list[tuple[str, bytes]]:
    """Devolve [(nome, bytes)] dos .XLS, entrando em ZIP aninhado se houver."""
    saida = []
    z = zipfile.ZipFile(io.BytesIO(conteudo))
    for n in z.namelist():
        if n.endswith("/"):
            continue
        d = z.read(n)
        up = n.upper()
        if up.endswith(".XLS") or up.endswith(".XLSX"):
            saida.append((n, d))
        elif up.endswith(".ZIP"):
            saida.extend(xls_do_zip(d))
    return saida


def data_do_header(hdr: list[str]) -> str | None:
    """A coluna MA<ddmmyy> carrega a data do levantamento. Mais confiável que
    o nome do arquivo, que já veio com typo em agosto."""
    for h in hdr:
        m = re.fullmatch(r"(?:MA|MO|MI)(\d{2})(\d{2})(\d{2})", str(h).strip().upper())
        if m:
            d, mo, y = (int(x) for x in m.groups())
            try:
                return date(2000 + y, mo, d).isoformat()
            except ValueError:
                return None
    return None


def parse_xls(nome: str, dados: bytes) -> tuple[str | None, list[dict]]:
    wb = xlrd.open_workbook(file_contents=dados, encoding_override="latin-1")
    sh = wb.sheet_by_index(0)
    hdr = [str(c).strip().upper() for c in sh.row_values(0)]
    col = {h: i for i, h in enumerate(hdr)}
    faltando = [c for c in ("ESP", "VAR", "MAPK", "MOPK", "MIPK") if c not in col]
    if faltando:
        print(f"    {nome}: colunas ausentes {faltando} — pulando")
        return None, []

    d = data_do_header(hdr)
    linhas = []
    for i in range(1, sh.nrows):
        row = sh.row_values(i)
        esp = str(row[col["ESP"]]).strip().upper()
        var = str(row[col["VAR"]]).strip().upper()
        if esp != "PALTA" or "HASS" not in var:
            continue
        if "PROM" in var:                     # subtotal da espécie
            continue
        mx = num(row[col["MAPK"]]); md = num(row[col["MOPK"]]); mn = num(row[col["MIPK"]])
        if md is None:
            continue
        linhas.append({
            "min": mn if mn is not None else md,
            "max": mx if mx is not None else md,
            "moda": md,
            "proc": str(row[col["PROC"]]).strip() if "PROC" in col else "",
        })
    return d, linhas


def main() -> int:
    dry       = "--dry-run" in sys.argv
    sem_cache = "--sem-cache" in sys.argv
    ano = date.today().year
    if "--ano" in sys.argv:
        ano = int(sys.argv[sys.argv.index("--ano") + 1])

    print(f"ETL Argentina — Palta Hass atacado (Mercado Central) · ano {ano} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    zips = listar_zips(ano)
    if not zips:
        print("  ERRO: nenhum ZIP de FRUTAS encontrado para o ano. "
              "A página de precios mayoristas mudou.")
        return 1
    print(f"  {len(zips)} ZIP mensais: meses {[m for m, _ in zips]}")
    print("  cada ZIP tem um .XLS por dia útil; são 8 downloads, leva alguns "
          "minutos.", flush=True)

    por_data, procs = {}, defaultdict(int)
    for mes, url in zips:
        try:
            print(f"  baixando mês {mes:>2}...", flush=True)
            r = requests.get(url, headers=HEADERS, timeout=180)
            r.raise_for_status()
            arquivos = xls_do_zip(r.content)
        except Exception as e:                        # noqa: BLE001
            print(f"  mês {mes}: falhou ({e}) — seguindo")
            continue
        n_dias = 0
        for nome, dados in arquivos:
            try:
                d, linhas = parse_xls(nome, dados)
            except Exception as e:                    # noqa: BLE001
                print(f"    {nome}: erro de leitura ({e})")
                continue
            if not d or not linhas or not d.startswith(str(ano)):
                continue
            por_data[d] = linhas
            for x in linhas:
                procs[x["proc"]] += 1
            n_dias += 1
        print(f"  mês {mes:>2}: {len(arquivos)} arquivos, {n_dias} dias com Palta Hass", flush=True)

    if not por_data:
        print("  ERRO: nenhum dia com Palta Hass. Layout dos XLS mudou.")
        return 1

    # o câmbio vem DEPOIS da extração, para pedir fora só os dias que faltam
    fx, fonte_fx = cambio_fx.carregar(PAIS, PAR, ano, sorted(por_data),
                                      sem_cache=sem_cache)

    agora = datetime.now(timezone.utc).isoformat()
    regs, sem_fx = [], []
    for d in sorted(por_data):
        linhas = por_data[d]
        taxa = cambio_fx.fx_da_data(fx, d)
        if not taxa:
            sem_fx.append(d)
            continue
        regs.append({
            "data":            d,
            "pais":            PAIS,
            "cidade":          CIDADE,
            "produto":         PRODUTO,
            "unidade":         "USD/kg",
            "preco_min_usd":   round(min(x["min"] for x in linhas) / taxa, 4),
            "preco_max_usd":   round(max(x["max"] for x in linhas) / taxa, 4),
            "preco_medio_usd": round((sum(x["moda"] for x in linhas)
                                      / len(linhas)) / taxa, 4),
            "cotacao_par":     PAR,
            "cotacao_local":   round(taxa, 4),
            "fonte":           FONTE,
            "extracted_at":    agora,
        })

    if not regs:
        print("  ERRO: nada a gravar — nenhum dia tem câmbio "
              "(banco vazio e as fontes de blue fora do ar).")
        return 1

    meds = [r["preco_medio_usd"] for r in regs]
    print(f"\n  {len(regs)} dias | {regs[0]['data']} a {regs[-1]['data']}")
    print(f"  câmbio: {fonte_fx}")
    print(f"  USD/kg (moda): {min(meds):.2f} – {max(meds):.2f} "
          f"(média {sum(meds)/len(meds):.2f})")
    print(f"  procedências vistas: {dict(sorted(procs.items(), key=lambda x: -x[1]))}")
    print("  Últimos 5 dias:")
    for r in regs[-5:]:
        print(f"    {r['data']}  {r['preco_min_usd']:.2f} / "
              f"{r['preco_medio_usd']:.2f} / {r['preco_max_usd']:.2f} USD/kg "
              f"@ {r['cotacao_local']:.0f} ARS")

    # Semáforo: buraco velho é aviso, dia mais recente sem câmbio é falha.
    ultimo = max(por_data)
    if sem_fx:
        print(f"\n  AVISO: {len(sem_fx)} dia(s) sem câmbio, fora da carga: "
              f"{sem_fx[:5]}{' …' if len(sem_fx) > 5 else ''}")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 1 if ultimo in sem_fx else 0

    print("\n[2] Upsert no Supabase...")
    import supabase_upsert
    res = supabase_upsert.upsert(TABELA, regs, on_conflict=CHAVE)
    if res["errors"]:
        for e in res["errors"][:3]:
            print(f"  ERRO lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1
    print(f"  OK: {res['inserted']} registros")

    if ultimo in sem_fx:
        print(f"  ERRO: o dia mais recente com preço ({ultimo}) ficou sem câmbio "
              f"e não entrou. O histórico foi gravado; o dado novo, não.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
