"""
ETL Preços Uruguai — Palta Hass, atacado (UAM / Observatorio Granjero)
======================================================================
SUBSTITUI a série anterior, que era ~152 UYU/kg constante na moeda local, com
5 registros cuja própria coluna `fonte` dizia
"Varejo Tata+TiendaInglesa / fator 2.042 (estimativa atacado)" — varejo
multiplicado por um fator, apresentado como atacado.

Fonte real: Boletín de precios mayoristas de referencia da UAM (Unidad
Agroalimentaria Metropolitana, o mercado atacadista de Montevidéu). Publicado
DUAS vezes por semana, segunda e quinta.

    https://uam.com.uy/boletin-de-precios-mayoristas/

A tabela do boletim tem esta cara (página 5 do boletim de 10/08/2026):

    Especie Variedad País Unidad Calibre    Categoría Min Max
    Palta   Hass     PE   KG    GRANDE     I         200 220
    Palta   Hass     PE   KG    MEDIANO    I         180 200
    Palta   Hass     PE   KG    MEDIANO    II        150 160
    Palta   Hass     BR   KG    GRANDE     E         180 200
    Palta   Hass     BR   KG    GRANDE     I         140 170
    Palta   Hass     BR   KG    MEDIANO    II        130 140

Preço em UYU/kg, por calibre e categoria, COM PAÍS DE ORIGEM. O Uruguai não
produz Hass em escala — importa, e o Brasil é um dos dois fornecedores da praça
ao lado do Peru. Guardo a origem no log; não explodo em linhas separadas porque
v_precos_origem_semanal agrega por país e linhas extras entrariam duas vezes na
média. Quando quiser a comparação BR × PE por data, é mudar o contrato da view,
não este parser.

A página lista só os ~9 boletins mais recentes, num carrossel. Como o upsert é
idempotente na UNIQUE (data, pais, cidade, produto), cada execução acumula e a
série cresce sozinha — o que nunca aconteceu com os coletores antigos, que
sobrescreviam.

Os nomes dos PDF são inconsistentes ("...-10-de-agosto-de-2026.pdf" mas
"...-6-de-noviembre-2025.pdf", sem o "de" antes do ano), então o coletor lê os
href da página em vez de montar a URL.

Câmbio: USD/UYU POR DATA, do Yahoo (ticker UYU=X via yfinance), gravado junto
com cada registro. A primeira versão deste ETL aplicava UMA taxa (a do dia da
execução) a todas as datas — o preço em UYU era real e datado, mas a conversão
para USD não. Diferença medida em 11/08/2026: taxa do dia 40,2367 contra
39,55 no início de julho, ou seja os valores de julho saíam ~1,7% baixos.
É a mesma categoria de falha dos coletores antigos e foi corrigida.

Se o Yahoo estiver fora, cai na open.er-api.com — que só dá a taxa de hoje.
Nesse caso o ETL AVISA em letra grande que a série está com taxa única, porque
esse é justamente o defeito que não pode passar calado.

Uso:
    python etl_uruguai_uam.py                       # baixa e processa os boletins da página
    python etl_uruguai_uam.py --dry-run
    python etl_uruguai_uam.py --arquivo b.pdf       # processa um PDF local (backfill)
    python etl_uruguai_uam.py --testar-cambio       # só confere a série de câmbio
"""

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

try:
    import pdfplumber
except ImportError:                                    # pragma: no cover
    print("ERRO: falta o pdfplumber (pip install pdfplumber).")
    raise

TABELA  = "precos_origem"
CHAVE   = "data,pais,cidade,produto"
PAIS    = "Uruguai"
CIDADE  = "Montevideo"
PRODUTO = "Palta Hass"
FONTE   = "UAM — boletín de precios mayoristas de referencia (uam.com.uy)"

URL_LISTA  = "https://uam.com.uy/boletin-de-precios-mayoristas/"
URL_CAMBIO = "https://open.er-api.com/v6/latest/USD"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Language": "es-UY,es;q=0.9",
    "Referer": URL_LISTA,
}

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
         "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
         "noviembre": 11, "diciembre": 12}

