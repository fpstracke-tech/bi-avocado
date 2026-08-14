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

Bloqueio anti-bot
-----------------
Em 14/08/2026 a UAM passou a devolver, para o IP do runner do GitHub, HTTP 200
com uma página de ~12 KB que não é o site: `<html lang="en">` num site em
espanhol, `charset="utf8"` sem hífen, zero `.pdf` e um <script> logo no começo
do documento. O tamanho mudava a cada tentativa (11945 / 11904 / 12015 / 11974
bytes) — nonce por requisição. É challenge JS, não mudança de layout. A API de
mídia caía junto, devolvendo HTML no lugar de JSON.

O conserto é `desbloquear_com_navegador()`: um Chromium headless carrega a
página, deixa o desafio rodar, e os cookies resultantes vão para a SESSAO. Daí
a API de mídia e o download dos PDFs voltam a funcionar por requests — o WAF
libera pelo cookie, não pelo fingerprint de TLS. Sem playwright instalado a
função devolve None em silêncio e o ETL segue como antes, que é o caso de quem
roda da própria máquina e nunca vê o bloqueio. O binário do Chromium é baixado
sob demanda pelo próprio script, então nenhum passo extra no workflow.

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
URL_API    = "https://uam.com.uy/wp-json/wp/v2/media"
URL_CAMBIO = "https://open.er-api.com/v6/latest/USD"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Language": "es-UY,es;q=0.9",
    "Referer": URL_LISTA,
}

# Uma sessão só para tudo que fala com a UAM: listagem, API de mídia e download
# dos PDFs. É o que permite o cookie do desafio, capturado uma vez pelo
# navegador, valer para as três coisas.
SESSAO = requests.Session()
SESSAO.headers.update(HEADERS)

_MARCAS_BLOQUEIO = ("just a moment", "captcha", "cloudflare", "access denied",
                    "attention required", "checking your browser", "sucuri",
                    "enable javascript", "ddos protection")

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


def parece_bloqueio(html: str) -> bool:
    """
    True quando a resposta veio com status 2xx mas NÃO é o site.

    Distinguir isso de "o layout mudou" é o ponto todo: são consertos opostos.
    Sinal de bloqueio é página curta, sem nenhum `.pdf`, com script antes de
    qualquer conteúdo ou com `lang="en"` — a UAM é em espanhol. Página grande e
    sem PDF é layout novo, e aí o parser é que precisa mudar.
    """
    if not html:
        return False
    baixo = html.lower()
    if ".pdf" in baixo:                       # veio conteúdo de verdade
        return False
    if any(m in baixo for m in _MARCAS_BLOQUEIO):
        return True
    if len(html) > 60_000:                    # grande e sem PDF: layout, não WAF
        return False
    cabeca = baixo[:600]
    return "<script" in cabeca or 'lang="en"' in cabeca


def _garantir_chromium() -> bool:
    """
    Baixa o Chromium do playwright quando ele não está no disco.

    Fica aqui e não num passo do workflow de propósito: assim o conserto é o
    próprio script, e quem clonar o repo e rodar na mão não precisa lembrar do
    passo extra. No runner do GitHub as bibliotecas de sistema já existem, então
    o `install` sem `--with-deps` basta e não precisa de sudo.
    """
    import subprocess
    print("    Chromium ausente — baixando (uma vez por runner)...", flush=True)
    try:
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           capture_output=True, text=True, timeout=900)
    except Exception as e:                             # noqa: BLE001
        print(f"    não deu para instalar o Chromium ({e})", flush=True)
        return False
    if r.returncode != 0:
        rabo = (r.stderr or r.stdout or "").strip()[-300:]
        print(f"    playwright install falhou (rc={r.returncode}): {rabo}", flush=True)
        return False
    return True


