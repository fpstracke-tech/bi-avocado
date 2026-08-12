"""
ETL Preços Europa — CIRAD / FruiTrop / Tropisens (PDF que chega por e-mail)
===========================================================================
SUBSTITUI o processo manual: o relatório chega por e-mail, alguém salva o PDF e
um PRINT vai para o Power BI. Não existia série histórica, só a foto da semana.

O fornecedor não tem API (já perguntado). O PDF resolve — mas o relatório teve
QUATRO GERAÇÕES DE LAYOUT entre 2023 e 2026, e cada uma quebra o parser da
outra. Mapa levantado em 12/08/2026 sobre 6 relatórios de anos diferentes:

    arquivo              pgs  cabeçalho da tabela p1              valor
    Avocado 15-23.pdf     8   Week 15 | Week 15/14 | 2023/2022    14.01 €
    Avocado 50-23.pdf     7   Week 50 | Week 50/49                13.52 €
    Avocado 01-24.pdf     7   Week 01 | Week 01/52                14.02 €
    Avocado 01-25.pdf     5   Week 01 | Week 01/52                (vazio)
    Avocado 46-25.pdf    10   Week 46 | Week 46/ 45                9.36 €
    Week 31-2026.pdf      8   Week 30 | Week 29                   10.00 €

O que isso obrigou:

  1. SEMANA DO PREÇO = SEMANA DO RELATÓRIO. A notação antiga era explícita
     ("Week 15 | Week 15/14" = valor da 15, variação de 14 para 15). O template
     de 2026 degenerou para "Week 30 | Week 29" e o rótulo ficou uma semana
     atrasado: o gráfico do mesmo PDF mostra w30 = 10,22 e w31 = 10,00, e o
     relatório da semana 30 publicou "Week 30 = 10,22". Confiar no rótulo de
     2026 gravaria S30 = 10,00 por cima do correto e perderia a S31.

  2. O VALOR SAI DO TEXTO, não da célula da tabela. Em 2025 semana 01 a tabela
     é extraída vazia — os números moram fora das células. O texto funciona nos
     6 layouts.

  3. A ÂNCORA DO TÍTULO MUDA. De "EU Reference Price—Hass grade 18" (CIRAD)
     para "EU Barometer—Hass grade 18" (Tropisens, 2025 s39 em diante).

  4. NÚMERO DE PÁGINAS VARIA DE 5 A 10. Nada pode ser buscado por índice de
     página; tudo é por conteúdo.

  5. OS GRÁFICOS MUDARAM DE NATUREZA. Em 2023-2025 os rótulos são TEXTO e saem
     exatos. Em 2026 são IMAGEM RASTER (zero texto "wNN" na página) e exigem
     OCR — com votação entre 10 leituras, porque numa leitura única o gráfico
     GREEN devolveu 3,75 em vez de 8,75 (confusão de 8 com 3).

  6. ZERO É "SEM DADO". Em 2025 semana 01 o gráfico mostra w2 e w3 = 0,00
     porque o ano acabou de começar. A guarda de faixa já barra.

  7. A CIRAD REVISA semanas passadas entre edições. O desempate é pela edição
     de relatório mais recente, nunca pela ordem dos arquivos na pasta.

O que sai de UM relatório: a semana do relatório pela tabela (exato, sempre) +
até 5 semanas por gráfico (Hass e Green) + os grupos de calibre da semana +
a grade calibre × origem.

FONTES DE ENTRADA
    --arquivo a.pdf [b.pdf ...]   PDFs locais
    --pasta   ./PDF               recursivo, pega subpastas por ano
    (padrão)                      ownCloud via WebDAV
    --recentes N                  quantos do WebDAV processar (padrão 5)
    --todos                       a pasta inteira (backfill)
    --sem-ocr                     só texto, sem OCR (mais rápido no histórico)

Produção: e-mail Microsoft 365 -> Power Automate deposita na pasta do ownCloud
-> este ETL lê por WebDAV. Nenhuma credencial de e-mail entra no GitHub.

    OWNCLOUD_URL / OWNCLOUD_PASTA / OWNCLOUD_USER / OWNCLOUD_PASS

OCR exige tesseract-ocr no sistema (apt-get install tesseract-ocr).
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

FONTE_TAB  = "CIRAD/Tropisens — tabela de referência (texto)"
FONTE_TXT  = "CIRAD/Tropisens — rótulos do gráfico (texto)"
FONTE_OCR  = "CIRAD/Tropisens — rótulos do gráfico (OCR com votação)"
FONTE_CAL  = "CIRAD/Tropisens — prices by grade"

# €/caixa 4kg: histórico conhecido vai de ~7,5 a ~23. €/kg: calibres 26+.
PRECO_MIN, PRECO_MAX = 3.0, 40.0
PRECO_KG_MIN, PRECO_KG_MAX = 0.5, 8.0
CALIBRE_EM_KG = 26
SALTO_MAX_PCT = 45.0

OCR_DPIS = (200, 260, 300, 360, 420)
OCR_PSMS = ("6", "11")

SIMBOLOS = "".join(chr(c) for c in range(0xF000, 0xF100))

# Rótulos de variedade que às vezes aparecem na linha de origens do PDF.
ORIGEM_NAO_E_PAIS = {"hass", "green", "greens", "varieties", "green varieties",
                     "hass varieties", "variety", "grade", "grades", "-", "="}

RE_ANCORA = re.compile(
    r"EU\s+(?:Reference\s+Price|Barometer)\s*[—–-]\s*Hass\s+grade\s+18", re.I)
RE_REF = re.compile(
    r"([\d]{1,2}[.,]\d{2})\s*€\s*"
    r"([+-]\s*[\d]{1,2}[.,]\d{2})\s*€?\s*"
    r"([+-]?\s*\d{1,3})\s*%", re.S)
RE_SEMANAS = (
    re.compile(r"AVOCADO\s+REPORT\s+WEEK\s*\n?\s*(\d{1,2})", re.I),
    re.compile(r"WEEK\s*\n?\s*(\d{1,2})\s*\n?\s*AVOCADO\s+MARKET\s+REPORT", re.I),
    re.compile(r"WEEK\s*\n?\s*AVOCADO\s+MARKET\s+REPORT\s*\n?\s*(\d{1,2})", re.I),
)
RE_CAB_TABELA = re.compile(r"Week\s*0?(\d{1,2})", re.I)


def limpa(s) -> str:
    if not s:
        return ""
    s = re.sub(r"\(cid:\d+\)", "tt", str(s))
    s = "".join(ch for ch in s if ch not in SIMBOLOS)
    return re.sub(r"\s+", " ", s).strip()


def _f(s):
    return float(re.sub(r"[^\d.,-]", "", str(s)).replace(",", "."))


def unidade_do_grade(grade: str) -> str:
    nums = [int(x) for x in re.findall(r"\d+", grade or "")]
    return "EUR/kg" if nums and min(nums) >= CALIBRE_EM_KG else "EUR/caixa 4kg"


def faixa_ok(valor, unidade) -> bool:
    if valor is None:
        return False
    if unidade == "EUR/kg":
        return PRECO_KG_MIN <= valor <= PRECO_KG_MAX
    return PRECO_MIN <= valor <= PRECO_MAX


# Um gráfico cobre no máximo ~5 semanas para trás, então uma semana só um pouco
# ADIANTE da do relatório nunca é virada de ano — é erro de cabeçalho. Virada de
# ano de verdade dá salto grande (relatório S1 com barras w50, w51, w52).
# Sem esse limiar, o Avocado 18-25.pdf (que traz "17" no cabeçalho por erro de
# digitação da CIRAD) jogava a semana 18 para 2024.
SALTO_VIRADA_ANO = 26


def ano_da_semana(ano_rel: int, sem_rel: int, sem: int) -> int:
    return ano_rel - 1 if (sem - sem_rel) >= SALTO_VIRADA_ANO else ano_rel


# ── CABEÇALHO: SEMANA E ANO ───────────────────────────────────────────────
def semana_ano(p1: str, nome: str, rotulo: str):
    sem = ano = None
    for r in RE_SEMANAS:
        m = r.search(p1)
        if m:
            sem = int(m.group(1))
            break
    m = re.search(r"\b(20\d{2})\b", p1)
    if m:
        ano = int(m.group(1))

    # o nome do arquivo é a segunda opinião: "Avocado 15-23.pdf",
    # "CIRAD avocado report Week 31-2026.pdf"
    mf = re.search(r"(\d{1,2})\s*-\s*(\d{2,4})", nome)
    sem_f = ano_f = None
    if mf:
        sem_f = int(mf.group(1))
        a = mf.group(2)
        ano_f = int(a) if len(a) == 4 else 2000 + int(a)

    if sem and sem_f and sem != sem_f:
        print(f"    {rotulo}: semana do documento ({sem}) difere do nome do "
              f"arquivo ({sem_f}) — uso a do documento")
    if ano and ano_f and ano != ano_f:
        print(f"    {rotulo}: ano do documento ({ano}) difere do nome ({ano_f})")
    return (sem or sem_f), (ano or ano_f)


def ref_do_texto(p1: str):
    """(valor, variacao_eur, variacao_pct) do bloco de preço de referência."""
    m0 = RE_ANCORA.search(p1)
    if not m0:
        return None
    m = RE_REF.search(p1[m0.end():m0.end() + 500])
    if not m:
        return None
    return _f(m.group(1)), _f(m.group(2)), _f(m.group(3))


def semana_declarada(pg1) -> int | None:
    """Primeira célula 'Week NN' da tabela da página 1, só para conferência."""
    for tb in pg1.extract_tables():
        for row in tb:
            for c in row:
                m = RE_CAB_TABELA.fullmatch(limpa(c) or "")
                if m:
                    return int(m.group(1))
    return None


# ── GRÁFICOS ──────────────────────────────────────────────────────────────
def _agrupa(marcas, folga=60):
    """Separa os rótulos de semana em gráficos distintos por salto no eixo x."""
    marcas = sorted(marcas, key=lambda m: m["x"])
    grupos, atual = [], []
    for m in marcas:
        if atual and m["x"] - atual[-1]["x"] > folga:
            grupos.append(atual)
            atual = []
        atual.append(m)
    if atual:
        grupos.append(atual)
    return grupos


def _casa(grupo, valores):
    """Casa cada semana com o valor decimal alinhado no mesmo x."""
    x0 = min(g["x"] for g in grupo) - 12
    x1 = max(g["x"] for g in grupo) + 25
    y_eixo = min(g["y"] for g in grupo)
    cand = [v for v in valores if x0 <= v["x"] <= x1 and v["y"] < y_eixo - 5]
    out, usados = {}, set()
    for g in sorted(grupo, key=lambda g: g["x"]):
        prox = sorted((abs(v["x"] - g["x"]), k)
                      for k, v in enumerate(cand) if k not in usados)
        if prox and prox[0][0] < 22:
            usados.add(prox[0][1])
            out[g["s"]] = cand[prox[0][1]]["v"]
    return out


def identifica_por_titulo(grupo, palavras) -> str | None:
    """
    'Hass 18' ou 'Green 18' pelo TÍTULO do gráfico.

    Necessário porque a posição não serve: em 2023 semana 15 existe UM gráfico
    só (o do Green), e o do Hass está rotacionado e ilegível. Um "usa o
    primeiro" gravou 8,66 do Green como se fosse Hass 18, quando a tabela do
    mesmo PDF dizia 14,01. Título é o único critério que não chuta.

    A janela é estreita de propósito: o texto corrido "EU Reference Price—Hass
    grade 18" fica no alto da página e não pode ser confundido com o título de
    um gráfico que está mais abaixo.
    """
    x0 = min(g["x"] for g in grupo) - 40
    x1 = max(g["x"] for g in grupo) + 40
    y_sem = min(g["y"] for g in grupo)
    achados = []
    for w in palavras:
        t = (w["text"] or "").strip().upper()
        if t not in ("HASS", "GREEN"):
            continue
        xm = (w["x0"] + w["x1"]) / 2
        ym = (w["top"] + w["bottom"]) / 2
        if x0 <= xm <= x1 and (y_sem - 220) <= ym <= (y_sem - 20):
            achados.append((abs(ym - y_sem), t))
    if not achados:
        return None
    achados.sort()
    return "Hass 18" if achados[0][1] == "HASS" else "Green 18"


def graficos_texto(pg) -> list[dict]:
    """[{semana: valor}] por gráfico, quando os rótulos são TEXTO (2023-2025)."""
    ws = pg.extract_words(keep_blank_chars=False)
    marcas, valores = [], []
    for w in ws:
        t = w["text"]
        xm, ym = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        m = re.fullmatch(r"w\s?(\d{1,2})", t, re.I)
        if m:
            marcas.append({"s": int(m.group(1)), "x": xm, "y": ym})
            continue
        m = re.fullmatch(r"(\d{1,2})[.,](\d{2})", t)
        if m:
            valores.append({"v": float(f"{m.group(1)}.{m.group(2)}"), "x": xm, "y": ym})
    saida = []
    for g in _agrupa(marcas):
        s = _casa(g, valores)
        if s:
            saida.append((s, identifica_por_titulo(g, ws)))
    return saida


def _ocr_uma(pg, im, dpi, psm) -> dict:
    bbox = (im["x0"], im["top"], im["x1"], im["bottom"])
    pil = pg.crop(bbox).to_image(resolution=dpi).original
    d = pytesseract.image_to_data(pil, config=f"--psm {psm}", output_type=Output.DICT)
    marcas, valores = [], []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        if not t:
            continue
        xm = d["left"][i] + d["width"][i] / 2
        ym = d["top"][i] + d["height"][i] / 2
        m = re.fullmatch(r"w\s?(\d{1,2})", t, re.I)
        if m:
            marcas.append({"s": int(m.group(1)), "x": xm, "y": ym})
            continue
        m = re.fullmatch(r"(\d{1,2})[.,](\d{2})", t)
        if m:
            valores.append({"v": float(f"{m.group(1)}.{m.group(2)}"), "x": xm, "y": ym})
    if not marcas:
        return {}
    x0 = min(m["x"] for m in marcas) - 12
    y_eixo = min(m["y"] for m in marcas)
    cand = [v for v in valores if v["x"] >= x0 and v["y"] < y_eixo - 5]
    out, usados = {}, set()
    for g in sorted(marcas, key=lambda g: g["x"]):
        prox = sorted((abs(v["x"] - g["x"]), k)
                      for k, v in enumerate(cand) if k not in usados)
        if prox and prox[0][0] < 40:
            usados.add(prox[0][1])
            out[g["s"]] = cand[prox[0][1]]["v"]
    return out


def grafico_ocr(pg, im, rotulo: str) -> dict:
    """
    Votação entre OCR_DPIS x OCR_PSMS. Aceita valor com >=3 votos e >=60% entre
    as leituras que ACHARAM aquela semana — leitura que não enxerga o rótulo é
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
    final = {}
    for s in sorted({k for r in leituras for k in r}):
        votos = Counter(r[s] for r in leituras if s in r)
        valor, n = votos.most_common(1)[0]
        if n < 3 or n / sum(votos.values()) < 0.6:
            print(f"      {rotulo} w{s}: sem consenso {dict(votos)} — descartada")
            continue
        if len(votos) > 1:
            print(f"      {rotulo} w{s}: {dict(votos)} -> {valor} (votação)")
        final[s] = valor
    return final


