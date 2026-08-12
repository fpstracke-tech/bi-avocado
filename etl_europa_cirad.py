"""
ETL Preços Europa — CIRAD / FruiTrop (PDF que chega por e-mail)
================================================================
SUBSTITUI o processo manual: o relatório chega por e-mail, alguém salva o PDF e
um PRINT vai para o Power BI. Não existe série histórica, só a foto da semana.

O fornecedor não tem API (já perguntado). O PDF resolve, mas NÃO pela via
óbvia. Auditoria de 12/08/2026 sobre o relatório da semana 31/2026:

  1. A tabela de texto da página 1 tem o RÓTULO DE SEMANA ERRADO.
     Ela diz "Week 30 | Week 29" com "10.00 € | -0.22 €". Mas o gráfico de
     barras do MESMO relatório mostra w30 = 10,22 e w31 = 10,00. E o print do
     relatório da semana 30 mostra "Week 30 = 10,22", que bate com o gráfico.
     Ou seja: o 10,00 é da semana 31, e -0,22 é a variação de w30 para w31
     (10,00 - 10,22 = -0,22). Coerente.
     => O VALOR DA TABELA É DA SEMANA DO RELATÓRIO, não da anterior.
     Confiar no rótulo gravaria S30 = 10,00 por cima do correto 10,22 e
     perderia a S31 inteira.

  2. O gráfico de barras traz os valores IMPRESSOS como rótulo de dado, e são
     CINCO semanas por relatório (w27..w31). Um único PDF já preenche a
     estrutura de "últimas 4 semanas". É a fonte mais confiável do documento:
     cada número vem com a sua semana ao lado.

  3. Os gráficos são IMAGEM RASTER — nem texto nem vetor. Conferido: zero
     rótulos "wNN" como texto na página 1 e zero objetos de linha. Então os
     rótulos saem por OCR, com VOTAÇÃO entre 10 leituras (5 resoluções x 2
     modos do tesseract). Não é preciosismo: em leitura única o gráfico GREEN
     devolveu w31 = 3,75 em vez de 8,75 (confusão de 8 com 3). Na votação,
     8,75 ganhou por 8 a 1.

  4. A CIRAD REVISA valores de semanas passadas entre edições. Por isso o
     desempate é pela edição de relatório mais recente, nunca pela ordem em que
     os arquivos aparecem na pasta.

O que sai de UM relatório:
    europa_cirad_precos    5 semanas de Hass 18 + 5 de Green 18
                           + os grupos de calibre da semana (12/14, 16/18/20,
                             22/24, 26), da página 5
    europa_cirad_calibre   ~24 linhas de calibre x origem, da página 4

FONTES DE ENTRADA
    --arquivo a.pdf [b.pdf ...]   PDFs locais
    --pasta   ./PDF               todos os .pdf de uma pasta
    (padrão)                      ownCloud via WebDAV
    --recentes N                  quantos do WebDAV processar (padrão 5)
    --todos                       a pasta inteira (backfill)
    --sem-ocr                     só as tabelas de texto, sem ler gráficos

Produção: o e-mail é Microsoft 365, um flow do Power Automate deposita o PDF na
pasta do ownCloud, este ETL lê por WebDAV. Nenhuma credencial de e-mail entra
no GitHub.

    OWNCLOUD_URL    https://owncloud.empresa.com/remote.php/dav/files/USUARIO
    OWNCLOUD_PASTA  TFruits PowerBI/Projeto Report Avocado/Tropisens/PDF
    OWNCLOUD_USER   usuario
    OWNCLOUD_PASS   senha de APLICATIVO

Requer tesseract-ocr no sistema (apt-get install tesseract-ocr).
"""

import io
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests

try:
    import pdfplumber
except ImportError:                                    # pragma: no cover
    print("ERRO: falta o pdfplumber (pip install pdfplumber).")
    raise

try:
    import pytesseract
    from pytesseract import Output
    TEM_OCR = True
except ImportError:                                    # pragma: no cover
    TEM_OCR = False

TAB_PRECO     = "europa_cirad_precos"
TAB_CALIBRE   = "europa_cirad_calibre"
CHAVE_PRECO   = "ano,semana,grade"
CHAVE_CALIBRE = "ano,semana,variedade,origem,calibre"

