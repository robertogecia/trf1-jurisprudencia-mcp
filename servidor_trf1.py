# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]>=1.4.0,<2", "httpx>=0.27", "truststore>=0.9"]
# (mcp 2.x renomeou FastMCP para MCPServer e mudou APIs — 11/09/2026 o pip trouxe 2.x e o
#  registro falhou; manter <2 até migrar junto com o servidor do TJRO)
# ///
"""
Servidor MCP — Jurisprudência do TRF1 (Tribunal Regional Federal da 1ª Região)
==============================================================================
Pesquisa pública no portal de jurisprudência do CJF (https://jurisprudencia.cjf.jus.br/trf1),
SEM login — fontes TRF1 e JEF1 (Turmas Recursais).

Expõe três ferramentas ao Claude:
  • buscar_jurisprudencia_trf1  — pesquisa por tema, com operadores do motor e filtros
  • obter_decisao_trf1          — todos os documentos publicados sob um número de processo,
                                  com ementa e dispositivo INTEGRAIS (o portal não expõe o voto)
  • diagnostico_ritmo_trf1      — por que as buscas podem estar falhando (sem rede)

Backend (engenharia reversa do portal oficial, 11/09/2026 — spec em references/protocolo-cjf.md):
  app Java/JSF + PrimeFaces 6.2, renderizado no servidor, com sessão (3 cookies) e
  javax.faces.ViewState estático por sessão. Busca = POST partial/ajax em /trf1/index.xhtml;
  resposta = XML <partial-response> com o HTML dos resultados dentro de CDATA.
  Motor textual tipo BRS: operadores E / OU / NAO / ADJn / PROXn / COM / MESMO / XOU,
  aspas para frase, `$` como radical, `termo[CAMPO]` para restringir a um campo.

Limite estrutural, dito com todas as letras: o portal entrega EMENTA e DISPOSITIVO (campo
"Decisão"), nunca o voto. O inteiro teor dos casos antigos fica em arquivo.trf1.jus.br,
atrás de desafio Cloudflare (403 já na 1ª requisição — não automatizável e não se tenta
contornar); o dos casos PJe é um link genérico à consulta pública, sem o número do processo.

Gerado para Roberto Grécia Bessa — OAB/RO 7865-A.
"""
from __future__ import annotations

import asyncio
import contextlib
import html as _html
import json
import os
import re
import sys
import time
import unicodedata
from typing import Any

try:
    import fcntl  # POSIX (mac/Linux); ausente no Windows
except Exception:
    fcntl = None

# Usa o trust store do sistema operacional (macOS Keychain), assim como o curl.
# Resolve o erro "self-signed certificate" quando há proxy TLS na rede.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # truststore é opcional; segue sem ele
    pass

try:
    import httpx
except Exception:  # permite importar o módulo para testes sem httpx instalado
    httpx = None  # type: ignore

# --------------------------------------------------------------------------- #
# Constantes do portal                                                         #
# --------------------------------------------------------------------------- #
SITE = "https://jurisprudencia.cjf.jus.br"
CAMINHO = "/trf1/index.xhtml"
ENDPOINT = SITE + CAMINHO
# Três bases no mesmo portal (mapeadas ao vivo em 11/09/2026, fixtures 08-12):
#  trf1      — acórdãos/súmulas/arguições/monocráticas do TRF1 + JEF1; painel avançado; voto NÃO exposto.
#  tnu       — Turma Nacional de Uniformização (PUIL etc.); tipos ACORDAO/DECISAOMONO/DECISAOPRES;
#              filtro de precedente qualificado (Representativo de Controvérsia / Precedente Relevante);
#              inteiro teor em HTML no eproc da TNU, baixável sem desafio → "inteiro teor lido" possível.
#  colegiado — decisões ADMINISTRATIVAS do Conselho (procedimentos normativos, inspeções, PCA); sem
#              tipos nem fonte; inteiro teor embutido no próprio resultado. Raramente serve a litígio.
BASES: dict[str, dict[str, Any]] = {
    "trf1": {"caminho": "/trf1/index.xhtml", "rotulo": "TRF1", "tribunal": "TRF-1",
             "tipos": ["ACORDAO", "SUMULA", "ARGUICAO", "DECISAOMONO"], "fonte": True,
             "tipo_acordao": False, "avancada": True},
    "tnu": {"caminho": "/tnu/index.xhtml", "rotulo": "TNU", "tribunal": "TNU",
            "tipos": ["ACORDAO", "DECISAOMONO", "DECISAOPRES"], "fonte": False,
            "tipo_acordao": True, "avancada": False},
    "colegiado": {"caminho": "/colegiado/index.xhtml", "rotulo": "Colegiado CJF", "tribunal": "CJF",
                  "tipos": [], "fonte": False, "tipo_acordao": False, "avancada": False},
}
TIPOS_ACORDAO_TNU = {"REPRESENTATIVO": "Representativos de Controvérsia", "RELEVANTE": "Precedentes Relevantes"}
URL_PJE_CONSULTA_PUBLICA = "https://pje2g.trf1.jus.br/consultapublica/ConsultaPublica/listView.seam"

## DECISÃO PESSOAL, NÃO REPLICAR EM PACOTE DISTRIBUÍDO ##
# Mesmo critério do servidor do TJRO: User-Agent de navegador real. O portal do CJF
# não mostrou filtro de UA (8 requisições de mapeamento em 11/09/2026 responderam
# normalmente), mas o arquivo.trf1.jus.br está atrás de Cloudflare — e este script
# NÃO tenta passar por ele. O UA aqui é só para não se destacar como robô num
# portal que hoje aceita o acesso; não contorna autenticação nem acessa nada que
# não seja público.
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
HEADERS_AJAX = {
    "Faces-Request": "partial/ajax",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": ENDPOINT,
    "Accept": "application/xml, text/xml, */*; q=0.01",
}

TIPOS_VALIDOS = ["ACORDAO", "SUMULA", "ARGUICAO", "DECISAOMONO", "DECISAOPRES"]
TIPOS_ROTULO = {
    "ACORDAO": "acórdãos", "SUMULA": "súmulas", "ARGUICAO": "arguições",
    "DECISAOMONO": "decisões monocráticas", "DECISAOPRES": "decisões da presidência",
}
FONTES_VALIDAS = ["TRF1", "JEF1"]
TIPOS_DATA = {"julgamento": "DTDP", "publicacao": "DTPP"}
POR_PAGINA_VALIDOS = (10, 30, 50)

# Campos do painel "Pesquisa avançada", na ORDEM em que os <input> aparecem no painel
# (confirmada no fixture 05_ckbavancada.xml). Os `name` (j_idtNN) são gerados pelo JSF
# e podem mudar num redeploy: o mapeamento real é lido da resposta do toggle a cada
# consulta, por posição, com os <label for=...> estáveis como conferência; esta tabela
# é só o fallback.
CAMPOS_AVANCADOS_ORDEM = [
    "numero", "classe", "relator", "revisor", "relator_convocado",
    "relator_para_acordao", "orgao_julgador", "origem", "ementa_decisao",
    "referencia_legislativa", "data_inicio", "data_fim",
]
CAMPOS_AVANCADOS_FALLBACK = {
    "numero": "formulario:j_idt28",
    "classe": "formulario:j_idt30",
    "relator": "formulario:j_idt32",
    "revisor": "formulario:j_idt34",
    "relator_convocado": "formulario:j_idt36",
    "relator_para_acordao": "formulario:j_idt38",
    "orgao_julgador": "formulario:j_idt40",
    "origem": "formulario:j_idt42",
    "ementa_decisao": "formulario:j_idt44",
    "referencia_legislativa": "formulario:j_idt46",
    "data_inicio": "formulario:j_idt48_input",
    "data_fim": "formulario:j_idt50_input",
}
# <label for="..."> estáveis → chave (10 dos 12 campos têm label — só as duas datas não; servem
# de conferência do mapeamento por posição)
LABELS_AVANCADOS = {
    "proc": "numero", "combo_classes": "classe", "rel": "relator", "rev": "revisor",
    "relc": "relator_convocado", "rela": "relator_para_acordao",
    "combo_orgaos": "orgao_julgador", "_origem": "origem",
    "emen": "ementa_decisao", "refl": "referencia_legislativa",
}
CAMPO_FONTE = "formulario:j_idt62"  # checkbox TRF1/JEF1 (name gerado; confirmado no index)

# Palavras que o motor trata como operador quando aparecem soltas.
_RESERVADAS = {"E", "OU", "NAO", "NÃO", "ADJ", "PROX", "COM", "MESMO", "XOU"}
_RE_RESERVADA_N = re.compile(r"^(ADJ|PROX)\d{0,2}$")

GRUPOS_MAX = 6
TERMOS_POR_GRUPO_MAX = 12
TERMO_MAX_CHARS = 80

# Orçamento máximo de caracteres da resposta de obter_decisao (evita afogar o contexto).
ORCAMENTO_DECISAO = 50_000
TRECHO_EMENTA = 800     # busca: ementa truncada
TRECHO_DECISAO = 320    # busca: dispositivo truncado

# --------------------------------------------------------------------------- #
# Funções puras (sem rede) — fáceis de testar                                  #
# --------------------------------------------------------------------------- #
def _fold(t: str) -> str:
    """Minúsculas sem acento: comparação sem caixa nem acento."""
    return "".join(
        c for c in unicodedata.normalize("NFD", (t or "").lower())
        if not unicodedata.category(c).startswith("M")
    )


def _cnj(nr: str) -> str:
    """Formata 20 dígitos no padrão CNJ NNNNNNN-DD.AAAA.J.TR.OOOO."""
    d = re.sub(r"\D", "", nr or "")
    if len(d) == 20:
        return f"{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}"
    return nr or ""


def _so_digitos(nr: str) -> str:
    return re.sub(r"\D", "", nr or "")


def _limpar(texto: str, limite: int = 0) -> str:
    """HTML → texto: destaque do portal (<font color="blue"><b>X</b></font>) vira «X»,
    <br> vira espaço, tags somem, entidades são decodificadas; opcionalmente trunca."""
    if not texto:
        return ""
    t = re.sub(r'(?is)<font[^>]*>\s*<b>(.*?)</b>\s*</font>', r"«\1»", texto)
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if limite and len(t) > limite:
        t = t[:limite].rsplit(" ", 1)[0] + "…"
    return t


def _sem_destaque(t: str) -> str:
    """Remove os marcadores «» de destaque (para campos de metadado)."""
    return (t or "").replace("«", "").replace("»", "")


def _data_iso(br: str) -> str:
    """DD/MM/AAAA → AAAA-MM-DD ('' se não casar)."""
    m = re.match(r"^\s*(\d{2})/(\d{2})/(\d{4})", br or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _normalizar_data(data: str, rotulo: str) -> str:
    """Aceita DD/MM/AAAA ou AAAA-MM-DD e devolve DD/MM/AAAA (formato do calendário do portal)."""
    d = (data or "").strip()
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", d):
        return d
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    raise ValueError(f"{rotulo} inválida: {data!r} — use AAAA-MM-DD ou DD/MM/AAAA.")


# --- Montagem da consulta na sintaxe do motor ------------------------------- #
# O motor do CJF é textual (tipo BRS): "não usar pontuação" (Ajuda), operadores por
# extenso, `$` como radical no fim da palavra, aspas para frase exata. Os `grupos`
# vêm do mesmo raciocínio do TJRO (julgados do mesmo assunto usam vocabulários
# diferentes): cada grupo é um OR entre parênteses, os grupos se somam por E.
# Nenhuma sintaxe crua vinda do modelo entra pelos grupos — cada termo é normalizado.
# Caracteres que o portal RECUSA com erro ("Os seguintes textos são inválidos para pesquisa:
# # ! + ' ; _ | - @" — mensagem real, 11/09/2026, quando "auxílio-doença" foi enviado). Viram
# espaço em qualquer lugar da consulta; hífen dentro de palavra vira frase ("auxílio doença").
_INVALIDOS_PORTAL = "#!+';_|-@"
_TRAD_INVALIDOS = str.maketrans({c: " " for c in _INVALIDOS_PORTAL})
_RE_PONTUACAO = re.compile(r"[^\w\s$?«»]", re.UNICODE)  # aspas internas e hífen também caem
# `termo[CAMPO]` / `termo[-CAMPO]` no FIM do termo de um grupo é preservado (é a sintaxe de
# campo do portal — EMEN, DECI, REL, ORGA...). Colchete em qualquer outro lugar é pontuação.
_RE_CAMPO_SUFIXO = re.compile(r"\[(-?[A-Za-z]{2,4})\]\s*$")


def _termo_para_query(termo: str) -> str:
    t = re.sub(r"\s+", " ", (termo or "")).strip()[:TERMO_MAX_CHARS]
    m_campo = _RE_CAMPO_SUFIXO.search(t)
    campo = m_campo.group(1).upper() if m_campo else ""
    if m_campo:
        t = t[:m_campo.start()]
    t = t.translate(_TRAD_INVALIDOS)
    t = _RE_PONTUACAO.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # radical: `$` só no FIM de palavra (no início ou no meio seria varredura cara)
    t = re.sub(r"\$(?=\S)", "", t)
    sufixo = f"[{campo}]" if campo else ""
    if " " in t:
        return f'"{t}"{sufixo}'
    up = t.upper()
    if up in _RESERVADAS or _RE_RESERVADA_N.match(up):
        return f'"{t}"{sufixo}'
    return t + sufixo


def _montar_grupos(grupos: list[list[str]] | None) -> str:
    if not grupos or not isinstance(grupos, list):
        return ""
    partes: list[str] = []
    for g in grupos[:GRUPOS_MAX]:
        if not isinstance(g, list):
            continue
        vistos: list[str] = []
        for termo in g[:TERMOS_POR_GRUPO_MAX]:
            q = _termo_para_query(str(termo))
            if q and q not in vistos:
                vistos.append(q)
        if not vistos:
            continue
        partes.append(vistos[0] if len(vistos) == 1 else "(" + " OU ".join(vistos) + ")")
    return " E ".join(partes)


def _montar_consulta(consulta: str, grupos: list[list[str]] | None) -> str:
    """Consulta livre (sintaxe do portal, passada como está) + grupos montados aqui."""
    # a consulta livre passa como está (é a sintaxe do portal), só sem os caracteres que o
    # portal recusa com erro e o `_` (que \w deixaria passar)
    livre = re.sub(r"\s+", " ", (consulta or "").translate(_TRAD_INVALIDOS)).strip()
    g = _montar_grupos(grupos)
    if livre and g:
        return f"({livre}) E {g}"
    return livre or g


# --- Extração do XML/HTML ---------------------------------------------------- #
_RE_UPDATE = r'<update id="{id}"><!\[CDATA\[(.*?)\]\]></update>'


def _extrair_update(xml: str, id_componente: str) -> str:
    m = re.search(_RE_UPDATE.format(id=re.escape(id_componente)), xml or "", re.S)
    # O JSF quebra o CDATA em "]]]]><![CDATA[>" quando o conteúdo contém "]]>": recompõe.
    return m.group(1).replace("]]]]><![CDATA[>", "]]>") if m else ""


def _extrair_viewstate(texto: str) -> str:
    m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', texto or "")
    if m:
        return m.group(1)
    m = re.search(r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[([^\]]+)\]\]>', texto or "")
    return m.group(1) if m else ""