# "Palta Hass PE KG GRANDE I 200 220"
RE_LINHA = re.compile(
    r"^Palta\s+Hass\s+([A-Z]{2})\s+(KG|UN|DOC|CAB|CJ)\s+(\S+)\s+(\S+)\s+"
    r"([\d.,]+)\s+([\d.,]+)\s*$", re.I)

# "Lunes 10 de agosto de 2026" / "jueves 6 de agosto 2026"
RE_DATA = re.compile(
    r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[áa]bado|domingo)?\s*"
    r"(\d{1,2})\s+de\s+([a-zá]+)\s+(?:de\s+)?(\d{4})", re.I)


def num(s):
    try:
        v = float(str(s).replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def data_do_texto(txt: str):
    m = RE_DATA.search(txt)
    if not m:
        return None
    dia, mes_nome, ano = m.group(1), m.group(2).lower(), m.group(3)
    mes = MESES.get(mes_nome)
    if not mes:
        return None
    try:
        return date(int(ano), mes, int(dia)).isoformat()
    except ValueError:
        return None


def parse_pdf(caminho_ou_bytes, rotulo: str = "") -> tuple[str | None, list[dict]]:
    """Devolve (data ISO, [{min,max,pais_origem,calibre,categoria}])."""
    abrir = (pdfplumber.open(caminho_ou_bytes) if isinstance(caminho_ou_bytes, (str, Path))
             else pdfplumber.open(caminho_ou_bytes))
    linhas, d = [], None
    with abrir as pdf:
        for i, pg in enumerate(pdf.pages):
            txt = pg.extract_text() or ""
            if d is None:
                d = data_do_texto(txt)
            for l in txt.split("\n"):
                m = RE_LINHA.match(l.strip())
                if not m:
                    continue
                origem, unidade, calibre, categoria, mn, mx = m.groups()
                if unidade.upper() != "KG":       # só linhas por quilo
                    continue
                a, b = num(mn), num(mx)
                if a is None or b is None:
                    continue
                linhas.append({
                    "min": min(a, b), "max": max(a, b),
                    "origem": origem.upper(),
                    "calibre": calibre, "categoria": categoria,
                })
    if not linhas:
        print(f"    {rotulo}: nenhuma linha 'Palta Hass ... KG' — layout mudou?")
    return d, linhas


def _links_de_boletim(html: str) -> list[str]:
    urls, vistos = [], set()
    for href in re.findall(r'href="([^"]+\.pdf)"', html, re.I):
        nome = href.rsplit("/", 1)[-1].lower()
        # só o boletim de frutas e hortaliças; fora os de animais de granja,
        # informes semanais, mensais e especiais
        if not nome.startswith("boletin-de-precios"):
            continue
        if "animales" in nome or "granja" in nome:
            continue
        if href not in vistos:
            vistos.add(href)
            urls.append(href)
    return urls


def listar_boletins(tentativas: int = 4) -> list[str]:
    """
    A UAM é instável para IP de datacenter. Em 11/08/2026 o job do GitHub
    Actions recebeu HTTP 200 com uma página SEM nenhum link de boletim (no run
    anterior, do mesmo dia, tinha funcionado) — comportamento de proteção
    anti-bot, não de mudança de layout. Daí as tentativas com espera crescente
    e o diagnóstico no fim: sem ele, "nenhum boletim encontrado" não distingue
    bloqueio de mudança no site, e são consertos completamente diferentes.
    """
    import time
    ultimo_html, ultimo_status = "", None
    for i in range(tentativas):
        try:
            r = requests.get(URL_LISTA, headers=HEADERS, timeout=120)
            ultimo_status = r.status_code
            r.raise_for_status()
            ultimo_html = r.text
            urls = _links_de_boletim(ultimo_html)
            if urls:
                return urls
            print(f"  tentativa {i+1}/{tentativas}: página veio sem boletins "
                  f"({len(ultimo_html)} bytes)", flush=True)
        except Exception as e:                         # noqa: BLE001
            print(f"  tentativa {i+1}/{tentativas} falhou: {e}", flush=True)
        if i < tentativas - 1:
            espera = 5 * (i + 1)
            print(f"    nova tentativa em {espera}s", flush=True)
            time.sleep(espera)

    # Diagnóstico: separar bloqueio de mudança de layout.
    print("\n  DIAGNÓSTICO da última resposta:")
    print(f"    HTTP status ............ {ultimo_status}")
    print(f"    tamanho ................ {len(ultimo_html)} bytes")
    if ultimo_html:
        baixo = ultimo_html.lower()
        n_pdf = len(re.findall(r'\.pdf', ultimo_html, re.I))
        print(f"    ocorrências de .pdf .... {n_pdf}")
        print(f"    contém 'boletin' ....... {'boletin' in baixo}")
        print(f"    contém 'uam' ........... {'uam' in baixo}")
        for pista, rotulo in [("cloudflare", "Cloudflare"),
                              ("captcha", "CAPTCHA"),
                              ("just a moment", "desafio JS"),
                              ("access denied", "acesso negado"),
                              ("attention required", "bloqueio WAF")]:
            if pista in baixo:
                print(f"    >>> indício de {rotulo} na resposta")
        print(f"    início ................. {ultimo_html[:180]!r}")
    print("\n  Se houver indício de bloqueio, o site está recusando o IP do "
          "runner e o\n  conserto é fonte alternativa ou execução local — não "
          "o parser.\n  Se a página vier íntegra e sem links, aí sim o layout "
          "mudou.")
    return []


def cambio_yahoo(desde: str) -> dict:
    """{'AAAA-MM-DD': UYU por USD} — fechamento diário do ticker UYU=X."""
    import yfinance as yf
    from datetime import timedelta
    inicio = (date.fromisoformat(desde) - timedelta(days=15)).isoformat()
    h = yf.Ticker("UYU=X").history(start=inicio)
    if h is None or len(h) == 0:
        raise RuntimeError("UYU=X devolveu série vazia")
    fx = {}
    for idx, linha in h.iterrows():
        v = float(linha["Close"])
        if v > 0:
            fx[idx.date().isoformat()] = v
    if not fx:
        raise RuntimeError("nenhum fechamento válido em UYU=X")
    return fx


def cambio_hoje() -> float:
    r = requests.get(URL_CAMBIO, timeout=60)
    r.raise_for_status()
    return float(r.json()["rates"]["UYU"])


def carregar_cambio(datas: list[str], tentativas: int = 3):
    """
    Devolve (dict data->taxa, datado: bool).

    datado=False significa taxa única para todas as datas — aceitável só como
    último recurso, e o chamador tem que avisar na tela.
    """
    desde = min(datas)
    erro = None
    for i in range(tentativas):
        try:
            fx = cambio_yahoo(desde)
            print(f"  câmbio USD/UYU: {len(fx)} dias do Yahoo "
                  f"({min(fx)} a {max(fx)})", flush=True)
            return fx, True
        except Exception as e:                         # noqa: BLE001
            erro = e
            print(f"  câmbio Yahoo: tentativa {i+1}/{tentativas} falhou ({e})",
                  flush=True)

    print(f"  Yahoo indisponível ({erro}). Tentando taxa do dia...", flush=True)
    try:
        taxa = cambio_hoje()
    except Exception as e:                             # noqa: BLE001
        print(f"  ERRO: câmbio USD/UYU indisponível também na er-api ({e}).")
        return None, False

    print("  " + "!" * 68)
    print(f"  AVISO: usando taxa ÚNICA de {taxa:.4f} para TODAS as {len(datas)} "
          f"datas.")
    print("  Os valores em USD de datas passadas ficam aproximados. Rode de novo")
    print("  quando o Yahoo voltar para gravar com câmbio datado.")
    print("  " + "!" * 68)
    return {d: taxa for d in datas}, False


def fx_da_data(fx: dict, d: str):
    """Taxa do dia; se não houver (fim de semana, feriado), a última anterior."""
    if d in fx:
        return fx[d]
    ant = [k for k in fx if k <= d]
    return fx[max(ant)] if ant else None


def main() -> int:
    dry = "--dry-run" in sys.argv
    local = None

    # Confere só a série de câmbio, sem baixar nada da UAM. Serve para validar
    # o yfinance isolado quando algo parecer errado nos valores em USD.
    if "--testar-cambio" in sys.argv:
        from datetime import timedelta
        hoje = date.today()
        amostra = [(hoje - timedelta(days=k)).isoformat() for k in (30, 20, 10, 3, 0)]
        fx, datado = carregar_cambio(amostra)
        if not fx:
            return 1
        print(f"  datado={datado}")
        for d in amostra:
            print(f"    {d} -> {fx_da_data(fx, d)}")
        return 0

    if "--arquivo" in sys.argv:
        local = Path(sys.argv[sys.argv.index("--arquivo") + 1])

    print(f"ETL Uruguai — Palta Hass atacado (UAM) · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    por_data = {}
    if local:
        if not local.exists():
            print(f"  ERRO: {local} não existe.")
            return 1
        d, linhas = parse_pdf(local, local.name)
        print(f"  {local.name}: data={d}, {len(linhas)} linhas Palta Hass")
        if d and linhas:
            por_data[d] = linhas
    else:
        urls = listar_boletins()
        if not urls:
            print("  ERRO: nenhum boletim encontrado na página.")
            return 1
        print(f"  {len(urls)} boletins na página — baixando um a um", flush=True)
        import io
        for u in urls:
            nome = u.rsplit("/", 1)[-1]
            try:
                r = requests.get(u, headers=HEADERS, timeout=180)
                r.raise_for_status()
                d, linhas = parse_pdf(io.BytesIO(r.content), nome)
            except Exception as e:                     # noqa: BLE001
                print(f"    {nome}: falhou ({e})")
                continue
            if d and linhas:
                por_data[d] = linhas
                print(f"    {nome}: {d} · {len(linhas)} linhas", flush=True)
            else:
                print(f"    {nome}: sem Palta Hass (data={d})")

    if not por_data:
        print("  ERRO: nada extraído.")
        return 1

    fx, datado = carregar_cambio(sorted(por_data))
    if not fx:
        return 1

    agora = datetime.now(timezone.utc).isoformat()
    regs, sem_fx = [], []
    for d in sorted(por_data):
        linhas = por_data[d]
        taxa = fx_da_data(fx, d)
        if not taxa:
            sem_fx.append(d)
            continue
        origens = sorted({x["origem"] for x in linhas})
        medias = [(x["min"] + x["max"]) / 2 for x in linhas]
        regs.append({
            "data":            d,
            "pais":            PAIS,
            "cidade":          CIDADE,
            "produto":         PRODUTO,
            "unidade":         "USD/kg",
            "preco_min_usd":   round(min(x["min"] for x in linhas) / taxa, 4),
            "preco_max_usd":   round(max(x["max"] for x in linhas) / taxa, 4),
            "preco_medio_usd": round((sum(medias) / len(medias)) / taxa, 4),
            "cotacao_par":     "USD/UYU",
            "cotacao_local":   round(taxa, 4),
            "fonte":           f"{FONTE} · origens: {'+'.join(origens)}",
            "extracted_at":    agora,
        })

    if sem_fx:
        print(f"  AVISO: {len(sem_fx)} datas sem câmbio, fora da carga: {sem_fx}")
    if not regs:
        print("  ERRO: nada a gravar depois do câmbio.")
        return 1

    meds = [r["preco_medio_usd"] for r in regs]
    print(f"\n  {len(regs)} boletins | {regs[0]['data']} a {regs[-1]['data']}"
          f"{'' if datado else '  (CÂMBIO NÃO DATADO)'}")
    print(f"  USD/kg: {min(meds):.2f} – {max(meds):.2f}")
    for r in regs:
        print(f"    {r['data']}  {r['preco_min_usd']:.2f} / {r['preco_medio_usd']:.2f} "
              f"/ {r['preco_max_usd']:.2f} USD/kg @ {r['cotacao_local']:.4f} UYU "
              f"[{r['fonte'].split('origens: ')[-1]}]")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 0

    print("\n[2] Upsert no Supabase...")
    import supabase_upsert
    res = supabase_upsert.upsert(TABELA, regs, on_conflict=CHAVE)
    if res["errors"]:
        for e in res["errors"][:3]:
            print(f"  ERRO lote {e['batch_start']}: HTTP {e['status']} — {e['detail']}")
        return 1
    print(f"  OK: {res['inserted']} registros")
    return 0


if __name__ == "__main__":
    sys.exit(main())