FONTE_GRAF = "CIRAD / FruiTrop — gráfico semanal (rótulos por OCR com votação)"
FONTE_TAB  = "CIRAD / FruiTrop — tabela de referência"
FONTE_CAL  = "CIRAD / FruiTrop — prices by grade"

# Faixa de sanidade em €/caixa 4kg. O histórico conhecido vai de ~5 a ~23.
PRECO_MIN, PRECO_MAX = 3.0, 40.0
# Calibres 26 e acima são cotados em €/KG, não por caixa — faixa própria. Sem
# isso o GRADE 26 (2,50 €/kg) é descartado pela guarda do preço de caixa e a
# linha desaparece em silêncio. Aconteceu no primeiro teste, 12/08/2026.
PRECO_KG_MIN, PRECO_KG_MAX = 0.5, 8.0
CALIBRE_EM_KG = 26        # deste calibre para cima, €/kg


def _unidade_do_calibre(grade: str) -> str:
    """'Hass 26' -> EUR/kg · 'Hass 18' / 'Hass 12/14' -> EUR/caixa 4kg"""
    nums = [int(x) for x in re.findall(r"\d+", grade or "")]
    return "EUR/kg" if nums and min(nums) >= CALIBRE_EM_KG else "EUR/caixa 4kg"


def _faixa_ok(valor: float, unidade: str) -> bool:
    if unidade == "EUR/kg":
        return PRECO_KG_MIN <= valor <= PRECO_KG_MAX
    return PRECO_MIN <= valor <= PRECO_MAX
# Variação semanal acima disso é quase certamente erro de leitura, não mercado.
SALTO_MAX_PCT = 45.0

OCR_DPIS = (200, 260, 300, 360, 420)
OCR_PSMS = ("6", "11")

SIMBOLOS = "".join(chr(c) for c in range(0xF000, 0xF100))


def limpa(s) -> str:
    if not s:
        return ""
    s = re.sub(r"\(cid:\d+\)", "tt", str(s))       # ligadura tt do PDF
    s = "".join(ch for ch in s if ch not in SIMBOLOS)
    return re.sub(r"\s+", " ", s).strip()


def num(s):
    m = re.search(r"(-?\d+[.,]?\d*)", s or "")
    return float(m.group(1).replace(",", ".")) if m else None


def inteiro(s):
    m = re.search(r"(\d+)", s or "")
    return int(m.group(1)) if m else None


def ano_da_semana(ano_rel: int, sem_rel: int, sem: int) -> int:
    """Semana maior que a do relatório pertence ao ano anterior (virada de ano)."""
    return ano_rel - 1 if sem > sem_rel else ano_rel


def _origem_limpa(s: str) -> str:
    s = re.sub(r"\([^)]*\)", "", limpa(s))
    return re.sub(r"\s+", " ", s).strip(" /")


def _faixa(celula: str):
    """'(9.00) 9.50 / 10.50' -> (9.50, 10.50). Célula com sub-grupos -> (None, None)."""
    t = limpa(celula)
    if not t:
        return None, None
    principal = re.sub(r"\([^)]*\)", " ", t)
    if principal.count(":") >= 2:
        return None, None
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+[.,]?\d*", principal)]
    if len(nums) == 1:
        return nums[0], nums[0]
    if len(nums) == 2:
        return min(nums), max(nums)
    return None, None