def _extrair_mensagens(xml: str) -> list[str]:
    """Erros de validação do JSF vêm em <update id="j_idt16:messages">."""
    bloco = _extrair_update(xml, "j_idt16:messages") or xml or ""
    msgs = re.findall(r'ui-messages-error-detail">(.*?)</span>', bloco, re.S)
    if not msgs:
        msgs = re.findall(r'ui-messages-error-summary">(.*?)</span>', bloco, re.S)
    return [_limpar(m) for m in msgs if _limpar(m)]


def _extrair_total(html: str) -> int:
    """Total REAL da consulta: rowCount do widget DataGrid (mais confiável); fallback
    nos contadores "N Documento(s) encontrado(s)" e na linha "Exibindo"."""
    m = re.search(r"rowCount:(\d+)", html or "")
    if m:
        return int(m.group(1))
    contadores = re.findall(r"(\d+) Documento\(s\) encontrado", html or "")
    if contadores:
        return sum(int(c) for c in contadores)
    m = re.search(r"Exibindo [\d\s\-]+ de\s+(\d+)", html or "")
    return int(m.group(1)) if m else 0


_RE_DOC_SPLIT = re.compile(r'<table class="table_pesquisa_lista" id="doc_([^"]+)"')  # TNU: doc_TNU00033758
# Cada par rótulo→valor vive num <div class="ui-outputpanel">; ancorar no </div> que fecha o
# par tolera <td> com atributo e tabela dentro da ementa (red team 11/09/2026: com `<td class>`
# o parser antigo emitia o documento com TODOS os metadados vazios, sem aviso).
_RE_CAMPO = re.compile(
    r'<span class="label_pontilhada">\s*(.*?)\s*</span>\s*</td>\s*</tr>\s*'
    r'<tr[^>]*>\s*<td\b[^>]*>(.*?)</td>\s*</tr>\s*</div>',
    re.S,
)
_RE_SIGLA_CLASSE = re.compile(r"\(([A-ZÇ]{2,10})\)\s*$")


def _parsear_documentos(html: str, base: str = "trf1") -> list[dict]:
    """Cada resultado é <table class="table_pesquisa_lista" id="doc_N">; dentro, pares
    rótulo (label_pontilhada) → valor. Iteramos pelos pares, não por lista fixa: a
    presença de campos varia (Relator convocado, Relator para acórdão, Decisão...)."""
    partes = _RE_DOC_SPLIT.split(html or "")
    docs: list[dict] = []
    vistos: set[str] = set()
    for i in range(1, len(partes) - 1, 2):
        doc_id, bloco = partes[i], partes[i + 1]
        if doc_id in vistos:
            continue
        vistos.add(doc_id)
        campos: dict[str, str] = {}
        for rotulo, valor in _RE_CAMPO.findall(bloco):
            r = _limpar(rotulo)
            if r and r not in campos:
                campos[r] = valor
        d: dict[str, Any] = {"id": doc_id, "base": base, "campos": {}}
        # O portal destaca o termo pesquisado (e o do filtro) em TODOS os campos; o marcador
        # «» só interessa na ementa e no dispositivo — em relator/data/órgão ele contaminaria
        # a citação (visto ao vivo em 11/09/2026: "Relator: DESEMBARGADOR «NEY BELLO»").
        for r, v in campos.items():
            if r in ("Número", "Inteiro teor", "Fonte da publicação"):
                d["campos"][r] = v
            elif r in ("Ementa", "Decisão"):
                d["campos"][r] = _limpar(v)
            else:
                d["campos"][r] = _sem_destaque(_limpar(v))
        num_raw = campos.get("Número", "")
        formas = [_limpar(x) for x in re.split(r"(?i)<br\s*/?>", num_raw)]
        formas = [f for f in formas if f]
        d["numero"] = formas[0] if formas else ""
        d["numero_digitos"] = next((_so_digitos(f) for f in formas if len(_so_digitos(f)) == 20), _so_digitos(d["numero"]))
        meta = lambda r: _sem_destaque(_limpar(campos.get(r, "")))  # noqa: E731
        # TNU: "Acórdão<br/>Precedente Relevante" — a 2ª linha é a QUALIFICAÇÃO do precedente
        partes_tipo = [_sem_destaque(_limpar(x)) for x in re.split(r"(?i)<br\s*/?>", campos.get("Tipo", ""))]
        partes_tipo = [x for x in partes_tipo if x]
        d["tipo"] = partes_tipo[0] if partes_tipo else ""
        d["qualificacao"] = " / ".join(partes_tipo[1:])
        if "Tipo" in d["campos"]:
            d["campos"]["Tipo"] = d["tipo"]
        if d["qualificacao"]:
            d["campos"]["Qualificação do precedente"] = d["qualificacao"]
        d["classe"] = meta("Classe")
        m = _RE_SIGLA_CLASSE.search(d["classe"])
        d["sigla"] = m.group(1) if m else ""
        d["relator"] = meta("Relator(a)")
        d["relator_convocado"] = meta("Relator convocado")
        d["relator_para_acordao"] = meta("Relator(a) para acórdão") or meta("Relator para acórdão")
        d["origem"] = meta("Origem")
        d["orgao"] = meta("Órgão julgador")
        d["data_julgamento"] = meta("Data")
        d["data_publicacao"] = meta("Data da publicação")
        fontes = [_sem_destaque(_limpar(x)) for x in re.split(r"(?i)<br\s*/?>", campos.get("Fonte da publicação", ""))]
        fontes_unicas: list[str] = []
        for f in fontes:
            if f and f not in fontes_unicas:
                fontes_unicas.append(f)
        d["fonte_publicacao"] = "; ".join(fontes_unicas)
        d["ementa"] = _limpar(campos.get("Ementa", ""))
        d["decisao"] = _limpar(campos.get("Decisão", ""))
        m = re.search(r'href="([^"]+)"', campos.get("Inteiro teor", ""))
        link = _html.unescape(m.group(1)) if m else ""
        d["link_inteiro_teor"] = link
        d["link_tipo"] = (
            "arquivo" if "arquivo.trf1" in link else "tnu" if "eproctnu" in link
            else "pje" if "pje" in link else "" if not link else "outro"
        )
        # colegiado: o inteiro teor vem EMBUTIDO como documento HTML dentro do campo
        bruto_it = campos.get("Inteiro teor", "")
        d["inteiro_teor_embutido"] = _texto_documento(bruto_it) if "<body" in bruto_it.lower() else ""
        docs.append(d)
    return docs


def _texto_documento(html_doc: str) -> str:
    """Texto de um documento HTML inteiro (inteiro teor da TNU no eproc, ou o embutido do
    colegiado): quebras de bloco viram '\n', tags somem, entidades decodificadas."""
    if not html_doc:
        return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_doc)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>|</li>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t\r\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def _mapear_campos_avancados(painel: str) -> tuple[dict[str, str], list[str]]:
    """Lê os `name` dos inputs do painel avançado por POSIÇÃO (12 campos, ordem fixa) e
    confere com os <label for=...> estáveis. Devolve (mapa, avisos)."""
    avisos: list[str] = []
    nomes = re.findall(r'<input[^>]*name="(formulario:j_idt\d+(?:_input)?)"', painel or "")
    mapa: dict[str, str] = {}
    if len(nomes) == len(CAMPOS_AVANCADOS_ORDEM):
        mapa = dict(zip(CAMPOS_AVANCADOS_ORDEM, nomes))
    else:
        avisos.append(
            f"painel avançado com {len(nomes)} campos (esperados {len(CAMPOS_AVANCADOS_ORDEM)}) — "
            "usando nomes de fallback; se o filtro for ignorado, o portal mudou de layout"
        )
        mapa = dict(CAMPOS_AVANCADOS_FALLBACK)
    # conferência pelos labels: o input logo após <label for="rel"> deve ser o de relator
    for m in re.finditer(r'<label for="([^"]+)"', painel or ""):
        chave = LABELS_AVANCADOS.get(m.group(1))
        if not chave:
            continue
        m2 = re.search(r'<input[^>]*name="(formulario:j_idt\d+(?:_input)?)"', painel[m.end():])
        if m2 and mapa.get(chave) != m2.group(1):
            avisos.append(f"campo '{chave}': posição diz {mapa.get(chave)}, label diz {m2.group(1)} — prevalece o label")
            mapa[chave] = m2.group(1)
    return mapa, avisos


# --- Resultado do julgamento (jurimetria dirigida, offline) ------------------ #
# Mesma arquitetura do TJRO (pares independentes, ambíguo não decide), aplicada ao campo
# "Decisão" — que no CJF é o dispositivo puro, sem o corpo do acórdão em volta. Rótulos
# reaproveitados com a calibração do red team de 04/09/2026; novo aqui: PREJUDICADO.
_OPOSTOS: list[tuple[str, str]] = [
    ("PROVIDO", "DESPROVIDO"),
    ("ACOLHIDO", "REJEITADO"),
]
_RE_NAO_CONHECIDO = re.compile(r"\bNAO (SE )?CONHEC", re.ASCII)
_RE_CONHECIDO = re.compile(r"(?<!NAO )(?<!NAO SE )\bCONHEC", re.ASCII)
# "-LHE/-LHES" e "INTEGRAL" vieram dos 60 dispositivos reais dos fixtures (red team 11/09/2026).
_RE_DESPROVIDO = re.compile(
    r"\b(DESPROVI|IMPROVI|NAO PROVI|NEG\w*(-(SE|LHE|LHES)| SE)? (INTEGRAL |PARCIAL )?PROVIMENTO"
    r"|PROVIMENTO NEGADO)", re.ASCII
)
_RE_PROVIDO = re.compile(
    r"(?<!NAO )\b(PROVI(DO|DOS|DA|DAS)\b"
    r"|D(A|AO|AR|OU|ERAM|EU)(-(SE|LHE|LHES)| SE)? (INTEGRAL |PARCIAL )?PROVIMENTO)", re.ASCII
)
_RE_REJEITADO = re.compile(r"\bREJEIT", re.ASCII)
_RE_ACOLHIDO = re.compile(r"\bACOLH", re.ASCII)
_RE_PREJUDICADO = re.compile(r"\bPREJUDICAD", re.ASCII)


def _resultado_de(texto: str) -> set[str]:
    t = _fold(texto).upper()
    cauda = t[-2500:] if len(t) > 2500 else t
    r: set[str] = set()
    if _RE_NAO_CONHECIDO.search(cauda):
        r.add("NÃO CONHECIDO")
    if _RE_CONHECIDO.search(cauda):
        r.add("CONHECIDO")
    if _RE_DESPROVIDO.search(cauda):
        r.add("DESPROVIDO")
    if _RE_PROVIDO.search(cauda):
        r.add("PROVIDO")
    if _RE_REJEITADO.search(cauda):
        r.add("REJEITADO")
    if _RE_ACOLHIDO.search(cauda):
        r.add("ACOLHIDO")
    if _RE_PREJUDICADO.search(cauda):
        r.add("PREJUDICADO")
    return r


def _lado_de(conjunto: set[str], par: tuple[str, str]) -> str | None:
    a, b = par
    if a in conjunto and b in conjunto:
        return None
    return a if a in conjunto else b if b in conjunto else None


_ROTULOS_RESULTADO = {"PROVIDO": "provido", "DESPROVIDO": "desprovido", "ACOLHIDO": "acolhido",
                      "REJEITADO": "rejeitado", "PREJUDICADO": "prejudicado", "NÃO CONHECIDO": "não conhecido"}


def _resumo_resultados_pagina(docs: list[dict]) -> dict:
    """Contagem OFFLINE de quantos JULGAMENTOS (nº + data) declaram cada resultado no
    dispositivo. Indício para escolher o que ler, nunca conclusão sobre a tese."""
    por_julgamento: dict[str, set[str]] = {}
    for i, d in enumerate(docs):
        chave = f"{d['numero_digitos']}|{d['data_julgamento']}" if d["numero_digitos"] and d["data_julgamento"] else f"#{i}"
        por_julgamento.setdefault(chave, set()).update(_resultado_de(d.get("decisao") or ""))
    contagem = {k: 0 for k in _ROTULOS_RESULTADO}
    sem = 0
    for conjunto in por_julgamento.values():
        lados = [l for l in (_lado_de(conjunto, par) for par in _OPOSTOS) if l]
        if len(lados) == 1:
            contagem[lados[0]] += 1
        elif not lados and "PREJUDICADO" in conjunto:
            contagem["PREJUDICADO"] += 1
        elif not lados and "NÃO CONHECIDO" in conjunto:
            contagem["NÃO CONHECIDO"] += 1
        else:
            sem += 1
    return {"contagem": contagem, "sem_resultado": sem, "total_julgamentos": len(por_julgamento)}


# --- Citação e formatação ---------------------------------------------------- #
def _citacao(d: dict) -> str:
    """Citação pronta para peça, no padrão forense. Segmentos ausentes são omitidos.
    Ex.: (TRF-1 - AC: 0020777-05.2018.4.01.3300, Relator: DESEMBARGADOR FEDERAL X,
    Data de Julgamento: 02/06/2026, DÉCIMA-PRIMEIRA TURMA, Data de Publicação: 02/06/2026)"""
    # Prefixo é sempre TRF-1 (o portal é o do TRF1/JEF1); origem que não seja "TRF - PRIMEIRA
    # REGIÃO" (seção judiciária, turma recursal) vira segmento próprio, não nome de tribunal.
    rotulo = d.get("sigla") or d.get("classe") or d.get("tipo") or "Julgado"
    tribunal = BASES.get(d.get("base") or "trf1", BASES["trf1"])["tribunal"]
    partes = [f"{tribunal} - {rotulo}: {d.get('numero') or _cnj(d.get('numero_digitos', ''))}"]
    if d.get("qualificacao"):
        partes.append(d["qualificacao"])
    origem = _fold(d.get("origem") or "").upper()
    if origem and "PRIMEIRA REGI" not in origem and origem not in ("TNU", "SEI!JULGAR DO CJF"):
        partes.append(f"Origem: {d['origem']}")
    if d.get("relator"):
        partes.append(f"Relator: {d['relator']}")
    if d.get("relator_convocado"):
        partes.append(f"Relator convocado: {d['relator_convocado']}")
    if d.get("relator_para_acordao"):
        partes.append(f"Relator para acórdão: {d['relator_para_acordao']}")
    if d.get("data_julgamento"):
        partes.append(f"Data de Julgamento: {d['data_julgamento']}")
    if d.get("orgao"):
        partes.append(d["orgao"])
    if d.get("data_publicacao"):
        partes.append(f"Data de Publicação: {d['data_publicacao']}")
    return "(" + ", ".join(partes) + ")"


def _link_documento_especifico(d: dict) -> str:
    """Link do inteiro teor só quando é ESPECÍFICO deste documento — nunca o link
    genérico de busca do PJe (mesmo href em todo resultado PJe, não aponta pro
    julgado certo). arquivo.trf1.jus.br (p1=CNJ) e o eproc da TNU (id_jurisprudencia)
    são por documento; "pje" é o único tipo genérico conhecido."""
    link = d.get("link_inteiro_teor") or ""
    return link if link and d.get("link_tipo") not in ("", "pje") else ""