def _coletar_com_navegador(sync_playwright, espera_s: int):
    """(html, cookies) da página de boletins já liberada. Estoura se der ruim."""
    with sync_playwright() as p:
        navegador = p.chromium.launch(args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ])
        ctx = navegador.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="es-UY",
            viewport={"width": 1366, "height": 900},
        )
        pagina = ctx.new_page()
        pagina.goto(URL_LISTA, wait_until="domcontentloaded", timeout=60_000)
        # O desafio seta o cookie e recarrega sozinho. Espero o site de verdade
        # aparecer em vez de dormir um tempo fixo, que ora sobra ora falta.
        for _ in range(espera_s):
            if not parece_bloqueio(pagina.content()):
                break
            pagina.wait_for_timeout(1000)
        else:
            print(f"    o desafio não liberou em {espera_s}s — seguindo assim mesmo",
                  flush=True)
        html, biscoitos = pagina.content(), ctx.cookies()
        navegador.close()
    return html, biscoitos


def desbloquear_com_navegador(espera_s: int = 45) -> str | None:
    """
    Resolve o desafio JS num Chromium headless e injeta os cookies na SESSAO.

    Devolve o HTML já liberado da página de boletins (útil como plano C, porque
    ele lista os ~9 mais recentes) ou None se não deu para desbloquear. Nunca
    estoura: sem playwright instalado, só avisa e devolve None, que é o caso de
    quem roda da própria máquina e nunca vê o bloqueio.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright não instalado — sem desbloqueio por navegador.\n"
              "    pip install playwright && python -m playwright install chromium",
              flush=True)
        return None

    print("  bloqueio detectado — abrindo navegador para resolver o desafio...",
          flush=True)
    html = biscoitos = None
    for tentativa in (1, 2):
        try:
            html, biscoitos = _coletar_com_navegador(sync_playwright, espera_s)
            break
        except Exception as e:                         # noqa: BLE001
            msg = str(e)
            falta_binario = ("executable doesn't exist" in msg.lower()
                             or "playwright install" in msg.lower())
            if tentativa == 1 and falta_binario and _garantir_chromium():
                continue
            print(f"    navegador falhou ({msg.splitlines()[0] if msg else e})",
                  flush=True)
            return None
    if biscoitos is None:
        return None

    for c in biscoitos:
        SESSAO.cookies.set(c["name"], c["value"],
                           domain=c.get("domain"), path=c.get("path") or "/")
    achados = _links_de_boletim(html)
    print(f"    {len(biscoitos)} cookies capturados · a página do navegador "
          f"trouxe {len(achados)} boletins", flush=True)
    if not biscoitos and not achados:
        return None
    return html


def _baixar_pdf(url: str) -> bytes:
    """Baixa pela SESSAO e recusa qualquer coisa que não comece com %PDF."""
    r = SESSAO.get(url, timeout=180)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"resposta não é PDF ({len(r.content)} bytes)")
    return r.content


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
            r = SESSAO.get(URL_API, timeout=90,
                           params={"search": "boletin-de-precios", "per_page": 100,
                                   "page": pagina, "_fields": "source_url,date"})
            if r.status_code == 400:          # pediu página além do fim
                break
            r.raise_for_status()
            # HTTP 200 com HTML no corpo é o WAF, não a API. Dizer isso aqui
            # evita o "Expecting value: line 1 column 1", que não explica nada.
            if "json" not in (r.headers.get("Content-Type") or "").lower():
                print(f"  API de mídia devolveu "
                      f"{r.headers.get('Content-Type')!r} em vez de JSON "
                      f"({len(r.content)} bytes) — resposta barrada.", flush=True)
                break
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


def _resumo_api(api: list[tuple[str, str]], desde: str | None) -> None:
    print(f"  API de mídia: {len(api)} boletins"
          + (f" publicados desde {desde}" if desde else "")
          + f" ({api[-1][0]} a {api[0][0]})", flush=True)


def listar_boletins(tentativas: int = 4, desde: str | None = None) -> list[str]:
    """
    API de mídia (acervo inteiro) → página pública (só os recentes) → navegador.

    O navegador só entra quando as duas primeiras vierem BARRADAS. Se a página
    chegar íntegra e mesmo assim sem links, é layout novo e abrir um Chromium
    não resolve nada — o diagnóstico já foi impresso e a função devolve vazio.
    """
    api = listar_boletins_api(desde)
    if api:
        _resumo_api(api, desde)
        return [u for _, u in api]

    print("  API de mídia não devolveu nada — caindo para a página pública",
          flush=True)
    if desde:
        print("  AVISO: a página pública lista só os boletins recentes, então "
              "--desde não vai ser respeitado nesta rodada.", flush=True)
    urls, bloqueado = listar_boletins_html(tentativas)
    if urls:
        return urls
    if not bloqueado:
        return []

    html = desbloquear_com_navegador()
    if html is None:
        return []

    # Com o cookie na SESSAO a API costuma voltar, e ela é a única que dá o
    # acervo completo. A página liberada fica como plano C.
    api = listar_boletins_api(desde)
    if api:
        _resumo_api(api, desde)
        return [u for _, u in api]
    urls = _links_de_boletim(html)
    if urls:
        print(f"  {len(urls)} boletins da página liberada pelo navegador "
              "(só os recentes — a API continua barrada)", flush=True)
    return urls


def listar_boletins_html(tentativas: int = 4) -> tuple[list[str], bool]:
    """
    A UAM é instável para IP de datacenter. Em 11/08/2026 o job do GitHub
    Actions recebeu HTTP 200 com uma página SEM nenhum link de boletim (no run
    anterior, do mesmo dia, tinha funcionado) — comportamento de proteção
    anti-bot, não de mudança de layout. Daí as tentativas com espera crescente
    e o diagnóstico no fim: sem ele, "nenhum boletim encontrado" não distingue
    bloqueio de mudança no site, e são consertos completamente diferentes.

    Devolve (urls, bloqueado). O segundo item é o que decide se vale a pena
    acordar o navegador headless.
    """
    import time
    ultimo_html, ultimo_status = "", None
    for i in range(tentativas):
        try:
            r = SESSAO.get(URL_LISTA, timeout=120)
            ultimo_status = r.status_code
            r.raise_for_status()
            ultimo_html = r.text
            urls = _links_de_boletim(ultimo_html)
            if urls:
                return urls, False
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
    bloqueado = parece_bloqueio(ultimo_html)
    print(f"    veredito ............... "
          f"{'BLOQUEIO (resposta não é o site)' if bloqueado else 'página íntegra'}")
    print("\n  Se for bloqueio, o site está recusando o IP do runner e o "
          "conserto é o\n  navegador headless (desbloquear_com_navegador), fonte "
          "alternativa ou\n  execução local — não o parser. Se a página vier "
          "íntegra e sem links,\n  aí sim o layout mudou.")
    return [], bloqueado


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
        renovou = False
        for n, u in enumerate(urls, 1):
            nome = u.rsplit("/", 1)[-1]
            try:
                conteudo = _baixar_pdf(u)
            except Exception as e:                     # noqa: BLE001
                # O cookie do desafio expira no meio de um backfill de 189
                # arquivos. Renovo uma vez e sigo; se falhar de novo, é outra
                # coisa e não adianta ficar reabrindo navegador por PDF.
                if renovou or desbloquear_com_navegador() is None:
                    print(f"    {nome}: falhou ({e})")
                    continue
                renovou = True
                try:
                    conteudo = _baixar_pdf(u)
                except Exception as e2:                # noqa: BLE001
                    print(f"    {nome}: falhou ({e2})")
                    continue
            try:
                d, linhas = parse_pdf(io.BytesIO(conteudo), nome)
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

    fx, datado = carregar_cambio(sorted(por_data))
    if not fx:
        return 1

    # Câmbio não datado é tolerável numa rodada diária (1-2 semanas, erro
    # pequeno, aviso basta) e inaceitável num backfill: aplicar a taxa de hoje
    # a 22 meses de boletim inventa a série em dólar inteira. Nesse caso é
    # melhor não gravar nada.
    span = (date.fromisoformat(max(por_data)) - date.fromisoformat(min(por_data))).days
    if not datado and span > 45:
        print(f"\n  ERRO: {len(por_data)} boletins cobrindo {span} dias, e o câmbio "
              f"veio SEM data\n  (taxa única para tudo). Aplicar a cotação de hoje a "
              f"esse intervalo inventaria\n  a série em USD, então nada foi gravado.\n"
              f"  Rode de novo quando o UYU=X do Yahoo responder — de preferência da "
              f"sua\n  máquina, que o alcança. O container do Claude e alguns runners "
              f"não alcançam.")
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