# ── LEITURA DOS GRÁFICOS DE BARRAS ────────────────────────────────────────
def _ocr_uma(pg, im, dpi, psm) -> dict:
    """
    {semana: valor} de UMA leitura.

    Usa POSIÇÃO para separar rótulo de dado de rótulo de eixo: o eixo Y fica à
    esquerda da primeira barra, e os valores ficam acima das barras alinhados no
    x do rótulo da semana. Sem isso, "10,00" do eixo Y entra como se fosse dado.
    """
    bbox = (im["x0"], im["top"], im["x1"], im["bottom"])
    pil = pg.crop(bbox).to_image(resolution=dpi).original
    d = pytesseract.image_to_data(pil, config=f"--psm {psm}", output_type=Output.DICT)
    itens = [{"t": (t or "").strip(),
              "x": d["left"][i] + d["width"][i] / 2,
              "y": d["top"][i] + d["height"][i] / 2}
             for i, t in enumerate(d["text"]) if (t or "").strip()]

    semanas = []
    for it in itens:
        m = re.fullmatch(r"w\s?(\d{1,2})", it["t"], re.I)
        if m:
            semanas.append((int(m.group(1)), it["x"], it["y"]))
    if not semanas:
        return {}
    semanas.sort(key=lambda s: s[1])
    x_min = min(s[1] for s in semanas)
    y_eixo = min(s[2] for s in semanas)

    valores = []
    for it in itens:
        m = re.fullmatch(r"(\d{1,2})[.,](\d{2})", it["t"])
        if not m:
            continue
        if it["y"] >= y_eixo - 5:          # na linha das semanas ou abaixo dela
            continue
        if it["x"] < x_min - 12:           # rótulo do eixo Y
            continue
        valores.append((float(f"{m.group(1)}.{m.group(2)}"), it["x"]))

    out, usados = {}, set()
    for s, xs, _ in semanas:
        cand = sorted((abs(v[1] - xs), k)
                      for k, v in enumerate(valores) if k not in usados)
        if cand and cand[0][0] < 40:
            usados.add(cand[0][1])
            out[s] = valores[cand[0][1]][0]
    return out


def ler_grafico(pg, im, rotulo: str) -> dict:
    """
    {semana: valor} com votação entre OCR_DPIS x OCR_PSMS.

    Aceita um valor só com >=3 votos E >=60% entre as leituras que ACHARAM
    aquela semana. Algumas leituras simplesmente não enxergam um rótulo; isso é
    ausência, não discordância, e não pode contar como voto contra.
    """
    leituras = []
    for dpi in OCR_DPIS:
        for psm in OCR_PSMS:
            try:
                r = _ocr_uma(pg, im, dpi, psm)
            except Exception:                          # noqa: BLE001
                continue
            if r:
                leituras.append(r)
    if not leituras:
        return {}

    final = {}
    for s in sorted({k for r in leituras for k in r}):
        votos = Counter(r[s] for r in leituras if s in r)
        valor, n = votos.most_common(1)[0]
        total = sum(votos.values())
        if n < 3 or n / total < 0.6:
            print(f"      {rotulo} w{s}: sem consenso {dict(votos)} — descartada")
            continue
        if len(votos) > 1:
            print(f"      {rotulo} w{s}: {dict(votos)} -> {valor} (votação)")
        final[s] = valor
    return final


def _valida_serie(serie: dict, rotulo: str) -> dict:
    """Tira valores fora da faixa e sinaliza saltos implausíveis."""
    ok = {}
    for s in sorted(serie):
        v = serie[s]
        if not (PRECO_MIN <= v <= PRECO_MAX):
            print(f"      {rotulo} w{s}: {v} fora da faixa "
                  f"{PRECO_MIN}-{PRECO_MAX} — descartado")
            continue
        ok[s] = v
    semanas = sorted(ok)
    for a, b in zip(semanas, semanas[1:]):
        if b - a != 1 or not ok[a]:
            continue
        salto = abs(ok[b] - ok[a]) / ok[a] * 100
        if salto > SALTO_MAX_PCT:
            print(f"      {rotulo} w{a}->w{b}: salto de {salto:.0f}% "
                  f"({ok[a]} -> {ok[b]}) — confira o PDF na mão")
    return ok