def _citacao_com_link(d: dict) -> str:
    """Citação pronta para peça, com a REFERÊNCIA INTEIRA (parênteses incluídos)
    em hiperlink markdown para onde o inteiro teor foi encontrado — mesma
    convenção da skill peticao-rg desde 14/09/2026 ("a referência inteira entre
    parênteses vira link clicável"), aplicada aqui na origem, com o link que a
    própria busca já encontrou. Nunca fabrica link: sem link específico deste
    documento (arquivo.trf1/eproc da TNU), devolve a citação em texto plano."""
    texto = _citacao(d)
    link = _link_documento_especifico(d)
    return f"[{texto}]({link})" if link else texto


def _nota_inteiro_teor(d: dict) -> str:
    if d.get("link_tipo") == "tnu":
        return (f"Inteiro teor: disponível em texto — obter_decisao_trf1(numero, base=\"tnu\") traz o "
                f"acórdão integral (eproc da TNU: {d['link_inteiro_teor']}).")
    if d.get("inteiro_teor_embutido"):
        return "Inteiro teor: embutido no resultado — obter_decisao_trf1(numero, base=\"colegiado\") traz o texto."
    if d.get("link_tipo") == "arquivo":
        return (
            f"Inteiro teor: {d['link_inteiro_teor']} — abrir NO NAVEGADOR (o arquivo.trf1.jus.br "
            "exige desafio Cloudflare; esta ferramenta não o lê)."
        )
    if d.get("link_tipo") == "pje":
        return (
            "Inteiro teor: processo do PJe — o portal só dá o link genérico da consulta pública "
            f"({URL_PJE_CONSULTA_PUBLICA}); pesquise lá pelo número {d.get('numero')} no navegador."
        )
    return "Inteiro teor: link não informado pelo portal."