def graficos_ocr(pg, rotulo: str) -> list[dict]:
    ims = sorted([i for i in pg.images if i["width"] > 150 and i["height"] > 120],
                 key=lambda i: (round(i["top"]), i["x0"]))
    saida = []
    for im in ims:
        s = grafico_ocr(pg, im, rotulo)
        if len(s) >= 2:
            saida.append((s, None))       # título está dentro da imagem
    return saida


def valida_serie(serie: dict, unidade: str, rotulo: str) -> dict:
    ok = {}
    for s in sorted(serie):
        if faixa_ok(serie[s], unidade):
            ok[s] = serie[s]
        elif serie[s]:
            print(f"      {rotulo} w{s}: {serie[s]} fora da faixa de "
                  f"{unidade} — descartado")
    semanas = sorted(ok)
    for a, b in zip(semanas, semanas[1:]):
        if b - a == 1 and ok[a]:
            salto = abs(ok[b] - ok[a]) / ok[a] * 100
            if salto > SALTO_MAX_PCT:
                print(f"      {rotulo} w{a}->w{b}: salto de {salto:.0f}% "
                      f"({ok[a]} -> {ok[b]}) — confira o PDF")
    return ok


# ── TABELAS POR CONTEÚDO (número de páginas varia de 5 a 10) ──────────────
def grupos_de_calibre(pdf):
    """
    [(grade, valor, delta, pct)] das CAIXAS pequenas 'GRADES 12/14' etc.

    O filtro é estreito de propósito. Os layouts antigos rotulam as linhas da
    grade calibre x origem como "Grade 12", "Grade 14" — e um filtro solto casa
    com a grade inteira e grava a cotação de uma origem como se fosse preço de
    referência do calibre. Aconteceu no teste de 12/08/2026: apareceram grades
    "Hass 10" e "Hass 12" que eram, na verdade, a cotação da Colômbia e da RSA.

    A caixa de verdade tem poucas linhas, um cabeçalho "Week NN" e um título que
    é SÓ "GRADES <numeros>". A grade não tem cabeçalho de semana.
    """
    saida = []
    for pg in pdf.pages:
        for tb in pg.extract_tables():
            if len(tb) > 5:
                continue                       # grade grande, não é caixa
            plano = [limpa(c) for row in tb for c in row]
            if not any(re.match(r"Week\s*\d", c or "", re.I) for c in plano):
                continue                       # caixa de referência tem semana
            titulo = next((t for t in plano
                           if re.fullmatch(r"GRADES?\s+[\d/\s]+", t or "", re.I)), None)
            if not titulo:
                continue
            linha = next((row for row in tb
                          if any("€" in (limpa(c) or "") for c in row)), None)
            if not linha:
                continue
            cels = [limpa(c) for c in linha if limpa(c)]
            grade = "Hass " + re.sub(r"^GRADES?\s+", "", titulo, flags=re.I).strip()
            try:
                v = _f(cels[0])
            except Exception:                          # noqa: BLE001
                continue
            d = p = None
            for extra in cels[1:3]:
                try:
                    if "%" in extra:
                        p = _f(extra)
                    elif d is None:
                        d = _f(extra)
                except Exception:                      # noqa: BLE001
                    pass
            saida.append((grade, v, d, p))
    return saida