# ── PARSER DO RELATÓRIO ───────────────────────────────────────────────────
def parse_pdf(fonte, rotulo: str, usar_ocr: bool = True) -> tuple[list, list]:
    precos, calibres = [], []
    with pdfplumber.open(fonte) as pdf:
        if len(pdf.pages) < 5:
            print(f"    {rotulo}: só {len(pdf.pages)} páginas — não parece o "
                  f"relatório CIRAD")
            return [], []

        pg1 = pdf.pages[0]
        p1 = limpa(pg1.extract_text())
        m = re.search(r"AVOCADO REPORT WEEK\s+(\d{1,2})", p1, re.I)
        if not m:
            print(f"    {rotulo}: não achei 'AVOCADO REPORT WEEK' na página 1")
            return [], []
        sem_rel = int(m.group(1))
        m = re.search(r"\b(20\d{2})\b", p1)
        if not m:
            print(f"    {rotulo}: não achei o ano na página 1")
            return [], []
        ano_rel = int(m.group(1))
        agora = datetime.now(timezone.utc).isoformat()

        def base(sem, grade, valor, fonte_txt, **extra):
            d = {"ano": ano_da_semana(ano_rel, sem_rel, sem), "semana": sem,
                 "grade": grade, "preco_eur": round(valor, 2),
                 "unidade": _unidade_do_calibre(grade), "relatorio_ano": ano_rel,
                 "relatorio_semana": sem_rel, "arquivo": rotulo,
                 "fonte": fonte_txt, "extracted_at": agora}
            d.update(extra)
            return d

        # ── tabela da página 1: variação e comparativo ────────────────────
        tab_valor = tab_semana = var_eur = var_pct = None
        tabs1 = pg1.extract_tables()
        if tabs1 and len(tabs1[0]) >= 2:
            hdr = [limpa(c) for c in tabs1[0][0]]
            val = [limpa(c) for c in tabs1[0][1]]
            tab_semana = inteiro(hdr[1]) if len(hdr) > 1 else None
            tab_valor = num(val[1]) if len(val) > 1 else None
            var_eur = num(val[2]) if len(val) > 2 else None
            var_pct = num(val[3]) if len(val) > 3 else None
            if tab_semana is not None and tab_semana != sem_rel:
                print(f"    {rotulo}: rótulo da tabela diz semana {tab_semana}, "
                      f"relatório é da {sem_rel} — vale a do relatório")

        # ── gráficos de barras rotulados (fonte primária) ─────────────────
        series = {}
        if usar_ocr and TEM_OCR:
            for grade, x0 in (("Hass 18", 28), ("Green 18", 306)):
                cand = [i for i in pg1.images
                        if abs(i["x0"] - x0) < 12 and abs(i["top"] - 376) < 12
                        and i["width"] > 150 and i["height"] > 120]
                if not cand:
                    print(f"    {rotulo}: gráfico de {grade} não localizado")
                    continue
                serie = _valida_serie(ler_grafico(pg1, cand[0], grade), grade)
                if serie:
                    series[grade] = serie
                    print(f"    {rotulo}: {grade} -> " + " ".join(
                        f"w{s}={serie[s]:.2f}" for s in sorted(serie)))
        elif usar_ocr and not TEM_OCR:
            print(f"    {rotulo}: pytesseract ausente — só tabelas de texto")

        for grade, serie in series.items():
            for s, v in serie.items():
                extra = ({"variacao_eur": var_eur, "variacao_media_pct": var_pct}
                         if s == sem_rel else {})
                precos.append(base(s, grade, v, FONTE_GRAF, **extra))

        # conferência cruzada: a tabela tem que bater com a barra da semana
        alvo = series.get("Hass 18", {}).get(sem_rel)
        if tab_valor is not None and alvo is not None and abs(tab_valor - alvo) > 0.011:
            print(f"    {rotulo}: DIVERGÊNCIA tabela {tab_valor} x gráfico "
                  f"w{sem_rel} {alvo} — fica o do gráfico, que traz a semana "
                  f"ao lado do número")

        # sem OCR (ou OCR falhou), cai para a tabela: uma semana é melhor que zero
        if not series and tab_valor is not None:
            if _faixa_ok(tab_valor, "EUR/caixa 4kg"):
                precos.append(base(sem_rel, "Hass 18", tab_valor, FONTE_TAB,
                                   variacao_eur=var_eur,
                                   variacao_media_pct=var_pct))
            else:
                print(f"    {rotulo}: tabela com {tab_valor}, fora da faixa")

        # ── página 5: preço por grupo de calibre ──────────────────────────
        for tb in pdf.pages[4].extract_tables():
            plano = [limpa(c) for row in tb for c in row]
            titulo = next((t for t in plano if re.match(r"GRADES?\s", t, re.I)), None)
            if not titulo:
                continue
            linha = next((row for row in tb
                          if any("€" in (limpa(c) or "") for c in row)), None)
            if not linha:
                continue
            cels = [limpa(c) for c in linha if limpa(c)]
            v = num(cels[0]) if cels else None
            grade = "Hass " + re.sub(r"^GRADES?\s+", "", titulo, flags=re.I).strip()
            if v is None or not _faixa_ok(v, _unidade_do_calibre(grade)):
                if v is not None:
                    print(f"    {rotulo}: {grade} com {v} fora da faixa de "
                          f"{_unidade_do_calibre(grade)} — descartado")
                continue
            precos.append(base(sem_rel, grade, v, FONTE_TAB,
                               variacao_eur=num(cels[1]) if len(cels) > 1 else None,
                               variacao_media_pct=num(cels[2]) if len(cels) > 2 else None))

        # ── página 4: calibre x origem ────────────────────────────────────
        tabs4 = pdf.pages[3].extract_tables()
        if tabs4 and len(tabs4[0]) > 2:
            tb = tabs4[0]
            variedades = [limpa(c) for c in tb[0]]
            origens = [_origem_limpa(c) for c in tb[1]]
            for row in tb[2:]:
                cels = [limpa(c) for c in row]
                if not cels or not cels[0]:
                    continue
                unidade = "EUR/kg" if "/kg" in cels[0].lower() else "EUR/caixa 4kg"
                cal = re.sub(r"\(.*?\)", "", cels[0]).strip()
                for k in range(1, min(len(cels), len(origens))):
                    if not cels[k] or not origens[k]:
                        continue
                    mn, mx = _faixa(cels[k])
                    var = ("Green" if k < len(variedades)
                           and "green" in (variedades[k] or "").lower() else "Hass")
                    calibres.append({
                        "ano": ano_rel, "semana": sem_rel, "variedade": var,
                        "origem": origens[k], "calibre": cal,
                        "preco_min": mn, "preco_max": mx, "unidade": unidade,
                        "texto_original": cels[k], "arquivo": rotulo,
                        "fonte": FONTE_CAL, "extracted_at": agora})
        else:
            print(f"    {rotulo}: página 4 sem a grade de calibres")

    return precos, calibres


