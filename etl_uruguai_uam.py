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

Câmbio: `cambio_fx.carregar` — USD/UYU POR DATA, banco primeiro, depois Yahoo
UYU=X, stooq e open.er-api. A primeira versão deste ETL aplicava UMA taxa (a do
dia da execução) a todas as datas — o preço em UYU era real e datado, mas a
conversão para USD não. Diferença medida em 11/08/2026: taxa do dia 40,2367
contra 39,55 no início de julho, ou seja os valores de julho saíam ~1,7% baixos.

O modo "taxa única com aviso" SAIU. Agora ele é impossível por construção: a
er-api só devolve a cotação de hoje, e no cambio_fx uma cotação só cobre os 4
dias seguintes. Data sem câmbio próprio fica fora da carga em vez de receber
uma taxa que não é dela. O guard-rail deixou de depender de alguém ler o aviso.

Uso:
    python etl_uruguai_uam.py                       # boletins da página
    python etl_uruguai_uam.py --dry-run
    python etl_uruguai_uam.py --arquivo b.pdf       # processa um PDF local
    python etl_uruguai_uam.py --desde 2024-10-01    # backfill pelo acervo
    python etl_uruguai_uam.py --testar-cambio       # só confere a série de câmbio
    python etl_uruguai_uam.py --sem-cache           # ignora o banco