def _format_busca(docs: list[dict], total: int, meta: dict) -> str:
    consulta = meta.get("consulta_montada", "")
    tipos = meta.get("tipos") or []
    fontes = meta.get("fontes") or []
    pagina = meta.get("pagina", 1)
    por_pagina = meta.get("por_pagina", 30)
    filtros = meta.get("filtros") or {}
    linhas: list[str] = []
    base = meta.get("base") or "trf1"
    cab = f"**{total} documento(s)** no portal do CJF/{BASES[base]['rotulo']} para `{consulta or '(só filtros)'}`"
    if tipos:
        cab += f" · tipo: {', '.join(TIPOS_ROTULO.get(t, t) for t in tipos)}"
    if fontes:
        cab += f" · fonte: {'+'.join(fontes)}"
    if meta.get("tipo_acordao"):
        cab += " · precedentes: " + ", ".join(TIPOS_ACORDAO_TNU.get(t, t) for t in meta["tipo_acordao"])
    if filtros:
        cab += " · filtros: " + "; ".join(f"{k}={v}" for k, v in filtros.items())
    total_paginas = max(1, -(-total // por_pagina)) if total else 1
    cab += f" · página {pagina}/{total_paginas} ({por_pagina} por página)"
    linhas.append(cab)
    for a in meta.get("avisos") or []:
        linhas.append(f"⚠️ {a}")

    if total > 3000 and pagina == 1:
        linhas.append(
            "Dica: total alto — restrinja com `E` (ex.: `\"dano moral\" E negativação`), com "
            "`grupos`, com `[EMEN]` para buscar só na ementa, ou com filtro de órgão/relator/data."
        )
    if not docs:
        linhas.append(
            "\nNenhum resultado nesta página. Se o total é 0: a busca casa PALAVRAS — tente "
            "sinônimos com `OU` ou `grupos`, radical com `$` (`desapropria$`), a súmula/tema que "
            "os julgados do assunto citam, e confira se não há preposição/pontuação na consulta "
            "(o motor não aceita). Se o total é > 0, a página pedida está além do fim."
        )
        return "\n".join(linhas)

    # "Um número, vários julgados": sob o mesmo número convivem acórdão, embargos, decisão
    # monocrática. Avisa e, se os dispositivos do MESMO julgamento se contradizem, aponta.
    por_numero: dict[str, list[dict]] = {}
    for d in docs:
        if d["numero_digitos"]:
            por_numero.setdefault(d["numero_digitos"], []).append(d)

    houve_corte = False
    for i, d in enumerate(docs, start=(pagina - 1) * por_pagina + 1):
        titulo = " ".join(x for x in (d.get("tipo"), d.get("classe")) if x) or "Documento"
        if d.get("qualificacao"):
            titulo += f" ★ {d['qualificacao']}"
        linhas.append(f"\n**{i}. {titulo} — {d.get('numero') or '(sem número)'}**  · Id. do documento: {d['id']}")
        meta_l = []
        if d.get("relator"):
            meta_l.append(f"Relator(a): {d['relator']}")
        if d.get("relator_convocado"):
            meta_l.append(f"Relator convocado: {d['relator_convocado']}")
        if d.get("relator_para_acordao"):
            meta_l.append(f"Relator p/ acórdão: {d['relator_para_acordao']}")
        if d.get("orgao"):
            meta_l.append(f"Órgão: {d['orgao']}")
        if d.get("origem") and "PRIMEIRA REGI" not in d["origem"].upper():
            meta_l.append(f"Origem: {d['origem']}")
        if d.get("data_julgamento"):
            meta_l.append(f"Julgamento: {d['data_julgamento']}")
        if d.get("data_publicacao"):
            meta_l.append(f"Publicação: {d['data_publicacao']}")
        if d.get("fonte_publicacao"):
            meta_l.append(f"Fonte: {d['fonte_publicacao']}")
        linhas.append("  " + " · ".join(meta_l))
        linhas.append(f"  Citação: {_citacao_com_link(d)}")
        em = d.get("ementa") or ""
        if len(em) > TRECHO_EMENTA:
            houve_corte = True
        linhas.append(f"  Ementa (trecho): {_limpar(em, TRECHO_EMENTA) or '—'}")
        if d.get("decisao"):
            linhas.append(f"  Dispositivo: {_limpar(d['decisao'], TRECHO_DECISAO)}")
        irmaos = por_numero.get(d["numero_digitos"], [])
        if len(irmaos) > 1 and irmaos[0] is d:
            desc = "; ".join(
                f"{x.get('data_julgamento') or '?'} {x.get('tipo') or ''} (id {x['id']}, Rel. {x.get('relator') or '?'})"
                for x in irmaos
            )
            linhas.append(f"  ⚠️ Mesmo número, {len(irmaos)} documentos nesta página: {desc} — cite pelo id + data, nunca só pelo número.")
            for par in _OPOSTOS:
                por_data: dict[str, set[str]] = {}
                for x in irmaos:
                    l = _lado_de(_resultado_de(x.get("decisao") or ""), par)
                    if l:
                        por_data.setdefault(x["data_julgamento"], set()).add(l)
                for data, ls in por_data.items():
                    if len(ls) > 1:
                        linhas.append(f"  ⚠️ Documentos do julgamento de {data} declaram resultados opostos ({' × '.join(sorted(ls))}) — provável voto vencido ou decisão monocrática indexada; leia cada um.")
        linhas.append(f"  {_nota_inteiro_teor(d)}")

    if houve_corte:
        linhas.append(
            f"\nℹ️ Ementas cortadas em {TRECHO_EMENTA} caracteres — ementa numerada aplica a tese nos "
            "ÚLTIMOS itens; use obter_decisao_trf1 para a ementa e o dispositivo INTEGRAIS antes de citar."
        )
    if len(docs) >= 3:
        r = _resumo_resultados_pagina(docs)
        c = r["contagem"]
        partes = [f"{c[k]} {v}" for k, v in _ROTULOS_RESULTADO.items() if c[k]]
        if partes:
            sem = f"; {r['sem_resultado']} sem resultado identificável" if r["sem_resultado"] else ""
            linhas.append(
                f"\nResumo desta página (offline, pelo dispositivo, 1× por julgamento): {', '.join(partes)}{sem} "
                f"— em {r['total_julgamentos']} julgamento(s). Indício para escolher o que ler; recurso "
                "provido por outro fundamento também conta como provido."
            )
    if total > pagina * por_pagina:
        linhas.append(f"\nPróxima página: pagina={pagina + 1} (mesmos parâmetros).")
    return "\n".join(linhas)


def _format_decisao(docs: list[dict], numero: str, total: int) -> str:
    if not docs:
        return (
            f"Nenhum documento publicado no portal do CJF/TRF1 sob o número {numero}. Confira o "
            "número (com ou sem pontuação); decisões muito recentes ou de 1º grau não estão na base."
        )
    linhas = [f"**{len(docs)} documento(s) publicado(s) sob o número {docs[0].get('numero') or numero}**"
              + (f" (o portal informa {total}; mostrando os primeiros)" if total > len(docs) else "")]
    if len(docs) > 1:
        linhas.append("Julgamentos distintos sob o mesmo número — a ficha se identifica por número + data + id:")
        for d in docs:
            linhas.append(f"- {d.get('data_julgamento') or '?'} · {d.get('tipo') or '?'} · id {d['id']} · Rel. {d.get('relator') or '?'} · {d.get('orgao') or ''}")
    orcamento_por_doc = max(8_000, ORCAMENTO_DECISAO // max(1, len(docs)))
    for d in docs:
        linhas.append(f"\n### {d.get('tipo') or 'Documento'} · id {d['id']} · julgado em {d.get('data_julgamento') or '?'}")
        linhas.append(f"Citação: {_citacao_com_link(d)}")
        campos = d.get("campos") or {}
        extras = {k: v for k, v in campos.items() if k not in ("Ementa", "Decisão", "Inteiro teor", "Número", "Fonte da publicação")}
        linhas.append("  " + " · ".join(f"{k}: {v}" for k, v in extras.items()))
        if d.get("fonte_publicacao"):
            linhas.append(f"  Fonte da publicação: {d['fonte_publicacao']}")
        em = d.get("ementa") or "—"
        if len(em) > orcamento_por_doc:
            em = em[:orcamento_por_doc].rsplit(" ", 1)[0] + "… [CORTADO pelo orçamento de caracteres]"
        linhas.append(f"\n**Ementa (integral, literal do portal):**\n{em}")
        linhas.append(f"\n**Dispositivo (campo \"Decisão\", literal):**\n{d.get('decisao') or '— (não informado pelo portal)'}")
        it = d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") or ""
        if it:
            if len(it) > orcamento_por_doc:
                it = it[:orcamento_por_doc].rsplit(" ", 1)[0] + "… [CORTADO pelo orçamento de caracteres]"
            linhas.append(f"\n**Inteiro teor (literal, {'eproc da TNU' if d.get('inteiro_teor_texto') else 'embutido no portal'}):**\n{it}")
        else:
            linhas.append(f"\n{_nota_inteiro_teor(d)}")
    base = docs[0].get("base") or "trf1"
    if any(d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") for d in docs):
        linhas.append(
            f"\n---\nPara a ficha de precedente: `tribunal: \"{BASES[base]['rotulo']}\"`, `id_documento` = id acima, "
            "`julgamento` em ISO, `ementa`/`dispositivo`/`trecho` literais (cortes com [...]). Com o inteiro "
            "teor acima lido por inteiro, `verificacao` pode ser \"inteiro teor lido\"; relator e órgão da "
            "ficha vêm do TEXTO (cabeçalho, 'RELATOR:'), não do índice."
        )
    else:
        linhas.append(
            "\n---\nPara a ficha de precedente: `tribunal: \"TRF1\"`, `id_documento` = id acima, "
            "`julgamento` em ISO, `ementa`/`dispositivo` literais (cortes com [...]). O portal NÃO "
            "expõe o voto: `verificacao` fica em \"só ementa/índice\" — em `limites`, diga o que só "
            "o voto resolveria. \"Inteiro teor lido\" só depois de abrir o PDF no navegador."
        )
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# Controle de ritmo — orçamento COMPARTILHADO entre processos                 #
# (copiado do servidor do TJRO: cada sessão do Claude sobe seu próprio processo #
# deste servidor; em memória, cada um contaria sozinho. Estado em ARQUIVO, sob  #
# trava: todos dividem o mesmo orçamento e recuam juntos quando há bloqueio.)   #
# --------------------------------------------------------------------------- #
# Cada operação do usuário custa 2 a 4 requisições (sessão + [toggle avançada] + busca +
# [paginação]; obter_decisao custa sempre 4), por isso a janela é maior que a do TJRO. A
# reserva é por requisição: uma operação pode ser cortada no meio pelo teto — decisão
# consciente (reservar N vagas de uma vez com devolução é maior do que vale hoje). O CJF não mostrou WAF até agora; a escada
# só alarga se um dia mostrar.
_JANELA_MAX_REQS = 24
_ESCADA_JANELA_S = [60.0, 5 * 60.0, 10 * 60.0, 20 * 60.0, 30 * 60.0]
_SUCESSOS_PARA_RELAXAR = 100
_BACKOFF_INICIAL_S = 10 * 60.0
_BACKOFF_MAXIMO_S = 60 * 60.0
_ESPACAMENTO_MIN_S = 1.5
_ESPERA_MAXIMA_S = 30.0

_ARQUIVO_ESTADO_DISJUNTOR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".disjuntor_estado_trf1.json"
)

_ESTADO_PADRAO: dict[str, Any] = {
    "versao": 1,
    "requisicoes": [],
    "proximo_livre_em": 0.0,
    "bloqueado_ate": 0.0,
    "indice_janela": 0,
    "sucessos": 0,
    "backoff_s": _BACKOFF_INICIAL_S,
    "incidentes": [],
    "ultima_requisicao_em": 0.0,
    "total_requisicoes": 0,
}
_MAX_INCIDENTES = 20


@contextlib.contextmanager
def _trava_estado():
    f = None
    try:
        if fcntl is not None:
            f = open(_ARQUIVO_ESTADO_DISJUNTOR + ".lock", "w")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass


def _ler_estado() -> dict[str, Any]:
    estado = dict(_ESTADO_PADRAO)
    try:
        with open(_ARQUIVO_ESTADO_DISJUNTOR, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except Exception:
        return estado
    estado.update({k: v for k, v in dados.items() if k in _ESTADO_PADRAO})
    agora = time.time()

    def _num(v, padrao: float) -> float:
        try:
            n = float(v)
            return n if n == n and n not in (float("inf"), float("-inf")) else padrao
        except Exception:
            return padrao

    margem = _ESPERA_MAXIMA_S + _ESPACAMENTO_MIN_S
    reqs = estado.get("requisicoes") or []
    estado["requisicoes"] = [
        float(t) for t in reqs if isinstance(t, (int, float)) and float(t) <= agora + margem
    ]
    estado["proximo_livre_em"] = min(_num(estado.get("proximo_livre_em"), 0.0), agora + margem)
    estado["bloqueado_ate"] = max(0.0, min(_num(estado.get("bloqueado_ate"), 0.0), agora + _BACKOFF_MAXIMO_S))
    estado["indice_janela"] = max(0, min(int(_num(estado.get("indice_janela"), 0)), len(_ESCADA_JANELA_S) - 1))
    estado["backoff_s"] = max(_BACKOFF_INICIAL_S, min(_num(estado.get("backoff_s"), _BACKOFF_INICIAL_S), _BACKOFF_MAXIMO_S))
    inc = estado.get("incidentes")
    estado["incidentes"] = inc[-_MAX_INCIDENTES:] if isinstance(inc, list) else []
    return estado


_estado_memoria: dict | None = None
_persistencia_indisponivel: str | None = None


def _transacao(fn):
    global _estado_memoria, _persistencia_indisponivel
    with _trava_estado():
        if _persistencia_indisponivel and _estado_memoria is not None:
            estado = _estado_memoria
        else:
            estado = _ler_estado()
        resultado = fn(estado)
        try:
            tmp = f"{_ARQUIVO_ESTADO_DISJUNTOR}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(estado, f)
            os.replace(tmp, _ARQUIVO_ESTADO_DISJUNTOR)
            _persistencia_indisponivel = None
            _estado_memoria = None
        except Exception as e:
            _persistencia_indisponivel = getattr(e, "strerror", None) or type(e).__name__
            _estado_memoria = estado
        return resultado


def _fmt_hms(segundos: float) -> str:
    segundos = max(0, int(segundos))
    m, s = divmod(segundos, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}min" if m else f"{h}h"
    if m:
        return f"{m}min{s:02d}s" if s else f"{m}min"
    return f"{s}s"


def _reservar_requisicao(agora: float | None = None) -> dict:
    agora = time.time() if agora is None else agora

    def _decidir(e: dict) -> dict:
        if agora < e["bloqueado_ate"]:
            return {"erro": (
                "O portal do CJF/TRF1 bloqueou uma consulta recente (suspeita de automação ou "
                "recusa explícita); para não prolongar o bloqueio, esta ferramenta está evitando "
                f"novas tentativas por mais {_fmt_hms(e['bloqueado_ate'] - agora)}. Enquanto isso, "
                "o portal jurisprudencia.cjf.jus.br/trf1 segue acessível no navegador."
            )}
        janela = _ESCADA_JANELA_S[e["indice_janela"]]
        e["requisicoes"] = [t for t in e["requisicoes"] if agora - t <= janela]
        if len(e["requisicoes"]) >= _JANELA_MAX_REQS:
            espera = janela - (agora - e["requisicoes"][0])
            return {"erro": (
                f"Muitas consultas em pouco tempo (limite atual: {_JANELA_MAX_REQS} requisições a cada "
                f"{_fmt_hms(janela)}, compartilhado por todos os processos desta extensão nesta máquina; "
                "cada busca gasta 2 a 4). "
                f"Aguarde {_fmt_hms(espera)} e tente de novo."
            )}
        vaga = max(agora, e["proximo_livre_em"])
        esperar = vaga - agora
        if esperar > _ESPERA_MAXIMA_S:
            return {"erro": (
                f"Fila de espera longa demais ({_fmt_hms(esperar)}) — há consultas demais em "
                "andamento em paralelo. Refaça a busca daqui a pouco, de preferência uma por vez."
            )}
        e["proximo_livre_em"] = vaga + _ESPACAMENTO_MIN_S
        e["requisicoes"].append(vaga)
        e["ultima_requisicao_em"] = vaga
        e["total_requisicoes"] = int(e.get("total_requisicoes", 0)) + 1
        return {"esperar_s": esperar}

    return _transacao(_decidir)


def _registrar_bloqueio_detectado(
    agora: float | None = None, operacao: str = "?", subir_escada: bool = True, espera_minima_s: float = 0.0,
) -> None:
    agora = time.time() if agora is None else agora

    def _aplicar(e: dict) -> None:
        reqs = e.get("requisicoes") or []
        janela = _ESCADA_JANELA_S[e["indice_janela"]]
        anteriores = e.get("incidentes") or []
        e["incidentes"] = (anteriores + [{
            "quando": agora,
            "operacao": operacao,
            "nivel": e["indice_janela"],
            "janela_s": int(janela),
            "reqs_ultimos_60s": len([t for t in reqs if agora - t <= 60]),
            "reqs_na_janela": len([t for t in reqs if agora - t <= janela]),
            "desde_ultima_req_s": int(agora - e["ultima_requisicao_em"]) if e.get("ultima_requisicao_em") else None,
            "desde_incidente_anterior_s": int(agora - anteriores[-1]["quando"]) if anteriores else None,
        }])[-_MAX_INCIDENTES:]
        e["bloqueado_ate"] = agora + max(e["backoff_s"], espera_minima_s)
        e["backoff_s"] = min(e["backoff_s"] * 2, _BACKOFF_MAXIMO_S)
        if subir_escada and e["indice_janela"] < len(_ESCADA_JANELA_S) - 1:
            e["indice_janela"] += 1
        e["sucessos"] = 0

    _transacao(_aplicar)


def _registrar_sucesso() -> None:
    def _aplicar(e: dict) -> None:
        e["backoff_s"] = _BACKOFF_INICIAL_S
        e["sucessos"] += 1
        if e["sucessos"] >= _SUCESSOS_PARA_RELAXAR:
            e["sucessos"] = 0
            if e["indice_janela"] > 0:
                e["indice_janela"] -= 1

    _transacao(_aplicar)


def _diagnostico_ritmo(agora: float | None = None) -> str:
    agora = time.time() if agora is None else agora
    with _trava_estado():
        e = _ler_estado()
    janela = _ESCADA_JANELA_S[e["indice_janela"]]
    na_janela = len([t for t in (e.get("requisicoes") or []) if agora - t <= janela])
    linhas = [
        "**Controle de ritmo do MCP TRF1 (portal do CJF)**",
        f"- Nível atual: {e['indice_janela'] + 1} de {len(_ESCADA_JANELA_S)} "
        f"(limite: {_JANELA_MAX_REQS} requisições a cada {_fmt_hms(janela)}; cada busca gasta 2 a 4)",
        f"- Orçamento usado agora: {na_janela}/{_JANELA_MAX_REQS} nesta janela",
        f"- Requisições desde o início (nesta máquina): {e.get('total_requisicoes', 0)}",
        (
            f"- ⚠️ BLOQUEADO — liberando em {_fmt_hms(e['bloqueado_ate'] - agora)}"
            if agora < e["bloqueado_ate"] else "- Situação: liberado"
        ),
    ]
    if _persistencia_indisponivel:
        linhas.insert(1, (
            f"- ⚠️ AVISO: não foi possível gravar {_ARQUIVO_ESTADO_DISJUNTOR} "
            f"({_persistencia_indisponivel}) — o orçamento NÃO está sendo compartilhado entre processos."
        ))
    inc = e.get("incidentes") or []
    if not inc:
        linhas.append("\nNenhum bloqueio registrado até agora nesta máquina.")
        return "\n".join(linhas)
    linhas.append(f"\n**Bloqueios registrados: {len(inc)}** (mais recentes primeiro)")
    for i in list(reversed(inc))[:8]:
        quando = time.strftime("%Y-%m-%d %H:%M", time.localtime(i["quando"]))
        intervalo = "—" if i.get("desde_ultima_req_s") is None else f"{i['desde_ultima_req_s']}s"
        linhas.append(
            f"- {quando} · {i['reqs_ultimos_60s']} requisições no minuto anterior, "
            f"{i['reqs_na_janela']} na janela de {_fmt_hms(i['janela_s'])} · "
            f"intervalo desde a anterior: {intervalo} · operação: {i['operacao']}"
        )
    media = sum(i["reqs_ultimos_60s"] for i in inc) / len(inc)
    pouco = len([i for i in inc if i["reqs_ultimos_60s"] <= 2])
    linhas.append(f"\n**Padrão observado:** em média {media:.1f} requisições no minuto que antecedeu cada bloqueio.")
    if pouco > len(inc) / 2:
        linhas.append(
            "A maioria dos bloqueios veio com pouquíssimo tráfego desta máquina — indício de "
            "causa fora do controle desta ferramenta (outro equipamento no mesmo IP, ou o próprio "
            "portal apertando o filtro). Espaçar mais aqui tende a não resolver."
        )
    elif media >= 8:
        linhas.append("Os bloqueios vieram após rajadas — preferir uma busca ampla a várias seguidas é o que mais ajuda.")
    return "\n".join(linhas)


# Cache de respostas idênticas (por processo): repetir a MESMA busca na mesma conversa
# não deve gerar nova sessão e nova requisição.
_CACHE_TTL_S = 5 * 60.0
_CACHE_MAX = 32
_cache_respostas: "dict[str, tuple[float, Any]]" = {}


def _cache_ler(chave: str):
    item = _cache_respostas.get(chave)
    if not item:
        return None
    quando, dados = item
    if time.time() - quando > _CACHE_TTL_S:
        _cache_respostas.pop(chave, None)
        return None
    return dados


def _cache_gravar(chave: str, dados) -> None:
    if len(_cache_respostas) >= _CACHE_MAX:
        _cache_respostas.pop(next(iter(_cache_respostas)), None)
    _cache_respostas[chave] = (time.time(), dados)


# --------------------------------------------------------------------------- #
# Camada HTTP — sessão JSF (cookies + ViewState) e detecção de bloqueio        #
# --------------------------------------------------------------------------- #
# Assinaturas de bloqueio. "captcha" genérico NÃO entra: a página inicial do portal carrega
# o script do reCAPTCHA do formulário "Fale conosco" (falso positivo real em 11/09/2026,
# que armou o disjuntor por 10 min na primeira requisição do teste online).
_RE_BLOQUEIO = re.compile(
    r"<title>\s*just a moment|cf-mitigated|challenges\.cloudflare\.com|cf-chl|"
    r"acesso (foi )?bloqueado|p[aá]gina bloqueada|robotiza|suspeita de automa",
    re.IGNORECASE,
)
_RE_SESSAO_EXPIRADA = re.compile(r"ViewExpired|sess[aã]o expirad|view state could not be restored", re.I)


def _envelope(texto: str) -> str:
    """Resposta sem o conteúdo dos CDATA (onde vivem as ementas). A página do Cloudflare e o
    <error-name>ViewExpiredException</error-name> do JSF não têm CDATA — continuam visíveis.
    CDATA aberto sem fechar = resposta cortada: corta ali."""
    e = re.sub(r"(?s)<!\[CDATA\[.*?\]\]>", " ", texto or "")
    return e.split("<![CDATA[")[0][:20_000]


class PortalRecusou(RuntimeError):
    """Bloqueio/recusa explícita do portal (já registrada no disjuntor)."""


class SessaoInvalida(RuntimeError):
    """A sessão JSF não serve mais (ViewState/JSESSIONID); tentar uma vez com sessão nova."""


def _diagnosticar_resposta(status: int, ctype: str, texto: str, esperado: str) -> str:
    if _RE_BLOQUEIO.search(texto or ""):
        return (
            "O portal recusou a consulta com uma página de bloqueio/desafio anti-robô "
            f"(HTTP {status}). Costuma ser temporário — aguarde antes de tentar de novo; "
            "o portal segue acessível no navegador."
        )
    if _RE_SESSAO_EXPIRADA.search(texto or ""):
        return "A sessão do portal expirou (ViewState); a ferramenta vai reabrir a sessão."
    return (
        f"O portal respondeu algo inesperado (esperava {esperado}; veio HTTP {status}, "
        f"content-type={ctype!r}). Pode ser instabilidade temporária do CJF — tente de novo em instantes."
    )


def _pares_para_form(pares) -> dict[str, Any]:
    """Lista de pares (nome, valor) → dict que o httpx codifica como form, repetindo o nome
    quando o valor é lista (checkboxes). Lista crua em `data=` vira stream síncrono e o
    AsyncClient recusa ("Attempted to send an sync request") — erro real de 11/09/2026."""
    if pares is None or isinstance(pares, dict):
        return dict(pares or {})
    form: dict[str, Any] = {}
    for k, v in pares:
        if k in form:
            form[k] = (form[k] if isinstance(form[k], list) else [form[k]]) + [v]
        else:
            form[k] = v
    return form


async def _requisitar(cli: "httpx.AsyncClient", metodo: str, url: str, operacao: str,
                      data: dict | None = None, headers: dict | None = None, esperado: str = "html") -> str:
    """Uma requisição ao portal, passando pelo disjuntor. Lê o corpo ANTES de decidir pelo
    status (mesma ordem do TJRO: um 403/429 precisa alimentar o disjuntor, não só estourar)."""
    reserva = _reservar_requisicao()
    if "erro" in reserva:
        raise RuntimeError(reserva["erro"])
    if reserva["esperar_s"] > 0:
        await asyncio.sleep(reserva["esperar_s"])
    if metodo == "GET":
        r = await cli.get(url, headers=headers)
    else:
        r = await cli.post(url, data=_pares_para_form(data), headers=headers)
    ctype = r.headers.get("content-type", "").lower()
    texto = r.text or ""
    # Só o ENVELOPE (fora dos CDATA): ementa que fale de "acesso bloqueado" ou "sessão
    # expirada" é texto de acórdão, não sinal do portal (red team 11/09/2026: na resposta
    # de paginação a 1ª ementa começa no offset ~6k, dentro da janela — armaria 10 min).
    envelope = _envelope(texto)
    eh_bloqueio = bool(_RE_BLOQUEIO.search(envelope))
    if eh_bloqueio or r.status_code in (403, 429):
        try:
            espera_minima = float(r.headers.get("retry-after") or 0)
        except ValueError:
            espera_minima = 0.0
        _registrar_bloqueio_detectado(operacao=operacao, subir_escada=eh_bloqueio, espera_minima_s=espera_minima)
        raise PortalRecusou(_diagnosticar_resposta(r.status_code, ctype, texto, esperado))
    if _RE_SESSAO_EXPIRADA.search(envelope):
        raise SessaoInvalida(_diagnosticar_resposta(r.status_code, ctype, texto, esperado))
    if r.status_code >= 400:
        raise RuntimeError(_diagnosticar_resposta(r.status_code, ctype, texto, esperado))
    if esperado == "xml" and "<partial-response" not in texto[:2000]:
        raise RuntimeError(_diagnosticar_resposta(r.status_code, ctype, texto, "XML <partial-response>"))
    if esperado == "html" and "javax.faces.ViewState" not in texto:  # "qualquer": sem checagem
        raise RuntimeError(_diagnosticar_resposta(r.status_code, ctype, texto, "a página do formulário"))
    _registrar_sucesso()
    return texto


def _form_busca(consulta: str, tipos: list[str], fontes: list[str], viewstate: str,
                avancados: dict[str, str] | None = None, mapa: dict[str, str] | None = None,
                tipo_data: str = "julgamento", tipo_acordao: list[str] | None = None) -> list[tuple[str, str]]:
    """Campos do POST de busca (lista de pares, porque checkboxes repetem o nome)."""
    form: list[tuple[str, str]] = [
        ("javax.faces.partial.ajax", "true"),
        ("javax.faces.source", "formulario:actPesquisar"),
        ("javax.faces.partial.execute", "@all"),
        ("javax.faces.partial.render", "formulario"),
        ("formulario:actPesquisar", "formulario:actPesquisar"),
        ("formulario", "formulario"),
        ("formulario:textoLivre", consulta),
    ]
    form += [("formulario:selectTiposDocumento", t) for t in tipos]
    form += [(CAMPO_FONTE, f) for f in fontes]
    form += [("formulario:tipoAcordao", t) for t in (tipo_acordao or [])]
    if avancados:
        mapa = mapa or CAMPOS_AVANCADOS_FALLBACK
        form.append(("formulario:ckbAvancada_input", "on"))
        form.append(("formulario:combo_tipo_data_input", TIPOS_DATA.get(tipo_data, "DTDP")))
        for chave, valor in avancados.items():
            form.append((mapa.get(chave, CAMPOS_AVANCADOS_FALLBACK[chave]), valor))
    form.append(("javax.faces.ViewState", viewstate))
    return form


def _form_toggle_avancada(tipos: list[str], fontes: list[str], viewstate: str) -> list[tuple[str, str]]:
    form: list[tuple[str, str]] = [
        ("javax.faces.partial.ajax", "true"),
        ("javax.faces.source", "formulario:ckbAvancada"),
        ("javax.faces.partial.execute", "formulario:ckbAvancada"),
        ("javax.faces.partial.render", "formulario:pesquisaAvancada"),
        ("javax.faces.behavior.event", "change"),
        ("javax.faces.partial.event", "change"),
        ("formulario", "formulario"),
        ("formulario:ckbAvancada_input", "on"),
    ]
    form += [("formulario:selectTiposDocumento", t) for t in tipos]
    form += [(CAMPO_FONTE, f) for f in fontes]
    form.append(("javax.faces.ViewState", viewstate))
    return form


def _form_paginacao(first: int, rows: int, viewstate: str) -> list[tuple[str, str]]:
    return [
        ("javax.faces.partial.ajax", "true"),
        ("javax.faces.source", "formulario:tabelaDocumentos"),
        ("javax.faces.partial.execute", "formulario:tabelaDocumentos"),
        ("javax.faces.partial.render", "formulario:tabelaDocumentos"),
        ("formulario:tabelaDocumentos_pagination", "true"),
        ("formulario:tabelaDocumentos_first", str(first)),
        ("formulario:tabelaDocumentos_rows", str(rows)),
        ("formulario:tabelaDocumentos_rppDD", str(rows)),
        ("formulario:tabelaDocumentos_encodeFeature", "true"),
        ("formulario", "formulario"),
        ("javax.faces.ViewState", viewstate),
    ]


async def _consultar_portal(consulta: str, tipos: list[str], fontes: list[str], pagina: int,
                            por_pagina: int, avancados: dict[str, str], tipo_data: str,
                            operacao: str, base: str = "trf1", tipo_acordao: list[str] | None = None) -> dict:
    """Fluxo completo numa sessão NOVA (cookies + ViewState do GET inicial), como o
    navegador faz: [toggle avançada] → busca → [paginação]. Sessão por chamada evita
    ViewState vencido e estado residual do painel avançado entre consultas."""
    if httpx is None:
        raise RuntimeError("pacote 'httpx' não instalado")
    chave = json.dumps([base, consulta, tipos, fontes, pagina, por_pagina, avancados, tipo_data, tipo_acordao], ensure_ascii=False)
    endpoint = SITE + BASES[base]["caminho"]
    headers_ajax = dict(HEADERS_AJAX, Referer=endpoint)
    em_cache = _cache_ler(chave)
    if em_cache is not None:
        return em_cache

    async def _uma_sessao() -> dict:
        avisos: list[str] = []
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True, headers=HEADERS_BASE) as cli:
            inicial = await _requisitar(cli, "GET", endpoint, operacao, esperado="html")
            viewstate = _extrair_viewstate(inicial)
            if not viewstate:
                raise RuntimeError("ViewState não encontrado na página inicial do portal — layout mudou?")
            mapa: dict[str, str] | None = None
            if avancados:
                xml_t = await _requisitar(cli, "POST", endpoint, operacao, data=_form_toggle_avancada(tipos, fontes, viewstate),
                                          headers=headers_ajax, esperado="xml")
                viewstate = _extrair_viewstate(xml_t) or viewstate
                painel = _extrair_update(xml_t, "formulario:pesquisaAvancada")
                mapa, av = _mapear_campos_avancados(painel)
                avisos += av
            xml_b = await _requisitar(cli, "POST", endpoint, operacao,
                                      data=_form_busca(consulta, tipos, fontes, viewstate, avancados, mapa, tipo_data, tipo_acordao),
                                      headers=headers_ajax, esperado="xml")
            msgs = _extrair_mensagens(xml_b)
            if msgs:
                raise RuntimeError("O portal recusou a consulta: " + " | ".join(msgs))
            html_form = _extrair_update(xml_b, "formulario")
            total = _extrair_total(html_form)
            html_docs = html_form
            first = (pagina - 1) * por_pagina
            # Zero resultado de verdade (nenhum "doc_" na própria página 1, total também 0):
            # nenhuma outra página vai trazer nada — não vale gastar uma requisição de
            # paginação nem confundir com o aviso de "total não informado" (achado real
            # 11/09/2026: com por_pagina!=30 e busca genuinamente vazia, a ferramenta pagava
            # uma requisição a mais e emitia dois avisos para um resultado que era só zero).
            zero_de_verdade = not total and not _RE_DOC_SPLIT.search(html_form)
            if zero_de_verdade:
                html_docs = html_form
            elif first > 0 or por_pagina != 30:
                if not total:
                    avisos.append("o portal não informou o total desta consulta — a paginação pode estar imprecisa")
                if total and first >= total:
                    html_docs = ""
                else:
                    viewstate = _extrair_viewstate(xml_b) or viewstate
                    xml_p = await _requisitar(cli, "POST", endpoint, operacao, data=_form_paginacao(first, por_pagina, viewstate),
                                              headers=headers_ajax, esperado="xml")
                    html_docs = _extrair_update(xml_p, "formulario:tabelaDocumentos") or ""
                    if not html_docs:
                        avisos.append("paginação não devolveu resultados — mostrando a 1ª página")
                        html_docs = html_form
            return {"total": total, "docs": _parsear_documentos(html_docs, base), "avisos": avisos}

    try:
        dados = await _uma_sessao()
    except SessaoInvalida:
        try:
            dados = await _uma_sessao()  # uma única nova tentativa com sessão nova
        except SessaoInvalida:
            raise RuntimeError(
                "A sessão do portal expirou duas vezes seguidas (ViewState recusado) — "
                "instabilidade do CJF; tente de novo em instantes."
            )
    _cache_gravar(chave, dados)
    return dados


INTEIRO_TEOR_MAX_DOCS = 3  # teto de acórdãos da TNU baixados por chamada (1 requisição cada)


async def _anexar_inteiro_teor_tnu(docs: list[dict], operacao: str) -> list[str]:
    """Baixa o HTML do inteiro teor no eproc da TNU (sem desafio, confirmado 11/09/2026) para
    até INTEIRO_TEOR_MAX_DOCS documentos e guarda o texto em d["inteiro_teor_texto"]."""
    avisos: list[str] = []
    alvos = [d for d in docs if d.get("link_tipo") == "tnu"][:INTEIRO_TEOR_MAX_DOCS]
    if len([d for d in docs if d.get("link_tipo") == "tnu"]) > INTEIRO_TEOR_MAX_DOCS:
        avisos.append(f"inteiro teor baixado só para os {INTEIRO_TEOR_MAX_DOCS} primeiros documentos (teto por chamada)")
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True, headers=HEADERS_BASE) as cli:
        for d in alvos:
            try:
                em_cache = _cache_ler("it:" + d["link_inteiro_teor"])
                if em_cache is None:
                    html_doc = await _requisitar(cli, "GET", d["link_inteiro_teor"], operacao, esperado="qualquer")
                    em_cache = _texto_documento(html_doc)
                    _cache_gravar("it:" + d["link_inteiro_teor"], em_cache)
                d["inteiro_teor_texto"] = em_cache
                if len(d["inteiro_teor_texto"]) < 200:
                    avisos.append(f"inteiro teor do id {d['id']} veio vazio/curto — abra o link no navegador")
            except Exception as e:  # um documento falhar não derruba a resposta
                avisos.append(f"inteiro teor do id {d['id']} não baixado ({type(e).__name__}: {e})")
    return avisos


def _normalizar_para_comparar(t: str) -> str:
    t = _sem_destaque(t or "")
    t = _fold(t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _verificar_trecho(textos: dict[str, str], trecho: str) -> dict:
    """Confere se `trecho` aparece literalmente em algum dos textos (ementa, dispositivo,
    inteiro teor). Tolerante a caixa, acento, pontuação e espaço; intolerante a palavra
    trocada/omitida. `[...]` no trecho separa fragmentos que devem aparecer nessa ordem."""
    fragmentos = [f for f in (x.strip() for x in re.split(r"\[\s*\.\.\.\s*\]|\[…\]|…", trecho or "")) if f]
    if not fragmentos:
        return {"valido": False, "onde": None, "faltando": [], "motivo": "trecho vazio"}
    faltando_por_texto: dict[str, list[str]] = {}
    for nome, texto in textos.items():
        alvo = _normalizar_para_comparar(texto)
        if not alvo:
            continue
        pos, faltando = 0, []
        for frag in fragmentos:
            f = _normalizar_para_comparar(frag)
            i = alvo.find(f, pos)
            if i < 0:
                faltando.append(frag)
            else:
                pos = i + len(f)
        if not faltando:
            return {"valido": True, "onde": nome, "faltando": [], "motivo": f"trecho encontrado literalmente em: {nome}"}
        faltando_por_texto[nome] = faltando
    melhor = min(faltando_por_texto.items(), key=lambda kv: len(kv[1]))[1] if faltando_por_texto else fragmentos
    return {"valido": False, "onde": None, "faltando": melhor,
            "motivo": "trecho NÃO encontrado literalmente — não cite entre aspas; parafraseie ou corrija"}


# --------------------------------------------------------------------------- #
# Implementação das ferramentas                                                #
# --------------------------------------------------------------------------- #
def _validar_base(base: str | None) -> str:
    b = (base or "trf1").strip().lower()
    if b not in BASES:
        raise ValueError(f"base inválida: {base!r}; use 'trf1' (padrão), 'tnu' ou 'colegiado'")
    return b


def _validar_tipos(tipo: list[str] | None, base: str = "trf1") -> list[str]:
    permitidos = BASES[base]["tipos"]
    if not permitidos:
        return []
    tipos = [str(t).upper().strip() for t in (tipo or ["ACORDAO"])]
    aliases = {"ACÓRDÃO": "ACORDAO", "SÚMULA": "SUMULA", "ARGUIÇÃO": "ARGUICAO",
               "DECISÃO MONOCRÁTICA": "DECISAOMONO", "DECISAO MONOCRATICA": "DECISAOMONO", "MONOCRATICA": "DECISAOMONO"}
    tipos = [aliases.get(t, t) for t in tipos]
    inv = [t for t in tipos if t not in permitidos]
    if inv:
        raise ValueError(f"tipo inválido para a base {base}: {inv}; use {permitidos}")
    return list(dict.fromkeys(tipos))


def _validar_tipo_acordao(tipo_acordao: list[str] | None, base: str) -> list[str]:
    if not tipo_acordao:
        return []
    if not BASES[base]["tipo_acordao"]:
        raise ValueError("tipo_acordao (REPRESENTATIVO/RELEVANTE) só existe na base 'tnu'")
    vals = [str(t).upper().strip() for t in tipo_acordao]
    inv = [v for v in vals if v not in TIPOS_ACORDAO_TNU]
    if inv:
        raise ValueError(f"tipo_acordao inválido: {inv}; use {list(TIPOS_ACORDAO_TNU)}")
    return list(dict.fromkeys(vals))


def _validar_fontes(fonte: list[str] | None) -> list[str]:
    fontes = [str(f).upper().strip() for f in (fonte or ["TRF1"])]
    inv = [f for f in fontes if f not in FONTES_VALIDAS]
    if inv:
        raise ValueError(f"fonte inválida: {inv}; use {FONTES_VALIDAS}")
    return list(dict.fromkeys(fontes))


async def _buscar(consulta: str, tipo, fonte, grupos, relator, orgao_julgador, classe, origem, numero,
                  ementa_decisao, referencia_legislativa, data_inicio, data_fim, tipo_data, pagina, por_pagina,
                  base: str = "trf1", tipo_acordao=None) -> str:
    try:
        base = _validar_base(base)
        tipos = _validar_tipos(tipo, base)
        fontes = _validar_fontes(fonte) if BASES[base]["fonte"] else []
        tipo_acordao_v = _validar_tipo_acordao(tipo_acordao, base)
        if tipo_data not in TIPOS_DATA:
            raise ValueError(f"tipo_data inválido: {tipo_data!r}; use 'julgamento' ou 'publicacao'")
        pagina = max(1, int(pagina or 1))
        por_pagina = int(por_pagina or 30)
        if por_pagina not in POR_PAGINA_VALIDOS:
            raise ValueError(f"por_pagina inválido: {por_pagina}; o portal aceita {POR_PAGINA_VALIDOS}")
        avancados: dict[str, str] = {}
        for chave, valor in (("relator", relator), ("orgao_julgador", orgao_julgador), ("classe", classe),
                             ("origem", origem), ("numero", numero), ("ementa_decisao", ementa_decisao),
                             ("referencia_legislativa", referencia_legislativa)):
            if valor and str(valor).strip():
                avancados[chave] = str(valor).strip()
        if data_inicio:
            avancados["data_inicio"] = _normalizar_data(str(data_inicio), "data_inicio")
        if data_fim:
            avancados["data_fim"] = _normalizar_data(str(data_fim), "data_fim")
        if avancados and not BASES[base]["avancada"]:
            raise ValueError(
                f"filtros (relator, órgão, classe, número, datas…) só existem na base 'trf1'; na base '{base}' "
                "use a sintaxe de campo na consulta: nome[REL], \"turma\"[ORGA], 20240101[DTDP]"
            )
        consulta_montada = _montar_consulta(consulta, grupos)
        if not consulta_montada and not avancados and not tipo_acordao_v:
            raise ValueError("Informe a consulta, ou grupos de termos, ou pelo menos um filtro.")
        dados = await _consultar_portal(consulta_montada, tipos, fontes, pagina, por_pagina, avancados, tipo_data,
                                        "busca", base, tipo_acordao_v)
    except (ValueError, RuntimeError) as e:
        return f"Erro na consulta ao TRF1: {e}"
    except Exception as e:  # rede, timeout...
        return f"Erro ao consultar o portal do CJF/TRF1 ({type(e).__name__}): {e}"
    meta = {"consulta_montada": consulta_montada, "tipos": tipos, "fontes": fontes, "pagina": pagina,
            "por_pagina": por_pagina, "filtros": avancados, "avisos": dados.get("avisos"), "base": base,
            "tipo_acordao": tipo_acordao_v}
    return _format_busca(dados["docs"], dados["total"], meta)


def _filtrar_por_numero(docs: list[dict], digitos: str) -> list[dict]:
    """Só documento cujo número bate: igual, ou (número parcial) por PREFIXO. Red team
    11/09/2026: `digitos in nd or nd in digitos` casava com documento SEM número (""),
    e "Súmula 12" (nd="12") casava com quase toda CNJ — e o aviso de divergência nunca
    disparava porque a lista não ficava vazia."""
    out = []
    for d in docs:
        nd = d.get("numero_digitos") or ""
        if nd and (nd == digitos or (len(digitos) < 20 and nd.startswith(digitos))):
            out.append(d)
    return out


async def _localizar_por_numero(numero: str, base: str, operacao: str, com_inteiro_teor: bool = True) -> tuple[dict, list[dict]]:
    """Todos os documentos sob um número: na base trf1 pelo campo Número do painel avançado
    (match exato); nas outras, busca livre pelos dígitos (o motor indexa a forma sem pontuação).
    Na TNU, baixa também o inteiro teor (até INTEIRO_TEOR_MAX_DOCS)."""
    digitos = _so_digitos(numero)
    if base == "trf1":
        dados = await _consultar_portal("", BASES["trf1"]["tipos"], list(FONTES_VALIDAS), 1, 50,
                                        {"numero": digitos}, "julgamento", operacao, base)
    else:
        dados = await _consultar_portal(digitos, BASES[base]["tipos"], [], 1, 50, {}, "julgamento", operacao, base)
    docs = _filtrar_por_numero(dados["docs"], digitos)
    # NUNCA mutar `dados` (é o dict guardado no cache — a mesma chamada repetida na mesma
    # janela de 5 min devolve o MESMO objeto; mutar em local duplicava avisos a cada chamada,
    # achado real 11/09/2026). Devolve um dict NOVO com os avisos combinados.
    avisos_extra = await _anexar_inteiro_teor_tnu(docs, operacao) if (docs and com_inteiro_teor and base == "tnu") else []
    resultado = {"total": dados["total"], "docs": dados["docs"], "avisos": list(dados.get("avisos") or []) + avisos_extra}
    return resultado, docs


async def _obter_decisao(numero: str, base: str = "trf1") -> str:
    digitos = _so_digitos(numero)
    if len(digitos) < 7:
        return f"Número muito curto para localizar com segurança: {numero!r}."
    try:
        base = _validar_base(base)
        dados, docs = await _localizar_por_numero(numero, base, "decisao")
        if not docs and dados["docs"]:
            docs = dados["docs"]
            dados = {**dados, "avisos": list(dados.get("avisos") or []) + ["o portal devolveu documentos cujo número não bate exatamente — confira"]}
    except (ValueError, RuntimeError) as e:
        return f"Erro na consulta ao TRF1: {e}"
    except Exception as e:
        return f"Erro ao consultar o portal do CJF/TRF1 ({type(e).__name__}): {e}"
    saida = _format_decisao(docs, _cnj(digitos) if len(digitos) == 20 else numero, dados["total"])
    for a in dados.get("avisos") or []:
        saida = f"⚠️ {a}\n" + saida
    return saida


async def _verificar_citacao(numero: str, trecho: str, base: str = "trf1") -> str:
    digitos = _so_digitos(numero)
    if len(digitos) < 7:
        return f"Número muito curto para localizar com segurança: {numero!r}."
    if not (trecho or "").strip():
        return "Informe o trecho que pretende citar entre aspas."
    try:
        base = _validar_base(base)
        dados, docs = await _localizar_por_numero(numero, base, "verificacao")
    except (ValueError, RuntimeError) as e:
        return f"Erro na consulta ao portal do CJF: {e}"
    except Exception as e:
        return f"Erro ao consultar o portal do CJF ({type(e).__name__}): {e}"
    if not docs:
        return f"Nenhum documento sob o número {numero} na base {base} — não há como verificar; não cite."
    linhas = []
    algum = False
    for d in docs:
        textos = {"ementa": d.get("ementa") or "", "dispositivo": d.get("decisao") or "",
                  "inteiro teor": d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") or ""}
        r = _verificar_trecho(textos, trecho)
        algum = algum or r["valido"]
        marca = "✅ VÁLIDO" if r["valido"] else "❌ NÃO ENCONTRADO"
        linhas.append(f"{marca} · id {d['id']} · {d.get('tipo') or '?'} · julgado em {d.get('data_julgamento') or '?'} · {r['motivo']}")
        if not r["valido"] and r["faltando"]:
            for f in r["faltando"][:3]:
                linhas.append(f"   fragmento sem correspondência: «{f[:160]}»")
    cabec = (f"**Verificação literal — {docs[0].get('numero') or numero} ({BASES[base]['rotulo']}, {len(docs)} documento(s))**")
    rodape = ("\nCobre ementa, dispositivo" + (" e inteiro teor" if any(d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") for d in docs) else
              " — NÃO o voto (o portal não o expõe nesta base)") +
              ". Comparação tolerante a caixa, acento, pontuação e espaço; `[...]` separa fragmentos em ordem. "
              "Se ❌: não cite entre aspas — parafraseie, ou confira o voto no navegador.")
    for a in dados.get("avisos") or []:
        linhas.insert(0, f"⚠️ {a}")
    return "\n".join([cabec] + linhas) + rodape


# --------------------------------------------------------------------------- #
# Registro das ferramentas MCP                                                 #
# --------------------------------------------------------------------------- #
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("Jurisprudência TRF1")

    @mcp.tool()
    async def buscar_jurisprudencia_trf1(
        consulta: str = "",
        tipo: list[str] | None = None,
        fonte: list[str] | None = None,
        grupos: list[list[str]] | None = None,
        relator: str | None = None,
        orgao_julgador: str | None = None,
        classe: str | None = None,
        origem: str | None = None,
        numero: str | None = None,
        ementa_decisao: str | None = None,
        referencia_legislativa: str | None = None,
        data_inicio: str | None = None,
        data_fim: str | None = None,
        tipo_data: str = "julgamento",
        pagina: int = 1,
        por_pagina: int = 30,
        base: str = "trf1",
        tipo_acordao: list[str] | None = None,
    ) -> str:
        """Pesquisa jurisprudência do TRF1 (Tribunal Regional Federal da 1ª Região) e das Turmas
        Recursais/JEF1 no portal oficial do CJF (jurisprudencia.cjf.jus.br/trf1), sem login —
        e, com base="tnu", a Turma Nacional de Uniformização (PUIL, precedentes representativos).

        É a fonte dos precedentes da Justiça Federal da 1ª Região (Rondônia está nela — INSS, Caixa,
        União, JEF federal). Cada resultado traz ementa e DISPOSITIVO literais do portal; o voto
        não é exposto (o inteiro teor é link para o navegador).

        A busca casa PALAVRAS, não sentido. Sintaxe do motor (escreva na `consulta`):
          termo1 E termo2 · termo1 OU termo2 · termo1 NAO termo2 · "frase exata" · (a OU b) E c
          termo1 ADJ2 termo2 (adjacentes, até 2 palavras entre) · termo1 PROX5 termo2 (mesma frase)
          termo1 COM termo2 (mesma sentença) · termo1 MESMO termo2 (mesmo parágrafo)
          desapropria$ (radical: desapropriação, desapropriado…) · termo[EMEN] (só na ementa),
          termo[DECI] (só no dispositivo), nome[REL] (relator), "quarta turma"[ORGA], 20240101[DTDP]
        NÃO use preposições, artigos nem pontuação (o motor não aceita): `"dano moral" E negativação`,
        não `dano moral por negativação`. Amplitude: E ⊇ MESMO ⊇ COM ⊇ PROX ⊇ ADJ.
        Julgados do mesmo assunto usam vocabulários diferentes: use `grupos` — cada grupo é uma
        lista de sinônimos (OU) e os grupos se somam (E); a ferramenta monta a sintaxe e escapa
        cada termo. Duas técnicas que rendem mais que várias buscas: ancorar pela súmula/tema que
        os julgados citam (grupo ["Súmula 385"]) e, após a 1ª busca, colher o vocabulário do melhor
        resultado (obter_decisao_trf1) para a próxima. Cada busca gasta 2 a 4 requisições ao portal;
        prefira uma busca bem construída a várias seguidas.

        Args:
            consulta: Termos na sintaxe do motor (acima). Pode ficar vazia se houver grupos ou filtro.
            tipo: Tipos de documento: ["ACORDAO"] (padrão), "SUMULA", "ARGUICAO", "DECISAOMONO".
            fonte: ["TRF1"] (padrão) e/ou "JEF1" (Turmas Recursais dos Juizados Federais).
            grupos: Grupos de sinônimos, ex.: [["dano moral"],["negativação","inscrição indevida",
                "cadastro de inadimplentes"]]. Expressão com espaço vira frase exata; `$` só no fim
                de palavra; `termo[EMEN]` no FIM do termo restringe ao campo. Toda outra pontuação
                (aspas internas, `:`, `{}`, `?`) é removida — para sintaxe avançada use `consulta`.
                Até 6 grupos × 12 termos.
            relator: Nome (ou parte) do relator — filtro do painel avançado do portal.
            orgao_julgador: Ex.: "QUINTA TURMA", "SEGUNDA SEÇÃO".
            classe: Sigla ou nome da classe (ex.: "AC", "AMS", "AG", "ApCiv").
            origem: Seção judiciária de origem (ex.: "RO", "SEÇÃO JUDICIÁRIA DE RONDÔNIA").
            numero: Número do processo (com ou sem pontuação) — restringe ao campo Número.
            ementa_decisao: Termos buscados SÓ na ementa/dispositivo (a consulta livre varre tudo).
            referencia_legislativa: Norma indexada manualmente pelo portal (ex.: "lei 8429") — cobertura
                parcial: zero aqui NÃO significa que não há acórdãos sobre a norma.
            data_inicio: Início do período (AAAA-MM-DD ou DD/MM/AAAA).
            data_fim: Fim do período (mesmos formatos).
            tipo_data: A que data o período se refere: "julgamento" (padrão) ou "publicacao".
            pagina: Página de resultados (1+).
            por_pagina: 10, 30 (padrão) ou 50 — os valores que o portal aceita. Use 50 só
                combinado com filtro que já reduza o total (data, órgão, relator, número) —
                50 itens de uma busca ampla e sem filtro pode gerar resposta grande demais
                (achado real 11/09/2026: "dano moral" sem filtro, 50 por página, estourou
                o limite de saída do chamador).
            base: "trf1" (padrão: TRF1 + JEF1), "tnu" (Turma Nacional de Uniformização — tipos
                ACORDAO/DECISAOMONO/DECISAOPRES, sem filtros de painel: use nome[REL] etc. na
                consulta; inteiro teor disponível em texto via obter_decisao_trf1) ou "colegiado"
                (decisões ADMINISTRATIVAS do Conselho — raramente serve a litígio).
            tipo_acordao: Só na base "tnu": ["REPRESENTATIVO"] (Representativos de Controvérsia —
                tese firmada) e/ou ["RELEVANTE"] (Precedentes Relevantes). Resultados qualificados
                aparecem com ★ e a qualificação entra na citação.

        Returns:
            Cabeçalho com o TOTAL real, a consulta montada e os filtros; por resultado: tipo,
            classe, número, ID DO DOCUMENTO (chave única daquela decisão — sob o mesmo número
            convivem acórdão, embargos e decisão monocrática), relator (e convocado / para acórdão),
            órgão, datas, citação pronta para peça no padrão "(TRF-1 - AC: nº, Relator: …, Data de
            Julgamento: …, TURMA, Data de Publicação: …)" — com a referência INTEIRA em hiperlink
            markdown para o inteiro teor quando o portal deu um link específico deste documento
            (arquivo.trf1/eproc da TNU; nunca o link genérico do PJe), ementa (trecho de 800
            caracteres — ementa numerada aplica a tese nos últimos itens), dispositivo (trecho) e a
            nota de inteiro teor.
            Avisa "mesmo número, N documentos" e resultados opostos no mesmo julgamento. Com 3+
            resultados, resume offline quantos julgamentos declaram cada resultado. Antes de citar,
            use obter_decisao_trf1 para a ementa e o dispositivo integrais.
        """
        return await _buscar(consulta, tipo, fonte, grupos, relator, orgao_julgador, classe, origem, numero,
                             ementa_decisao, referencia_legislativa, data_inicio, data_fim, tipo_data, pagina, por_pagina,
                             base, tipo_acordao)

    @mcp.tool()
    async def obter_decisao_trf1(numero: str, base: str = "trf1") -> str:
        """Traz TODOS os documentos publicados no portal do CJF sob um número de processo do TRF1/JEF1,
        com ementa e dispositivo INTEGRAIS e literais — é o que substitui o "inteiro teor" aqui.
        Na base "tnu" traz também o INTEIRO TEOR (relatório, voto, votantes) baixado do eproc da TNU
        (até 3 documentos por chamada) — aí "inteiro teor lido" é possível.

        Use antes de citar qualquer julgado devolvido por buscar_jurisprudencia_trf1. Lista os
        julgamentos distintos sob o mesmo número (acórdão, embargos, decisão monocrática), cada um
        com data, id, relator e órgão. O portal NÃO expõe o voto: para a ficha de precedente,
        `verificacao` fica em "só ementa/índice"; "inteiro teor lido" só depois de abrir o PDF no
        navegador (link na resposta; o arquivo.trf1.jus.br exige desafio Cloudflare e esta
        ferramenta não o lê).

        Args:
            numero: Número do processo (CNJ), com ou sem pontuação.
            base: "trf1" (padrão), "tnu" ou "colegiado" — a mesma em que o documento foi achado.

        Returns:
            Cabeçalho com a contagem de documentos sob o número; para cada um, citação pronta,
            metadados, ementa integral, dispositivo integral e a nota de inteiro teor; no fim, o
            que preencher na ficha de precedente. Saída limitada a ~50 mil caracteres.
        """
        return await _obter_decisao(numero, base)

    @mcp.tool()
    async def verificar_citacao_trf1(numero: str, trecho: str, base: str = "trf1") -> str:
        """Confere se um trecho aparece LITERALMENTE na ementa/dispositivo (e, na TNU, no inteiro teor)
        do julgado, antes de ir entre aspas para a peça.

        USE antes de qualquer citação direta. Comparação tolerante a caixa, acento, pontuação e
        espaço; intolerante a palavra trocada ou omitida. `[...]` no trecho separa fragmentos que
        devem aparecer nessa ordem. Se vier ❌, não cite entre aspas: parafraseie (sem aspas) ou
        confira o voto no navegador. Cobre só o que o portal expõe — na base trf1, NÃO o voto.

        Args:
            numero: Número do processo (CNJ), com ou sem pontuação.
            trecho: Texto que se pretende citar entre aspas (cortes marcados com [...]).
            base: "trf1" (padrão), "tnu" ou "colegiado".

        Returns:
            Por documento sob o número: ✅/❌, onde foi encontrado, e os fragmentos sem
            correspondência quando falhar.
        """
        return await _verificar_citacao(numero, trecho, base)

    @mcp.tool()
    async def diagnostico_ritmo_trf1() -> str:
        """Mostra por que as buscas do TRF1 podem estar falhando.

        Relata o nível atual do limite de ritmo, o orçamento consumido, se há bloqueio em curso
        (e quanto falta para liberar) e o histórico de bloqueios com o contexto de cada um.
        USE ISTO antes de concluir que "o portal está fora do ar". Não faz nenhuma requisição.
        """
        return _diagnostico_ritmo()

    _HAS_MCP = True
except Exception as _erro_mcp:  # permite importar o módulo (testes) sem o pacote mcp instalado
    # Nunca silenciar: em 11/09/2026 um erro de registro de tool foi engolido aqui e a
    # mensagem de saída culpou "pacote mcp não instalado" com o pacote instalado.
    import traceback as _tb

    print(f"[servidor_trf1] registro MCP falhou: {type(_erro_mcp).__name__}: {_erro_mcp}", file=sys.stderr)
    _tb.print_exc(file=sys.stderr)
    mcp = None
    _HAS_MCP = False


# --------------------------------------------------------------------------- #
# Ponto de entrada                                                             #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import tempfile as _tempfile

        base = os.path.dirname(os.path.abspath(__file__))
        fx = os.path.join(base, "fixtures")

        def _ler(nome: str) -> str:
            with open(os.path.join(fx, nome), encoding="utf-8") as f:
                return f.read()

        # --- 1. montagem de consulta ---
        assert _termo_para_query("dano moral") == '"dano moral"'
        assert _termo_para_query("negativação") == "negativação"
        assert _termo_para_query("desapropria$") == "desapropria$"
        assert _termo_para_query("$signado") == "signado", _termo_para_query("$signado")
        assert _termo_para_query("com") == '"com"'  # reservada isolada vira frase
        assert _termo_para_query("prox3") == '"prox3"'
        assert _termo_para_query('"já entre aspas"') == '"já entre aspas"'
        assert _termo_para_query("art. 1.022, CPC") == '"art 1 022 CPC"', _termo_para_query("art. 1.022, CPC")
        assert _termo_para_query("   ") == ""
        # red team 11/09: sufixo de campo preservado; aspas internas removidas; colchete no meio é pontuação
        assert _termo_para_query("dano moral[EMEN]") == '"dano moral"[EMEN]', _termo_para_query("dano moral[EMEN]")
        assert _termo_para_query("negativação[-DOUT]") == "negativação[-DOUT]"
        assert _termo_para_query("[EMEN]") == ""
        assert _termo_para_query('aspas" solta') == '"aspas solta"', _termo_para_query('aspas" solta')
        assert _termo_para_query("a[b]c") == '"a b c"'
        # caracteres que o portal recusa (mensagem real 11/09/2026): viram espaço
        assert _termo_para_query("auxílio-doença") == '"auxílio doença"', _termo_para_query("auxílio-doença")
        assert _termo_para_query("pré-executividade") == '"pré executividade"'
        assert _termo_para_query("art_1;x|y@z#!+") == '"art 1 x y z"', _termo_para_query("art_1;x|y@z#!+")
        assert _montar_consulta("auxílio-doença E perícia", None) == "auxílio doença E perícia"
        assert not any(c in _montar_consulta("a-b; c'd", [["e-f"]]) for c in _INVALIDOS_PORTAL)
        g = _montar_grupos([["dano moral"], ["negativação", "inscrição indevida", "negativação"]])
        assert g == '"dano moral" E (negativação OU "inscrição indevida")', g
        assert _montar_grupos([[], ["x"]]) == "x"
        assert _montar_grupos("nao e lista") == ""  # type: ignore[arg-type]
        assert len(_montar_grupos([["t"]] * 10).split(" E ")) == GRUPOS_MAX
        assert _montar_consulta("prescrição E fazenda", [["INSS"]]) == "(prescrição E fazenda) E INSS"
        assert _montar_consulta("", [["a", "b"]]) == "(a OU b)"
        assert _montar_consulta("  x  ", None) == "x"

        # --- 2. parsing do fixture real ---
        xml = _ler("02_busca_dano_moral.xml")
        assert _extrair_viewstate(xml).count(":") == 1
        assert _extrair_mensagens(xml) == [], _extrair_mensagens(xml)
        formulario = _extrair_update(xml, "formulario")
        assert formulario and "table_pesquisa_lista" in formulario
        assert _extrair_total(formulario) == 25943, _extrair_total(formulario)
        docs = _parsear_documentos(formulario)
        assert len(docs) == 30, len(docs)
        d0 = docs[0]
        assert d0["id"] == "1174043" and d0["numero"] == "0001369-17.2017.4.01.3606", d0
        assert d0["numero_digitos"] == "00013691720174013606"
        assert d0["tipo"] == "Acórdão" and d0["sigla"] == "ACP" and d0["orgao"] == "QUINTA TURMA", d0
        assert d0["relator"].startswith("JUIZ FEDERAL CHARLES RENAUD"), d0["relator"]
        assert d0["data_julgamento"] == "14/08/2026" and d0["data_publicacao"] == "14/08/2026"
        assert d0["fonte_publicacao"] == "PJe 14/08/2026 PAG", d0["fonte_publicacao"]  # duplicata removida
        assert "«DANO»" in d0["ementa"] or "«DANOS»" in d0["ementa"], d0["ementa"][:200]
        assert d0["decisao"].startswith("Decide a Quinta Turma"), d0["decisao"][:80]
        assert d0["link_tipo"] == "arquivo" and d0["link_inteiro_teor"].endswith("p1=00013691720174013606"), d0["link_inteiro_teor"]
        # destaque do portal fica só na ementa/dispositivo; em metadado contaminaria a citação
        bloco_dest = ('<table class="table_pesquisa_lista" id="doc_7"><div class="ui-outputpanel ui-widget"><tr><td><span class="label_pontilhada">Relator(a)</span></td></tr>'
                      '<tr><td>DESEMBARGADOR FEDERAL <font color="blue"><b>NEY BELLO</b></font></td></tr></div>'
                      '<div class="ui-outputpanel ui-widget"><tr><td><span class="label_pontilhada">Data</span></td></tr><tr><td><font color="blue"><b>29/04/2024</b></font></td></tr></div>'
                      '<div class="ui-outputpanel ui-widget"><tr><td><span class="label_pontilhada">Ementa</span></td></tr><tr><td>ATO DE <font color="blue"><b>IMPROBIDADE</b></font></td></tr></div>')
        dd = _parsear_documentos(bloco_dest)[0]
        assert dd["relator"] == "DESEMBARGADOR FEDERAL NEY BELLO" and dd["data_julgamento"] == "29/04/2024", dd
        assert dd["ementa"] == "ATO DE «IMPROBIDADE»", dd["ementa"]
        assert "«" not in _citacao(dd), _citacao(dd)
        # red team 11/09: <td> com atributo e tabela dentro da ementa
        bloco_td = ('<table class="table_pesquisa_lista" id="doc_8"><div class="ui-outputpanel ui-widget">'
                    '<tr><td><span class="label_pontilhada">Relator(a)</span></td></tr><tr class="x"><td class="v">FULANO</td></tr></div>'
                    '<div class="ui-outputpanel ui-widget"><tr><td><span class="label_pontilhada">Ementa</span></td></tr>'
                    '<tr><td>PARTE A <table><tr><td>celula</td></tr></table> PARTE B</td></tr></div>')
        dt = _parsear_documentos(bloco_td)[0]
        assert dt["relator"] == "FULANO" and dt["ementa"] == "PARTE A celula PARTE B", dt
        # filtro por número: sem número e "Súmula 12" não casam; prefixo casa
        sem_num = dict(d0, numero="", numero_digitos="")
        sumula = dict(d0, numero="Súmula 12", numero_digitos="12")
        assert _filtrar_por_numero([sem_num, sumula, d0], d0["numero_digitos"]) == [d0]
        assert _filtrar_por_numero([d0], "0001369") == [d0] and _filtrar_por_numero([d0], "1234567") == []
        # envelope: ementa com "acesso bloqueado"/"sessão expirada" dentro do CDATA não é bloqueio
        xml_falso = xml.replace("AMBIENTAL. CONSTITUCIONAL.", "ACESSO BLOQUEADO. SUSPEITA DE AUTOMAÇÃO. SESSÃO EXPIRADA.", 1)
        assert "ACESSO BLOQUEADO" in xml_falso and not _RE_BLOQUEIO.search(_envelope(xml_falso))
        assert not _RE_SESSAO_EXPIRADA.search(_envelope(xml_falso))
        assert _RE_BLOQUEIO.search(_envelope(_ler("07_arquivo_menu.html")))
        assert _RE_SESSAO_EXPIRADA.search(_envelope("<partial-response><error><error-name>class javax.faces.application.ViewExpiredException</error-name></error></partial-response>"))
        assert _extrair_update('<update id="a"><![CDATA[x]]]]><![CDATA[>y]]></update>', "a") == "x]]>y"
        # vocabulário real dos dispositivos
        assert _resultado_de("conhecer do agravo e dar-lhe provimento") >= {"PROVIDO", "CONHECIDO"}
        assert "DESPROVIDO" in _resultado_de("negar-lhe provimento") and "PROVIDO" in _resultado_de("dar integral provimento à apelação")
        rp = _resumo_resultados_pagina([dict(d0, decisao="julgar prejudicado o recurso"), dict(d0, numero_digitos="1", decisao="não conhecer da apelação")])
        assert rp["contagem"]["PREJUDICADO"] == 1 and rp["contagem"]["NÃO CONHECIDO"] == 1 and rp["sem_resultado"] == 0, rp
        # citação: origem diferente vira segmento, não prefixo
        cj = _citacao(dict(d0, origem="TURMA RECURSAL - RO"))
        assert cj.startswith("(TRF-1 - ACP: ") and "Origem: TURMA RECURSAL - RO" in cj, cj
        assert "Origem:" not in _citacao(d0)
        # --- bases tnu e colegiado (fixtures reais 09, 11, 12) ---
        xml_tnu = _ler("11_tnu_busca.xml")
        docs_tnu = _parsear_documentos(_extrair_update(xml_tnu, "formulario"), "tnu")
        assert len(docs_tnu) == 30 and docs_tnu[0]["id"].startswith("TNU"), (len(docs_tnu), docs_tnu[:1])
        assert _extrair_total(_extrair_update(xml_tnu, "formulario")) == 725
        assert all(d["tipo"] == "Acórdão" for d in docs_tnu) and any(d["qualificacao"] for d in docs_tnu)
        q = next(d for d in docs_tnu if d["qualificacao"])
        assert q["qualificacao"] in ("Precedente Relevante", "Representativo de Controvérsia"), q["qualificacao"]
        assert "Precedente" not in q["tipo"] and q["campos"]["Tipo"] == "Acórdão" and q["campos"]["Qualificação do precedente"] == q["qualificacao"]
        assert all(d["link_tipo"] == "tnu" for d in docs_tnu)
        ct = _citacao(docs_tnu[0])
        assert ct.startswith("(TNU - ") and "Origem:" not in ct and "TURMA NACIONAL DE UNIFORMIZAÇÃO" in ct, ct
        assert "disponível em texto" in _nota_inteiro_teor(docs_tnu[0])
        assert docs_tnu[0]["link_tipo"] == "tnu"
        assert _citacao_com_link(docs_tnu[0]) == f"[{ct}]({docs_tnu[0]['link_inteiro_teor']})"
        sb = _format_busca(docs_tnu[:3], 725, {"consulta_montada": "x", "tipos": ["ACORDAO"], "fontes": [], "pagina": 1, "por_pagina": 30, "base": "tnu", "tipo_acordao": ["RELEVANTE"]})
        assert "CJF/TNU" in sb and "precedentes: Precedentes Relevantes" in sb and "fonte:" not in sb, sb[:300]
        xml_col = _ler("09_colegiado_busca.xml")
        docs_col = _parsear_documentos(_extrair_update(xml_col, "formulario"), "colegiado")
        assert len(docs_col) == 15 and docs_col[0]["data_julgamento"] == "" and docs_col[0]["data_publicacao"]
        assert len(docs_col[0]["inteiro_teor_embutido"]) > 5000 and "RELATOR" in docs_col[0]["inteiro_teor_embutido"]
        assert _citacao(docs_col[0]).startswith("(CJF - ") and "Origem:" not in _citacao(docs_col[0])
        # colegiado sem link (embutido só como texto): citação sem hiperlink
        assert not docs_col[0]["link_inteiro_teor"]
        assert _citacao_com_link(docs_col[0]) == _citacao(docs_col[0])
        it_txt = _texto_documento(open(os.path.join(fx, "12_tnu_inteiro_teor.html"), encoding="iso-8859-1").read())
        assert 10_000 < len(it_txt) < 20_000 and "RELATOR" in it_txt and "Votante" in it_txt, len(it_txt)
        assert "<" not in it_txt[:5000]
        sd = _format_decisao([dict(docs_tnu[0], inteiro_teor_texto=it_txt)], docs_tnu[0]["numero"], 1)
        assert "Inteiro teor (literal, eproc da TNU)" in sd and "inteiro teor lido" in sd, sd[-400:]
        sd2 = _format_decisao([docs_col[0]], docs_col[0]["numero"], 1)
        assert "embutido no portal" in sd2
        # achado real 11/09/2026: busca genuinamente vazia (0 docs, sem rowCount/contador) com
        # por_pagina!=30 não deve gastar requisição de paginação nem emitir aviso de "total não
        # informado" — confere só as funções puras (o fluxo de rede é testado ao vivo)
        html_vazio_de_verdade = '<div id="formulario:tabelaDocumentos" class="ui-datagrid ui-widget"></div>'
        assert not _RE_DOC_SPLIT.search(html_vazio_de_verdade) and _extrair_total(html_vazio_de_verdade) == 0
        zero_de_verdade = not _extrair_total(html_vazio_de_verdade) and not _RE_DOC_SPLIT.search(html_vazio_de_verdade)
        assert zero_de_verdade is True

        # achado real 11/09/2026: chamar _localizar_por_numero 2x para o MESMO número (mesmo
        # cache) não pode duplicar avisos — o dict do cache nunca é mutado no lugar
        async def _consultar_fake(*a, **kw):
            return {"total": 1, "docs": [dict(d0, numero_digitos=d0["numero_digitos"])], "avisos": ["aviso original"]}
        async def _anexar_fake(docs, operacao):
            return ["teto por chamada"]
        _orig_consultar, _orig_anexar = _consultar_portal, _anexar_inteiro_teor_tnu
        globals()["_consultar_portal"], globals()["_anexar_inteiro_teor_tnu"] = _consultar_fake, _anexar_fake
        try:
            d1, _ = asyncio.run(_localizar_por_numero(d0["numero"], "tnu", "x"))
            d2, _ = asyncio.run(_localizar_por_numero(d0["numero"], "tnu", "x"))
            assert d1["avisos"] == ["aviso original", "teto por chamada"], d1["avisos"]
            assert d2["avisos"] == ["aviso original", "teto por chamada"], d2["avisos"]  # não duplicou
        finally:
            globals()["_consultar_portal"], globals()["_anexar_inteiro_teor_tnu"] = _orig_consultar, _orig_anexar
        # verificar_trecho
        vt = {"ementa": "A TESE fixada: «benefício» por incapacidade, art. 42.", "dispositivo": "negar provimento"}
        assert _verificar_trecho(vt, "tese fixada: beneficio por incapacidade")["valido"]
        assert _verificar_trecho(vt, "tese fixada [...] art 42")["onde"] == "ementa"
        r_neg = _verificar_trecho(vt, "tese fixada [...] art 43")
        assert not r_neg["valido"] and r_neg["faltando"] == ["art 43"], r_neg
        assert not _verificar_trecho(vt, "por incapacidade tese fixada")["valido"]  # ordem errada
        assert not _verificar_trecho(vt, "")["valido"]
        # validações por base
        assert _validar_tipos(None, "colegiado") == [] and _validar_tipos(["DECISAOPRES"], "tnu") == ["DECISAOPRES"]
        try:
            _validar_tipos(["SUMULA"], "tnu"); raise AssertionError("SUMULA não existe na tnu")
        except ValueError:
            pass
        assert _validar_tipo_acordao(["relevante"], "tnu") == ["RELEVANTE"]
        try:
            _validar_tipo_acordao(["RELEVANTE"], "trf1"); raise AssertionError("tipo_acordao só na tnu")
        except ValueError:
            pass
        fb = _pares_para_form(_form_busca("x", ["ACORDAO"], [], "vs", tipo_acordao=["REPRESENTATIVO"]))
        assert fb["formulario:tipoAcordao"] == "REPRESENTATIVO" and CAMPO_FONTE not in fb
        convocados = [d for d in docs if d["relator_convocado"]]
        assert len(convocados) == 2, len(convocados)  # rótulo próprio, não misturado no relator
        assert all("convocad" not in d["relator"].lower() for d in docs)
        assert any(d["link_tipo"] == "pje" for d in docs)
        cit = _citacao(d0)
        assert cit.startswith("(TRF-1 - ACP: 0001369-17.2017.4.01.3606, Relator: JUIZ FEDERAL CHARLES RENAUD"), cit
        # hiperlink na referência inteira SÓ quando o link é específico do documento
        # (arquivo.trf1/eproc da TNU); nunca no genérico do PJe (achado 14/09/2026)
        assert d0["link_tipo"] == "arquivo"
        cl = _citacao_com_link(d0)
        assert cl == f"[{cit}]({d0['link_inteiro_teor']})", cl
        d_pje = next(x for x in docs if x["link_tipo"] == "pje")
        assert _citacao_com_link(d_pje) == _citacao(d_pje), "link genérico do PJe não deve virar hiperlink"
        assert _link_documento_especifico(dict(d0, link_tipo="", link_inteiro_teor="")) == ""
        assert _link_documento_especifico(dict(d0, link_tipo="outro", link_inteiro_teor="https://x")) == "https://x"
        assert "Data de Julgamento: 14/08/2026, QUINTA TURMA, Data de Publicação: 14/08/2026)" in cit, cit
        # dispositivo → resultado
        assert _resultado_de(d0["decisao"]) == {"DESPROVIDO"}, _resultado_de(d0["decisao"])
        assert _lado_de({"PROVIDO", "DESPROVIDO"}, _OPOSTOS[0]) is None
        assert "PROVIDO" in _resultado_de("dar parcial provimento à apelação")
        assert "PREJUDICADO" in _resultado_de("julgar prejudicado o agravo")
        r = _resumo_resultados_pagina(docs)
        assert r["total_julgamentos"] == 30 and sum(r["contagem"].values()) + r["sem_resultado"] == 30, r
        saida = _format_busca(docs, 25943, {"consulta_montada": "dano moral", "tipos": ["ACORDAO"], "fontes": ["TRF1"], "pagina": 1, "por_pagina": 30})
        assert "**25943 documento(s)**" in saida and "Id. do documento: 1174043" in saida
        assert "Citação: [(TRF-1 - ACP:" in saida and "Próxima página: pagina=2" in saida
        assert "Resumo desta página" in saida and "abrir NO NAVEGADOR" in saida
        assert "Ementas cortadas" in saida
        # paginação: update de tabelaDocumentos, índices 30-59
        xml_p = _ler("04_pagina2.xml")
        docs_p = _parsear_documentos(_extrair_update(xml_p, "formulario:tabelaDocumentos"))
        assert len(docs_p) == 30 and docs_p[0]["numero"] != d0["numero"], docs_p[0]["numero"]
        saida_p = _format_busca(docs_p, 25943, {"consulta_montada": "dano moral", "tipos": ["ACORDAO"], "fontes": ["TRF1"], "pagina": 2, "por_pagina": 30})
        assert "**31. " in saida_p and "página 2/865" in saida_p
        # painel avançado: 12 inputs por posição, labels conferem
        painel = _extrair_update(_ler("05_ckbavancada.xml"), "formulario:pesquisaAvancada")
        mapa, avisos = _mapear_campos_avancados(painel)
        assert avisos == [], avisos
        assert mapa["numero"] == "formulario:j_idt28" and mapa["relator"] == "formulario:j_idt32", mapa
        assert mapa["data_inicio"].endswith("_input") and mapa["data_fim"] == "formulario:j_idt50_input", mapa
        mapa2, avisos2 = _mapear_campos_avancados("<input name=\"formulario:j_idt99\">")
        assert avisos2 and mapa2 == CAMPOS_AVANCADOS_FALLBACK
        # mensagens de erro do JSF (fixture sintético com o texto real observado)
        erro_xml = ('<partial-response><changes><update id="j_idt16:messages"><![CDATA[<span class="ui-messages-error-summary">Error!</span>'
                    '<span class="ui-messages-error-detail">É necessário selecionar pelo menos uma fonte de pesquisa!</span>]]></update></changes></partial-response>')
        assert _extrair_mensagens(erro_xml) == ["É necessário selecionar pelo menos uma fonte de pesquisa!"]
        # bloqueio Cloudflare real capturado
        cf = _ler("07_arquivo_menu.html")
        assert _RE_BLOQUEIO.search(cf), "assinatura Cloudflare não reconhecida"
        # regressão: a página inicial (com o script do reCAPTCHA do "Fale conosco") e as
        # respostas normais NÃO podem ser lidas como bloqueio
        for nome in ("01_index.html", "02_busca_dano_moral.xml", "05_ckbavancada.xml", "06_ajuda.html"):
            assert not _RE_BLOQUEIO.search(_ler(nome)), f"falso positivo de bloqueio em {nome}"
        assert "bloqueio/desafio" in _diagnosticar_resposta(403, "text/html", cf, "html")
        assert "inesperado" in _diagnosticar_resposta(500, "text/html", "<html>manutenção</html>", "xml")
        # formulários
        f = dict(_form_busca("x", ["ACORDAO"], ["TRF1"], "vs"))
        assert f["formulario:textoLivre"] == "x" and f[CAMPO_FONTE] == "TRF1" and "formulario:ckbAvancada_input" not in f
        fa = _form_busca("", ["ACORDAO"], ["TRF1"], "vs", {"relator": "Ney Bello", "data_inicio": "01/01/2024"}, mapa, "publicacao")
        fad = dict(fa)
        assert fad["formulario:ckbAvancada_input"] == "on" and fad["formulario:combo_tipo_data_input"] == "DTPP"
        assert fad["formulario:j_idt32"] == "Ney Bello" and fad["formulario:j_idt48_input"] == "01/01/2024"
        assert [v for k, v in fa if k == "formulario:selectTiposDocumento"] == ["ACORDAO"]
        fm = _pares_para_form(_form_busca("x", ["ACORDAO", "SUMULA"], ["TRF1", "JEF1"], "vs"))
        assert fm["formulario:selectTiposDocumento"] == ["ACORDAO", "SUMULA"] and fm[CAMPO_FONTE] == ["TRF1", "JEF1"], fm
        assert fm["formulario:textoLivre"] == "x" and _pares_para_form(None) == {}
        pg = dict(_form_paginacao(30, 30, "vs"))
        assert pg["formulario:tabelaDocumentos_first"] == "30" and pg["javax.faces.source"] == "formulario:tabelaDocumentos"
        assert _normalizar_data("2024-01-31", "d") == "31/01/2024" and _normalizar_data("31/01/2024", "d") == "31/01/2024"
        try:
            _normalizar_data("31-01-2024", "data_fim"); raise AssertionError("deveria recusar")
        except ValueError:
            pass
        assert _validar_tipos(["acórdão", "sumula"]) == ["ACORDAO", "SUMULA"]
        assert _validar_fontes(None) == ["TRF1"]
        # obter_decisao: formatação com 2 documentos sob o mesmo número
        irmao = dict(d0, id="999", tipo="Decisão Monocrática", data_julgamento="01/02/2025", decisao="dou provimento ao recurso")
        s2 = _format_decisao([d0, irmao], d0["numero"], 2)
        assert "**2 documento(s)" in s2 and "Julgamentos distintos" in s2 and "só ementa/índice" in s2
        assert "Ementa (integral" in s2 and "Dispositivo (campo" in s2
        assert "Nenhum documento" in _format_decisao([], "123", 0)
        # mesmo número + resultado oposto na busca
        s3 = _format_busca([d0, irmao], 2, {"consulta_montada": "x", "tipos": ["ACORDAO"], "fontes": ["TRF1"], "pagina": 1, "por_pagina": 30})
        assert "Mesmo número, 2 documentos" in s3, s3
        irmao2 = dict(irmao, data_julgamento=d0["data_julgamento"])
        s4 = _format_busca([d0, irmao2], 2, {"consulta_montada": "x", "tipos": ["ACORDAO"], "fontes": ["TRF1"], "pagina": 1, "por_pagina": 30})
        assert "resultados opostos" in s4, s4

        # --- 3. disjuntor (estado em arquivo temporário) ---
        globals()["_ARQUIVO_ESTADO_DISJUNTOR"] = os.path.join(_tempfile.gettempdir(), "_selftest_disjuntor_trf1.json")

        def _limpar_estado() -> None:
            for suf in ("", ".lock"):
                try:
                    os.unlink(_ARQUIVO_ESTADO_DISJUNTOR + suf)
                except OSError:
                    pass
            _cache_respostas.clear()

        _limpar_estado()
        t0 = 1_000_000.0
        r1 = _reservar_requisicao(t0)
        assert "erro" not in r1 and r1["esperar_s"] == 0, r1
        r2 = _reservar_requisicao(t0 + 0.1)
        assert "erro" not in r2 and r2["esperar_s"] >= 1.0, r2
        _limpar_estado()
        t0 = 2_000_000.0
        for i in range(_JANELA_MAX_REQS):
            assert "erro" not in _reservar_requisicao(t0 + i * 2), i
        estourou = _reservar_requisicao(t0 + _JANELA_MAX_REQS * 2)
        assert "Muitas consultas" in estourou["erro"], estourou
        assert "Muitas consultas" in _reservar_requisicao(t0 + _JANELA_MAX_REQS * 2 + 1)["erro"]  # outro processo vê
        _limpar_estado()
        t0 = 3_000_000.0
        _registrar_bloqueio_detectado(t0)
        recuo = _reservar_requisicao(t0 + 1)
        assert "evitando novas tentativas" in recuo["erro"] and "9min" in recuo["erro"], recuo
        assert "erro" not in _reservar_requisicao(t0 + 10 * 60 + 1)

        def _janela_visivel(base_t: float) -> str:
            for i in range(_JANELA_MAX_REQS):
                _reservar_requisicao(base_t + i * 2)
            return _reservar_requisicao(base_t + _JANELA_MAX_REQS * 2)["erro"]

        _limpar_estado()
        t = 4_000_000.0
        assert "1min" in _janela_visivel(t)
        t += 40 * 60
        _registrar_bloqueio_detectado(t)
        t += 40 * 60
        assert "5min" in _janela_visivel(t)
        for _ in range(8):
            t += 70 * 60
            _registrar_bloqueio_detectado(t)
        t += 70 * 60
        assert "30min" in _janela_visivel(t)
        _limpar_estado()
        t = 5_000_000.0
        _registrar_bloqueio_detectado(t)
        _registrar_bloqueio_detectado(t + 1)
        t += 70 * 60
        for _ in range(_SUCESSOS_PARA_RELAXAR):
            _registrar_sucesso()
        assert "5min" in _janela_visivel(t)
        _limpar_estado()
        t = 7_000_000.0
        for i in range(6):
            _reservar_requisicao(t + i * 2)
        _registrar_bloqueio_detectado(t + 20, "busca")
        rel = _diagnostico_ritmo(t + 21)
        assert "BLOQUEADO" in rel and "Bloqueios registrados: 1" in rel and "operação: busca" in rel, rel
        _limpar_estado()
        assert "Nenhum bloqueio registrado" in _diagnostico_ritmo(9_000_000.0)
        # saneamento de estado corrompido + 403 seco não sobe a escada
        _limpar_estado()
        agora = time.time()
        with open(_ARQUIVO_ESTADO_DISJUNTOR, "w", encoding="utf-8") as fh:
            json.dump({"bloqueado_ate": agora + 99 * 3600, "backoff_s": -5000, "indice_janela": 999,
                       "requisicoes": "nao e lista", "incidentes": {"x": 1}}, fh)
        est = _ler_estado()
        assert est["bloqueado_ate"] <= agora + _BACKOFF_MAXIMO_S + 1 and est["backoff_s"] >= _BACKOFF_INICIAL_S
        assert est["indice_janela"] == len(_ESCADA_JANELA_S) - 1 and est["requisicoes"] == [] and est["incidentes"] == []
        _limpar_estado()
        _registrar_bloqueio_detectado(agora, "busca", subir_escada=False, espera_minima_s=1800)
        est = _ler_estado()
        assert est["indice_janela"] == 0 and est["bloqueado_ate"] >= agora + 1800 - 1
        _limpar_estado()
        resid = [x for x in os.listdir(_tempfile.gettempdir()) if x.startswith("_selftest_disjuntor_trf1.json.") and x.endswith(".tmp")]
        assert not resid, resid
        # cache
        _cache_gravar("k", {"a": 1})
        assert _cache_ler("k") == {"a": 1} and _cache_ler("zzz") is None

        print("selftest offline OK")

        if "--online" in sys.argv:
            # volta ao arquivo de estado REAL: o teste online conta no orçamento compartilhado
            _limpar_estado()
            globals()["_ARQUIVO_ESTADO_DISJUNTOR"] = os.path.join(base, ".disjuntor_estado_trf1.json")
            async def _run() -> None:
                print("\n=== busca online: grupos + fonte TRF1 ===")
                print((await _buscar("", None, None, [["dano moral"], ["negativação", "inscrição indevida"]],
                                     None, None, None, None, None, None, None, None, None, "julgamento", 1, 10))[:3500])
                print("\n=== obter_decisao online ===")
                print((await _obter_decisao("0020777-05.2018.4.01.3300"))[:3000])
                print("\n=== TNU online: precedentes relevantes ===")
                print((await _buscar("", None, None, [["benefício por incapacidade", "auxílio-doença"]], None, None, None, None, None,
                                     None, None, None, None, "julgamento", 1, 10, "tnu", ["REPRESENTATIVO", "RELEVANTE"]))[:2500])
                print("\n=== filtro avançado online (relator + período) ===")
                print((await _buscar("improbidade", None, None, None, "NEY BELLO", None, None, None, None, None, None,
                                     "2024-01-01", "2024-12-31", "julgamento", 1, 10))[:2500])
                print("\n" + _diagnostico_ritmo())
            asyncio.run(_run())
    elif _HAS_MCP:
        mcp.run()
    else:
        sys.exit("registro MCP falhou (ver traceback acima) — se for ImportError, instale: pip install 'mcp[cli]' httpx truststore")