# ── ENTRADA: WEBDAV (ownCloud) ────────────────────────────────────────────
def webdav_config():
    url = os.environ.get("OWNCLOUD_URL", "").rstrip("/")
    pasta = os.environ.get("OWNCLOUD_PASTA", "").strip("/")
    user = os.environ.get("OWNCLOUD_USER", "")
    senha = os.environ.get("OWNCLOUD_PASS", "")
    faltando = [k for k, v in (("OWNCLOUD_URL", url), ("OWNCLOUD_PASTA", pasta),
                               ("OWNCLOUD_USER", user), ("OWNCLOUD_PASS", senha))
                if not v]
    if faltando:
        raise EnvironmentError("WebDAV não configurado. Faltando: "
                               + ", ".join(faltando)
                               + "\nUse senha de APLICATIVO do ownCloud.")
    return url, pasta, (user, senha)


def webdav_listar_pdfs():
    url, pasta, auth = webdav_config()
    alvo = f"{url}/{requests.utils.quote(pasta)}/"
    corpo = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
             '<d:prop><d:getlastmodified/></d:prop></d:propfind>')
    r = requests.request("PROPFIND", alvo, auth=auth, data=corpo, timeout=120,
                         headers={"Depth": "1", "Content-Type": "application/xml"})
    r.raise_for_status()
    raiz = ElementTree.fromstring(r.content)
    achados = []
    for resp in raiz.findall("{DAV:}response"):
        href = resp.findtext("{DAV:}href") or ""
        if not href.lower().endswith(".pdf"):
            continue
        mod = resp.find(".//{DAV:}getlastmodified")
        achados.append((requests.utils.unquote(href),
                        mod.text if mod is not None else ""))

    def quando(par):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(par[1])
        except Exception:                              # noqa: BLE001
            return None
    if achados and all(quando(a) for a in achados):
        achados.sort(key=quando, reverse=True)
    else:
        achados.sort(key=lambda a: a[0], reverse=True)
    print(f"  WebDAV: {len(achados)} PDFs na pasta")
    return alvo, auth, achados


def webdav_baixar(base_url, auth, href):
    nome = href.split("/")[-1]
    r = requests.get(f"{base_url}{requests.utils.quote(nome)}", auth=auth,
                     timeout=300)
    r.raise_for_status()
    return nome, io.BytesIO(r.content)