"""

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

import cambio_fx

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
PAR     = "USD/UYU"
FONTE   = "UAM — boletín de precios mayoristas de referencia (uam.com.uy)"

URL_LISTA = "https://uam.com.uy/boletin-de-precios-mayoristas/"
URL_API   = "https://uam.com.uy/wp-json/wp/v2/media"

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


def listar_boletins_api(desde: str | None = None) -> list[tuple[str, str]]:
    """
    [(data de publicação, url)] — acervo inteiro, mais novo primeiro.

    A página pública mostra só os ~9 boletins mais recentes, o que limita a
    série ao último mês. A API de mídia do WordPress devolve tudo: em
    12/08/2026 eram 189 arquivos, de 24/10/2024 a 10/08/2026. É isso que
    permite backfill — sem ela, o histórico do Uruguai depende de planilha,
    que só traz um preço médio por semana.

    `desde` filtra pela data de PUBLICAÇÃO do arquivo, não pela data do preço
    (essa sai de dentro do PDF). Serve para cortar download, não para decidir
    o que entra na série.
    """
    achados, pagina = [], 1
    while pagina <= 20:                       # trava: 2.000 arquivos
        try:
            r = requests.get(URL_API, headers=HEADERS, timeout=90,
                             params={"search": "boletin-de-precios", "per_page": 100,
                                     "page": pagina, "_fields": "source_url,date"})
            if r.status_code == 400:          # pediu página além do fim
                break
            r.raise_for_status()
            lote = r.json()
        except Exception as e:                # noqa: BLE001
            print(f"  API de mídia falhou na página {pagina}: {e}", flush=True)
            break
        if not isinstance(lote, list) or not lote:
            break
        for x in lote:
            u = (x.get("source_url") or "")
            nome = u.rsplit("/", 1)[-1].lower()
            if not nome.startswith("boletin-de-precios") or not nome.endswith(".pdf"):
                continue
            if "animales" in nome or "granja" in nome:
                continue
            pub = (x.get("date") or "")[:10]
            if desde and pub and pub < desde:
                continue
            achados.append((pub, u))
        total = int(r.headers.get("X-WP-TotalPages") or 1)
        if pagina >= total:
            break
        pagina += 1
    vistos, saida = set(), []
    for pub, u in sorted(achados, reverse=True):
        if u in vistos:
            continue
        vistos.add(u)
        saida.append((pub, u))
    return saida


def listar_boletins(tentativas: int = 4, desde: str | None = None) -> list[str]:
    """API de mídia primeiro; a página pública é o plano B."""
    api = listar_boletins_api(desde)
    if api:
        print(f"  API de mídia: {len(api)} boletins"
              + (f" publicados desde {desde}" if desde else "")
              + f" ({api[-1][0]} a {api[0][0]})", flush=True)
        return [u for _, u in api]
    print("  API de mídia não devolveu nada — caindo para a página pública",
          flush=True)
    if desde:
        print("  AVISO: a página pública lista só os boletins recentes, então "
              "--desde não vai ser respeitado nesta rodada.", flush=True)
    return listar_boletins_html(tentativas)


def listar_boletins_html(tentativas: int = 4) -> list[str]:
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


def carregar_cambio(datas: list[str], sem_cache: bool = False):
    """
    Câmbio datado para uma lista de datas que pode cruzar anos.

    O backfill vai de 24/10/2024 a hoje, e `cambio_fx.carregar` trabalha por
    ano civil (é assim que as fontes publicam). Aqui a lista é fatiada por ano
    e os resultados unidos — as chaves são datas, então não há colisão.
    """
    fx, rotulos = {}, []
    for ano in sorted({d[:4] for d in datas}):
        do_ano = [d for d in datas if d.startswith(ano)]
        parcial, rotulo = cambio_fx.carregar(PAIS, PAR, int(ano), do_ano,
                                             sem_cache=sem_cache)
        fx.update(parcial)
        rotulos.append(f"{ano}: {rotulo}")
    return fx, " · ".join(rotulos)


def main() -> int:
    dry       = "--dry-run" in sys.argv
    sem_cache = "--sem-cache" in sys.argv
    local = None

    # Confere só a série de câmbio, sem baixar nada da UAM. Serve para validar
    # a cascata isolada quando algo parecer errado nos valores em USD.
    if "--testar-cambio" in sys.argv:
        from datetime import timedelta
        hoje = date.today()
        amostra = sorted((hoje - timedelta(days=k)).isoformat()
                         for k in (30, 20, 10, 3, 0))
        fx, rotulo = carregar_cambio(amostra, sem_cache=sem_cache)
        if not fx:
            return 1
        print(f"  fontes: {rotulo}")
        for d in amostra:
            print(f"    {d} -> {cambio_fx.fx_da_data(fx, d)}")
        return 0

    if "--arquivo" in sys.argv:
        local = Path(sys.argv[sys.argv.index("--arquivo") + 1])

    # backfill: --desde 2024-10-01 puxa o acervo todo pela API de mídia.
    # --limite N corta a lista, para testar sem baixar 189 PDFs.
    desde = None
    if "--desde" in sys.argv:
        desde = sys.argv[sys.argv.index("--desde") + 1]
        try:
            date.fromisoformat(desde)
        except ValueError:
            print(f"  ERRO: --desde precisa ser AAAA-MM-DD, veio {desde!r}.")
            return 1
    limite = None
    if "--limite" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--limite") + 1])

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
        urls = listar_boletins(desde=desde)
        if not urls:
            print("  ERRO: nenhum boletim encontrado.")
            return 1
        if limite:
            urls = urls[:limite]
            print(f"  --limite {limite}: só os {len(urls)} mais recentes", flush=True)
        print(f"  {len(urls)} boletins — baixando um a um", flush=True)
        import io
        for n, u in enumerate(urls, 1):
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
                print(f"    [{n}/{len(urls)}] {nome}: {d} · {len(linhas)} linhas",
                      flush=True)
            else:
                print(f"    [{n}/{len(urls)}] {nome}: sem Palta Hass (data={d})",
                      flush=True)

    if not por_data:
        print("  ERRO: nada extraído.")
        return 1

    # o câmbio vem DEPOIS da extração, para pedir fora só os dias que faltam
    fx, fonte_fx = carregar_cambio(sorted(por_data), sem_cache=sem_cache)

    agora = datetime.now(timezone.utc).isoformat()
    regs, sem_fx = [], []
    for d in sorted(por_data):
        linhas = por_data[d]
        taxa = cambio_fx.fx_da_data(fx, d)
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
            "cotacao_par":     PAR,
            "cotacao_local":   round(taxa, 4),
            "fonte":           f"{FONTE} · origens: {'+'.join(origens)}",
            "extracted_at":    agora,
        })

    if sem_fx:
        print(f"  AVISO: {len(sem_fx)} boletim(ns) sem câmbio próprio, fora da "
              f"carga: {sem_fx[:5]}{' …' if len(sem_fx) > 5 else ''}")
    if not regs:
        print("  ERRO: nada a gravar — nenhuma data tem câmbio "
              "(banco vazio e as fontes de USD/UYU fora do ar).")
        return 1

    meds = [r["preco_medio_usd"] for r in regs]
    print(f"\n  {len(regs)} boletins | {regs[0]['data']} a {regs[-1]['data']}")
    print(f"  câmbio: {fonte_fx}")
    print(f"  USD/kg: {min(meds):.2f} – {max(meds):.2f}")
    for r in regs[-10:]:
        print(f"    {r['data']}  {r['preco_min_usd']:.2f} / {r['preco_medio_usd']:.2f} "
              f"/ {r['preco_max_usd']:.2f} USD/kg @ {r['cotacao_local']:.4f} UYU "
              f"[{r['fonte'].split('origens: ')[-1]}]")

    # Semáforo: buraco velho é aviso, boletim mais recente sem câmbio é falha.
    ultimo = max(por_data)

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
        print(f"  ERRO: o boletim mais recente ({ultimo}) ficou sem câmbio e não "
              f"entrou. O histórico foi gravado; o dado novo, não.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