def grade_origem(pdf):
    """A grade calibre x origem, achada por conteúdo em qualquer página."""
    for pg in pdf.pages:
        for tb in pg.extract_tables():
            if len(tb) < 5:
                continue
            # os layouts antigos escrevem "Grade 12"; os novos, só "12"
            primeira = [limpa(r[0]) if r else "" for r in tb]
            n = sum(1 for c in primeira
                    if re.fullmatch(r"(?:Grade\s*)?\d{2}(\s*\(.*\))?", c or "", re.I))
            if n >= 4:
                return tb
    return None


# ── PARSER ────────────────────────────────────────────────────────────────
def parse_pdf(fonte, rotulo: str, usar_ocr: bool = True) -> tuple[list, list]:
    precos, calibres = [], []
    with pdfplumber.open(fonte) as pdf:
        pg1 = pdf.pages[0]
        p1 = limpa(pg1.extract_text())
        sem_rel, ano_rel = semana_ano(pdf.pages[0].extract_text() or "", rotulo, rotulo)
        if not sem_rel or not ano_rel:
            print(f"    {rotulo}: não identifiquei semana/ano — pulando")
            return [], []
        agora = datetime.now(timezone.utc).isoformat()

        def reg(sem, grade, valor, fonte_txt, **extra):
            u = unidade_do_grade(grade)
            # variacao_* SEMPRE presentes, mesmo vazias: o PostgREST rejeita
            # lote cujos objetos não tenham o MESMO conjunto de chaves
            # (PGRST102 "All object keys must match"). As linhas da tabela têm
            # variação, as do gráfico não — e isso derrubou a carga em 12/08/2026.
            d = {"ano": ano_da_semana(ano_rel, sem_rel, sem), "semana": sem,
                 "grade": grade, "preco_eur": round(valor, 2), "unidade": u,
                 "variacao_eur": None, "variacao_media_pct": None,
                 "relatorio_ano": ano_rel, "relatorio_semana": sem_rel,
                 "arquivo": rotulo, "fonte": fonte_txt, "extracted_at": agora}
            d.update(extra)
            return d

        # ── preço de referência (texto: funciona nos 4 layouts) ───────────
        ref = ref_do_texto(pdf.pages[0].extract_text() or "")
        if not ref:
            print(f"    {rotulo}: bloco de preço de referência não encontrado")
        elif not faixa_ok(ref[0], "EUR/caixa 4kg"):
            print(f"    {rotulo}: referência {ref[0]} fora da faixa — descartada")
            ref = None

        # ── gráficos: texto primeiro, OCR só se não houver texto ──────────
        series = graficos_texto(pg1)
        fonte_graf = FONTE_TXT
        if not series and usar_ocr and TEM_OCR:
            series = graficos_ocr(pg1, rotulo)
            fonte_graf = FONTE_OCR
        elif not series and usar_ocr and not TEM_OCR:
            print(f"    {rotulo}: gráficos sem texto e pytesseract ausente")

        # identifica qual gráfico é Hass: o que casa com a referência na
        # semana do relatório. Sem isso teria que confiar em posição na página,
        # que muda entre layouts.
        # Cascata de identificação, sem chute:
        #   1) título ao lado do gráfico (funciona nos layouts de texto)
        #   2) o gráfico cujo valor na semana do relatório bate com a
        #      referência é o Hass (funciona no raster de 2026)
        #   3) se nenhum critério resolve, DESCARTA o gráfico e fica só a
        #      tabela. Preferir uma semana certa a quatro semanas erradas.
        alvo = ref[0] if ref else None
        grades = [g for _, g in series]
        if alvo is not None and "Hass 18" not in grades:
            for i, (s, _) in enumerate(series):
                if sem_rel in s and abs(s[sem_rel] - alvo) < 0.02:
                    grades[i] = "Hass 18"
                    break
        # com exatamente 2 gráficos e um identificado, o outro é o par
        if len(series) == 2 and grades.count(None) == 1:
            conhecido = next(g for g in grades if g)
            par = "Green 18" if conhecido == "Hass 18" else "Hass 18"
            grades[grades.index(None)] = par

        # ── a semana do relatório, conferida contra o gráfico ─────────────
        # O cabeçalho erra: Avocado 18-25.pdf diz 17 e é da 18. A prova está no
        # próprio PDF — a última barra do gráfico do Hass tem o mesmo valor da
        # referência, e a semana dela é a verdadeira. Evidência, não chute.
        idx_hass = grades.index("Hass 18") if "Hass 18" in grades else None
        if ref and idx_hass is not None:
            serie_h = series[idx_hass][0]
            casam = [w for w, v in serie_h.items() if abs(v - ref[0]) < 0.011]
            # O valor pode repetir em duas barras: no Avocado 03-24.pdf o 13,98
            # aparece em w3 E em w52. Pegar "a maior" transformou a semana 3 em
            # 52 e contaminou a S52. Então: candidato mais PRÓXIMO do cabeçalho,
            # e só aceito se a distância for de no máximo 2 semanas — erro de
            # digitação é de 1, coincidência de valor costuma estar a 40+.
            if casam:
                sem_graf = min(casam, key=lambda w: (abs(w - sem_rel), w))
                dist = abs(sem_graf - sem_rel)
                empate = sum(1 for w in casam if abs(w - sem_rel) == dist) > 1
                if sem_graf != sem_rel and dist <= 2 and not empate:
                    print(f"    {rotulo}: cabeçalho diz semana {sem_rel}, mas a "
                          f"referência {ref[0]:.2f} é a barra w{sem_graf} do "
                          f"gráfico — vale a do gráfico")
                    sem_rel = sem_graf
                elif sem_graf != sem_rel and dist > 2:
                    print(f"    {rotulo}: referência {ref[0]:.2f} também aparece "
                          f"em w{sem_graf}, longe do cabeçalho (w{sem_rel}) — "
                          f"coincidência de valor, mantenho o cabeçalho")
        decl = semana_declarada(pg1)
        if decl is not None and decl != sem_rel:
            print(f"    {rotulo}: rótulo da tabela diz semana {decl}, relatório "
                  f"é da {sem_rel} — vale a do relatório")
        if ref:
            precos.append(reg(sem_rel, "Hass 18", ref[0], FONTE_TAB,
                              variacao_eur=ref[1], variacao_media_pct=ref[2]))

        for i, (s, _) in enumerate(series):
            grade = grades[i]
            if grade is None:
                print(f"    {rotulo}: gráfico com {sorted(s)} sem título nem "
                      f"casamento com a referência — descartado, fica a tabela")
                continue
            s = valida_serie(s, "EUR/caixa 4kg", grade)
            if not s:
                continue
            print(f"    {rotulo}: {grade} -> " +
                  " ".join(f"w{k}={s[k]:.2f}" for k in sorted(s)))
            for k, v in s.items():
                if grade == "Hass 18" and k == sem_rel and ref:
                    continue        # a tabela já gravou esta, com variação
                precos.append(reg(k, grade, v, fonte_graf))

        # ── grupos de calibre ─────────────────────────────────────────────
        for grade, v, d, p in grupos_de_calibre(pdf):
            if faixa_ok(v, unidade_do_grade(grade)):
                precos.append(reg(sem_rel, grade, v, FONTE_TAB,
                                  variacao_eur=d, variacao_media_pct=p))
            else:
                print(f"    {rotulo}: {grade} com {v} fora da faixa — descartado")

        # ── grade calibre x origem ────────────────────────────────────────
        tb = grade_origem(pdf)
        if tb:
            variedades = [limpa(c) for c in tb[0]]
            origens = [re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", limpa(c))).strip(" /")
                       for c in tb[1]]
            # Em alguns layouts a linha de origens vem deslocada e o que cai
            # aqui é o rótulo de VARIEDADE do cabeçalho. Na conferência da carga
            # de 12/08/2026 apareceram "origens" chamadas Hass (534 linhas) e
            # Green Varieties (237). Não são origens; a coluna correspondente
            # não dá para atribuir a país nenhum, então fica fora.
            origens = ["" if o.lower() in ORIGEM_NAO_E_PAIS else o for o in origens]
            for row in tb[2:]:
                cels = [limpa(c) for c in row]
                if not cels or not re.match(r"(?:Grade\s*)?\d{2}", cels[0] or "", re.I):
                    continue
                unidade = "EUR/kg" if "/kg" in cels[0].lower() else "EUR/caixa 4kg"
                cal = re.sub(r"^Grade\s*", "",
                             re.sub(r"\(.*?\)", "", cels[0]), flags=re.I).strip()
                for k in range(1, min(len(cels), len(origens))):
                    if not cels[k] or not origens[k]:
                        continue
                    t = cels[k]
                    principal = re.sub(r"\([^)]*\)", " ", t)
                    nums = ([] if principal.count(":") >= 2
                            else [float(x.replace(",", "."))
                                  for x in re.findall(r"\d+[.,]?\d*", principal)])
                    mn = mx = None
                    if len(nums) == 1:
                        mn = mx = nums[0]
                    elif len(nums) == 2:
                        mn, mx = min(nums), max(nums)
                    var = ("Green" if k < len(variedades)
                           and "green" in (variedades[k] or "").lower() else "Hass")
                    calibres.append({
                        "ano": ano_rel, "semana": sem_rel, "variedade": var,
                        "origem": origens[k], "calibre": cal, "preco_min": mn,
                        "preco_max": mx, "unidade": unidade, "texto_original": t,
                        "arquivo": rotulo, "fonte": FONTE_CAL,
                        "extracted_at": agora})
    return precos, calibres