def main() -> int:
    argv = sys.argv
    dry = "--dry-run" in argv
    usar_ocr = "--sem-ocr" not in argv

    entradas = []
    if "--arquivo" in argv:
        i = argv.index("--arquivo") + 1
        while i < len(argv) and not argv[i].startswith("--"):
            p = Path(argv[i])
            if p.exists():
                entradas.append((p.name, str(p)))
            else:
                print(f"  AVISO: {p} não existe")
            i += 1
    elif "--pasta" in argv:
        d = Path(argv[argv.index("--pasta") + 1])
        entradas = [(p.name, str(p)) for p in sorted(d.glob("*.pdf"))]
        print(f"  pasta {d}: {len(entradas)} PDFs")
    else:
        base, auth, achados = webdav_listar_pdfs()
        limite = None if "--todos" in argv else 5
        if "--recentes" in argv:
            limite = int(argv[argv.index("--recentes") + 1])
        if limite and len(achados) > limite:
            print(f"  processando os {limite} mais recentes (--todos para todos)")
            achados = achados[:limite]
        for href, _ in achados:
            entradas.append(webdav_baixar(base, auth, href))

    if not entradas:
        print("  ERRO: nenhum PDF para processar.")
        return 1

    print(f"ETL Europa — CIRAD · {len(entradas)} PDF(s) · OCR "
          f"{'ligado' if usar_ocr and TEM_OCR else 'desligado'} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)

    precos, calibres = [], []
    for rotulo, fonte in entradas:
        try:
            p, c = parse_pdf(fonte, rotulo, usar_ocr)
        except Exception as e:                          # noqa: BLE001
            print(f"    {rotulo}: erro de leitura ({e})")
            continue
        precos += p
        calibres += c

    if not precos:
        print("  ERRO: nenhum preço extraído.")
        return 1

    # A CIRAD revisa semanas passadas entre edições. Vence a edição mais nova.
    def edicao(r):
        return (r.get("relatorio_ano") or 0, r.get("relatorio_semana") or 0)

    porchave = {}
    for r in sorted(precos, key=edicao):
        k = (r["ano"], r["semana"], r["grade"])
        ant = porchave.get(k)
        if ant and abs(ant["preco_eur"] - r["preco_eur"]) > 0.011:
            print(f"  revisão em {k[2]} S{k[1]}/{k[0]}: {ant['preco_eur']:.2f} "
                  f"(relatório S{ant['relatorio_semana']}) -> "
                  f"{r['preco_eur']:.2f} (relatório S{r['relatorio_semana']}) "
                  f"— fica o mais recente")
        porchave[k] = r
    precos = list(porchave.values())

    porcal = {}
    for c in sorted(calibres, key=lambda x: (x["ano"], x["semana"])):
        porcal[(c["ano"], c["semana"], c["variedade"], c["origem"],
                c["calibre"])] = c
    calibres = list(porcal.values())

    porgrade = {}
    for r in precos:
        porgrade.setdefault(r["grade"], []).append(r)
    print(f"\n  {len(precos)} linhas de preço em {len(porgrade)} grades:")
    for g in sorted(porgrade):
        ss = sorted(porgrade[g], key=lambda r: (r["ano"], r["semana"]))
        print(f"    {g:16s} " + " ".join(
            f"S{r['semana']}/{str(r['ano'])[2:]}={r['preco_eur']:.2f}" for r in ss))
    print(f"  {len(calibres)} linhas de calibre x origem")

    if dry:
        print("\n  --dry-run: nada foi gravado.")
        return 0

    print("\n[2] Upsert no Supabase...", flush=True)
    import supabase_upsert
    falhou = False
    for tabela, dados, chave in ((TAB_PRECO, precos, CHAVE_PRECO),
                                 (TAB_CALIBRE, calibres, CHAVE_CALIBRE)):
        if not dados:
            continue
        res = supabase_upsert.upsert(tabela, dados, on_conflict=chave)
        if res["errors"]:
            falhou = True
            for e in res["errors"][:3]:
                print(f"  ERRO {tabela} lote {e['batch_start']}: "
                      f"HTTP {e['status']} — {e['detail']}")
        else:
            print(f"  OK {tabela}: {res['inserted']} registros")
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