# ── ENTRADA: WEBDAV ───────────────────────────────────────────────────────
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
    r = requests.get(f"{base_url}{requests.utils.quote(nome)}", auth=auth, timeout=300)
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
        # rglob de propósito: o histórico vem organizado em subpastas por ano
        entradas = [(p.name, str(p)) for p in sorted(d.rglob("*.pdf"))]
        print(f"  pasta {d}: {len(entradas)} PDFs (incluindo subpastas)")
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

    print(f"ETL Europa — CIRAD/Tropisens · {len(entradas)} PDF(s) · OCR "
          f"{'ligado' if usar_ocr and TEM_OCR else 'desligado'} · "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)

    precos, calibres, falhas = [], [], []
    for n, (rotulo, fonte) in enumerate(entradas, 1):
        if len(entradas) > 10 and n % 10 == 0:
            print(f"  ... {n}/{len(entradas)}", flush=True)
        try:
            p, c = parse_pdf(fonte, rotulo, usar_ocr)
        except Exception as e:                          # noqa: BLE001
            print(f"    {rotulo}: erro de leitura ({e})")
            falhas.append(rotulo)
            continue
        if not p:
            falhas.append(rotulo)
        precos += p
        calibres += c

    if not precos:
        print("  ERRO: nenhum preço extraído.")
        return 1

    def edicao(r):
        return (r.get("relatorio_ano") or 0, r.get("relatorio_semana") or 0)

    porchave, revisoes = {}, 0
    for r in sorted(precos, key=edicao):
        k = (r["ano"], r["semana"], r["grade"])
        ant = porchave.get(k)
        if ant and abs(ant["preco_eur"] - r["preco_eur"]) > 0.011:
            # preço revisado: o registro novo vale inteiro. A variação antiga
            # se referia ao valor antigo e viraria mentira se fosse mantida.
            revisoes += 1
            if revisoes <= 8:
                print(f"  revisão {k[2]} S{k[1]}/{k[0]}: {ant['preco_eur']:.2f} "
                      f"(rel S{ant['relatorio_semana']}) -> {r['preco_eur']:.2f} "
                      f"(rel S{r['relatorio_semana']})")
        elif ant:
            # MESMO preço em edição mais nova. Só a caixa de referência publica
            # variação e comparativo com a média; o gráfico traz apenas o valor.
            # Como o relatório da semana N+1 repete a semana N no gráfico e é
            # edição mais nova, sem esta fusão ele apagaria a variação que o
            # relatório da semana N havia trazido — e o campo ficava preenchido
            # em 18 de 274 linhas. O percentual vs média das safras anteriores
            # não é recalculável a partir da série, então perdê-lo é perder dado.
            for campo in ("variacao_eur", "variacao_media_pct"):
                if r.get(campo) is None and ant.get(campo) is not None:
                    r[campo] = ant[campo]
        porchave[k] = r
    precos = list(porchave.values())

    porcal = {}
    for c in sorted(calibres, key=lambda x: (x["ano"], x["semana"])):
        porcal[(c["ano"], c["semana"], c["variedade"], c["origem"], c["calibre"])] = c
    calibres = list(porcal.values())

    if revisoes > 8:
        print(f"  ... e mais {revisoes - 8} revisões (fica sempre a edição mais nova)")

    # cobertura por grade e ano, com os buracos explícitos
    print(f"\n  {len(precos)} linhas de preço · {len(calibres)} de calibre x origem")
    if falhas:
        print(f"  {len(falhas)} PDF(s) sem preço: "
              f"{falhas[:6]}{' ...' if len(falhas) > 6 else ''}")
    for grade in sorted({r["grade"] for r in precos}):
        for ano in sorted({r["ano"] for r in precos if r["grade"] == grade}):
            ss = sorted(r["semana"] for r in precos
                        if r["grade"] == grade and r["ano"] == ano)
            faltam = [w for w in range(min(ss), max(ss) + 1) if w not in ss]
            txt = f"    {grade:16s} {ano}: {len(ss):2d} semanas (S{min(ss)}-S{max(ss)})"
            if faltam:
                txt += f" · faltam {faltam}" if len(faltam) <= 12 else \
                       f" · faltam {len(faltam)} semanas"
            print(txt)

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
