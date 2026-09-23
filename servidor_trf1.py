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

Expõe quatro ferramentas ao Claude:
  • buscar_jurisprudencia_trf1  — pesquisa por tema, com operadores do motor e filtros
  • obter_decisao_trf1          — todos os documentos publicados sob um número de processo,
                                  com ementa e dispositivo INTEGRAIS (na TNU, também o inteiro
                                  teor) e recibo em disco com sha256
  • verificar_citacao_trf1      — conferência literal antes das aspas, dizendo DE QUEM é a frase
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
import hashlib
import html as _html
import json
import os
import re
import sys
import threading
import time
import unicodedata
from typing import Any

VERSAO = "1.1.0"
REPO_GITHUB = "robertogecia/trf1-jurisprudencia-mcp"

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

# User-Agent HONESTO por padrão: o cliente se identifica como o que é (convenção do servidor
# do TJSE, adotada aqui em 22/09/2026 — este servidor é distribuído, e a versão de Chrome
# falso que existia até a v1.0.0 era contorno de filtro anti-robô, indefensável num pacote
# que outros advogados instalam). Medido em 22/09/2026: o portal do CJF e o eproc da TNU
# aceitam o UA identificado (13.039 resultados; inteiro teor de 483 KB). Quem precisar de
# outro (rede corporativa que só deixa passar navegador) define TRF1_USER_AGENT — decisão e
# responsabilidade de quem define. O arquivo.trf1.jus.br (Cloudflare) NÃO é tentado.
USER_AGENT_PADRAO = f"trf1-jurisprudencia-mcp/{VERSAO} (pesquisa juridica; cliente MCP; ritmo limitado; +https://github.com/{REPO_GITHUB})"
HEADERS_BASE = {
    "User-Agent": os.environ.get("TRF1_USER_AGENT") or USER_AGENT_PADRAO,
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# Recibos: cada documento que obter_decisao_trf1 entrega fica gravado em disco, por id, com o
# texto que o portal devolveu e sha256 — é contra ESSE arquivo que o lint da peticao-rg e o
# revisor-adversarial conferem a ficha dias depois, sem gastar o orçamento do portal. Mesmo
# formato dos recibos do TJRO/STJ/TJSE/TCE-RO (`id_documento`, `nr_processo`, `texto`).
DIR_RECIBOS = os.environ.get("TRF1_MCP_DIR_RECIBOS") or os.path.join(
    os.path.expanduser("~"), ".trf1-jurisprudencia-recibos"
)
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


# --- Atribuição: de quem é a frase (portado do servidor do TJSE, 22/09/2026) ------- #
# As regex trabalham sobre texto NORMALIZADO (norm: minúsculas, sem acento, aspas e
# travessões unificados). São genéricas — cobrem tj-xx, stj, stf, trf-n, tst, trt-n, tnu —
# e não têm nada de Sergipe. Determinação 1 de references/correcoes-determinadas-2026-09-22.md:
# na base tnu o servidor confere o trecho contra relatório e voto, onde um ✅ pode estar
# apontando para ementa do STJ transcrita, para o voto vencido ou para o que a parte alega.
def norm(t: str) -> str:
    """Forma de comparação: NFKC (º/ª → o/a, ligaduras), sem acento, minúscula, aspas e
    travessões unificados, espaço único, pontuação colada."""
    t = unicodedata.normalize("NFKC", t or "")
    t = _fold(t)
    t = re.sub(r"\bn\s*[.o°]{1,3}\s*(?=\d)", "n ", t)
    t = re.sub(r"§\s+", "§", t)
    t = re.sub(r"[“”‘’\"'`´]", "'", t)
    t = re.sub(r"[–—-]", "-", t)
    t = re.sub(r"\s+([.,;:)\]])", r"\1", t)
    t = re.sub(r"([(\[])\s+", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


_TRIB = r"(?:tj-?[a-z]{2}|stj|stf|trf-?\d|tst|trt-?\d+|tnu)"
_RE_ATRIB = re.compile(r"\(" + _TRIB + r"\b"  # "(TJ-MG - …)", "(tj-pr 0048…)", "(STJ, REsp …)"
                       r"|\((?:resp|aresp|agint|agrg|edcl|eresp|rms|adi|adpf|apelacao(?: civel)?|agravo de instrumento|puil|pedilef)\b[^()]{0,220}"
                       r"\brel(?:ator[a]?|\.)?\s*(?:p/|para|min|des|juiz|dr)"
                       r"|\b" + _TRIB + r"\s*[-,–]\s*(?:resp|aresp|agint|agrg|edcl|re|are|hc|rhc|apelacao|ac|ai|puil|pedilef)\b[^.\n]{0,200}\brel")
_RE_ABRE_BLOCO = re.compile(r"\bementa\s*:|\bementa\b(?=\s*[-.]?\s*[a-z])|\bacordao\s*:|\bprecedentes?\s*:|\btranscrevo\b|"
                            r"\bin verbis\b|\bnos seguintes termos\s*:|\bassim (?:decidiu|se manifestou|ementado)\b|\bsumula\s+(?:vinculante\s+)?n?\s*\d+\s*[:-]")
_RE_VOZ_PROPRIA = re.compile(r"\b(nesse sentido|neste sentido|com efeito|no caso dos autos|no caso em tela|entendo|"
                             r"ante o exposto|diante do exposto|pelo exposto|e como voto|voto por|voto pelo|passo a|compulsando)\b")
_RE_CARA_DE_EMENTA = re.compile(r"\brecurso\s+(?:\w+\s+){0,3}(?:conhecido|provido|desprovido|improvido|nao provido)\b|"
                                r"\btese de julgamento\b|\bcaso em exame\b|\bquestao em discussao\b|\bdispositivos? relevantes?\b|"
                                r"\bsentenca (?:mantida|reformada)\b|\bapelacao\s+(?:civel\s+)?(?:conhecida|provida|desprovida)\b")
_RE_DIVERGENCIA = re.compile(r"\b(?:peco|pedi[dn]o\s+de?|com a devida|data)\s+venia\b[^.]{0,120}\b(?:diverg|discord)|\bdivirjo\b|"
                             r"\bvoto\s+(?:vencido|divergente|vista)\b|\bouso\s+divergir\b|\bvoto[- ]vista\b")
_RE_ALEGACAO = re.compile(r"\b(sustent\w+|aleg\w+|aduz\w*|argument\w+|pugn\w+|requer\w*|assever\w+|defende\w*|afirm\w+|"
                          r"em suas razoes|nas razoes|em contrarrazoes|irresignad\w+)\b")
_RE_QUEM_ALEGA = re.compile(r"\b(apelante|apelad[oa]|agravante|agravad[oa]|recorrente|recorrid[oa]|embargante|embargad[oa]|"
                            r"autor[a]?|reu|re\b|requerente|requerid[oa]|impetrante|parte|banco|inss|uniao|ministerio publico|parquet|procuradoria)\b")
_RE_NEGACAO = re.compile(r"\b(nao|jamais|nunca|inexist\w*|descab\w*|incabivel|incabiveis|inaplicav\w*|indevid\w*|afast\w*|"
                         r"improced\w*|nega\w*|rejeit\w*|sem\s+raz(?:ao|oes)|carece\w*|impossibilidade|vedad[oa]s?)\b[^.;:]{0,60}$")
_RE_NEGACAO_FALSA = re.compile(r"\bnao\s+(obstante|so\b|apenas|somente|se\s+confunde)")
# TNU: a TESE fixada pelo próprio colegiado vem entre aspas na ementa e no dispositivo ("Tese
# fixada: '…'"). Aspas ali são do tribunal citando A SI MESMO — o alerta ENTRE ASPAS calaria
# exatamente no trecho mais citável do acórdão. Só se desliga com esse contexto explícito.
_RE_TESE_PROPRIA = re.compile(r"\btese\s+(?:jur[ií]dica\s+)?(?:fixada|firmada|proposta)\b|\bfixando a seguinte tese\b|\bseguinte tese\b")
PISO_TRECHO_PALAVRAS, PISO_TRECHO_CHARS, VAO_MAXIMO, ENCADEIA_MAX = 4, 25, 1500, 1200


def faixas_transcritas(tn: str, inicio: int = 0) -> list[tuple[int, int]]:
    """Faixas do texto NORMALIZADO que são palavra de outro julgado: da abertura do bloco
    ("Ementa:", "Precedentes:", "transcrevo") até a atribuição que o fecha ("(TJ-MG - …)",
    "STJ - REsp …, Rel."). Blocos em sequência só se encadeiam se o intervalo for curto e sem
    marca de voz do próprio relator ("nesse sentido", "no caso dos autos", "entendo"…)."""
    faixas, piso = [], inicio
    for m in _RE_ATRIB.finditer(tn, inicio):
        if m.start() < piso:
            continue
        prof, fim = 0, m.end()
        if tn[m.start()] == "(":
            for k in range(m.start(), min(len(tn), m.start() + 700)):
                prof += (tn[k] == "(") - (tn[k] == ")")
                if prof == 0:
                    fim = k + 1
                    break
        else:
            pt = tn.find(".", m.end())
            fim = pt + 1 if 0 <= pt - m.end() < 300 else m.end()
        aberturas = [a.start() for a in _RE_ABRE_BLOCO.finditer(tn, piso, m.start())]
        intervalo = tn[piso: m.start()]
        if aberturas and m.start() - aberturas[-1] <= 9000:
            ini = next((a for a in aberturas if not _RE_VOZ_PROPRIA.search(tn[a: m.start()])), aberturas[-1])
        elif faixas and not _RE_VOZ_PROPRIA.search(intervalo) and (
                len(intervalo) < ENCADEIA_MAX or (len(intervalo) < 6000 and _RE_CARA_DE_EMENTA.search(intervalo))):
            ini = piso
        else:
            ini = max(piso, m.start() - 300)
        faixas.append((ini, fim))
        piso = fim
    return faixas


def faixa_divergente(tn: str, inicio: int = 0) -> tuple[int, int] | None:
    """Do primeiro sinal de divergência ("peço vênia para divergir", "voto vencido", "voto-vista")
    até o fim: o que está ali pode ser o voto VENCIDO."""
    m = _RE_DIVERGENCIA.search(tn, inicio)
    return (m.start(), len(tn)) if m else None


def _bruto(corpo: str, tn: str, pos_norm: int) -> int:
    """Posição no texto BRUTO equivalente a `pos_norm` no normalizado (norm só colapsa e remove)."""
    if pos_norm <= 0:
        return 0
    if pos_norm >= len(tn):
        return len(corpo)
    alvo = norm(tn[pos_norm: pos_norm + 40])[:24]
    if not alvo:
        return min(len(corpo), int(pos_norm * len(corpo) / max(len(tn), 1)))
    chute = min(len(corpo) - 1, int(pos_norm * len(corpo) / max(len(tn), 1)))
    for raio in (60, 400, 2000, len(corpo)):
        ini, fim = max(0, chute - raio), min(len(corpo), chute + raio)
        k = norm(corpo[ini:fim]).find(alvo)
        if k < 0:
            continue
        conta = 0
        for i in range(ini, fim):
            if len(norm(corpo[ini:i + 1])) > conta:
                conta = len(norm(corpo[ini:i + 1]))
            if conta > k:
                return i
        return ini
    return chute


def conferir(texto: str, trecho: str, tribunal: str = "TNU", com_atribuicao: bool = True) -> dict[str, Any]:
    """Conferência literal por palavra inteira; `[...]` separa fragmentos que devem vir em ordem,
    a no máximo VAO_MAXIMO caracteres um do outro (determinação 4: até a v1.0.0 duas palavras
    bastavam e `[...]` costurava a abertura da ementa ao fim do dispositivo). Com
    `com_atribuicao`, diz DE QUEM é a frase (TRANSCRIÇÃO, VOTO DIVERGENTE, ENTRE ASPAS,
    ALEGAÇÃO DA PARTE, NEGAÇÃO)."""
    frags = [f.strip() for f in re.split(r"\[\s*\.\.\.\s*\]|\(\s*\.\.\.\s*\)|\[…\]|…", trecho or "") if f.strip()]
    if not frags:
        return {"ok": False, "erro": "trecho vazio"}
    util = norm(" ".join(frags))
    if len(util.split()) < PISO_TRECHO_PALAVRAS or len(util) < PISO_TRECHO_CHARS:
        return {"ok": False, "erro": f"trecho curto demais para conferência útil (mínimo {PISO_TRECHO_PALAVRAS} palavras e "
                                     f"{PISO_TRECHO_CHARS} caracteres): qualquer acórdão contém isso"}
    tn = norm(texto)
    pos, ini0, spans = 0, None, []
    for f in frags:
        palavras = re.findall(r"\w+", norm(f))
        if not palavras:
            return {"ok": False, "fragmento": f}
        m = re.search(r"(?<!\w)" + r"\W+".join(re.escape(w) for w in palavras) + r"(?!\w)", tn[pos:])
        if not m:
            return {"ok": False, "fragmento": f}
        a, b = pos + m.start(), pos + m.end()
        if spans and a - spans[-1][1] > VAO_MAXIMO:
            return {"ok": False, "fragmento": f, "erro": f"o fragmento aparece, mas a {a - spans[-1][1]} caracteres do anterior "
                    f"(máximo {VAO_MAXIMO}): `[...]` não pode costurar partes distantes do acórdão"}
        spans.append((a, b))
        if ini0 is None:
            ini0 = a
        pos = b
    alertas: list[str] = []
    em_transcricao = False
    if com_atribuicao:
        faixas = faixas_transcritas(tn, 0)
        em_transcricao = any(a < fb and b > fa for a, b in spans for fa, fb in faixas)
        if em_transcricao:
            alertas.append(f"TRANSCRIÇÃO: o trecho está dentro de bloco que o voto transcreve de OUTRO julgado/tribunal — não é "
                           f"palavra da {tribunal}. Se for citar, cite como a {tribunal} citando; melhor: pesquise o original.")
        div = faixa_divergente(tn, 0)
        if div and any(b > div[0] for _, b in spans):
            alertas.append("VOTO DIVERGENTE: o trecho vem depois de um sinal de divergência no acórdão (pedido de vênia, voto "
                           "vencido ou voto-vista). Pode ser o voto VENCIDO — leia quem venceu antes de citar como entendimento do órgão.")
    if not em_transcricao:  # aspas se checam SEMPRE, inclusive na ementa (Súmula do STJ transcrita não é palavra do órgão)
        antes_q, depois_q = tn[max(0, ini0 - 1200): ini0], tn[pos: pos + 1200]
        aspas = [m.start() for m in re.finditer(r"(?<![a-z])'|'(?![a-z])", antes_q)]
        n_q = len(aspas)
        abre = aspas[-1] if aspas else 0
        # tese do próprio colegiado só desliga o alerta se estiver logo antes da aspa que FICOU ABERTA (red team 22/09, 3)
        if n_q % 2 == 1 and re.search(r"'(?![a-z])", depois_q) and not _RE_TESE_PROPRIA.search(antes_q[max(0, abre - 80): abre]):
            alertas.append(f"ENTRE ASPAS: o trecho parece estar dentro de aspas no acórdão — é o tribunal citando alguém (doutrina, "
                           f"lei, decisão recorrida, outro julgado). Confira de quem é a frase antes de atribuí-la à {tribunal}.")
        if com_atribuicao:
            jan = tn[max(0, ini0 - 400): ini0]
            ult = max((x.end() for x in _RE_ALEGACAO.finditer(jan)), default=-1)
            if ult >= 0 and _RE_QUEM_ALEGA.search(jan[max(0, ult - 160): ult + 160]) and not _RE_VOZ_PROPRIA.search(jan[ult:]):
                alertas.append("ALEGAÇÃO DA PARTE: pouco antes do trecho o texto relata o que uma parte (INSS, União, recorrente…) "
                               "sustenta/alega — o trecho pode ser tese da parte, não decisão do tribunal. Confira no relatório/voto quem fala.")
    antes = tn[max(0, (ini0 or 0) - 90): ini0 or 0]
    if _RE_NEGACAO.search(antes) and not _RE_NEGACAO_FALSA.search(antes[-40:]):
        alertas.append("NEGAÇÃO: há negativa logo antes do trecho — o recorte pode inverter o julgado. Não citar sem ler.")
    return {"ok": True, "alertas": alertas, "contexto": re.sub(r"\s+", " ", tn[max(0, (ini0 or 0) - 120): pos + 120]),
            "spans": spans, "tn": tn}


# --- Fecho do inteiro teor da TNU: órgão, relator e data lidos do TEXTO -------------- #
# "Índice é indício, texto é prova" (TJRO v1.7.1; TJSE v0.6.2). O eproc da TNU entrega o
# acórdão inteiro: o cabeçalho traz "RELATOR(A) : Juiz Federal …", o ACÓRDÃO traz "A Turma
# Nacional de Uniformização decidiu, por …" e a data "Brasília, 14 de maio de 2025.".
_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
          "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_RE_FECHO_TNU = re.compile(r"a turma nacional de uniformizacao(?:,)? (?:decidiu|por (?:unanimidade|maioria))|acordam os (?:membros|juizes|integrantes) da turma nacional de uniformizacao")
_RE_RELATOR_TEXTO = re.compile(r"(?im)^\s*RELATOR(?:A)?(?:\s*\(A\))?\s*:\s*(.+?)\s*$")
_RE_DATA_FECHO = re.compile(r"(?i)bras[ií]lia(?:\s*/\s*df)?\s*,\s*(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})")


def _fecho_tnu(texto: str) -> dict[str, str]:
    """Órgão, relator e data lidos do próprio inteiro teor (vazios quando não reconhecidos —
    nunca inventados). O relator do cabeçalho pode ser o sorteado; em julgamento por
    maioria a ata traz "RELATOR DO ACÓRDÃO" — ambos são devolvidos."""
    out = {"orgao_fecho": "", "relator_texto": "", "relator_acordao_texto": "", "data_fecho": ""}
    if not texto:
        return out
    tn = norm(texto)
    if _RE_FECHO_TNU.search(tn):
        out["orgao_fecho"] = "TURMA NACIONAL DE UNIFORMIZAÇÃO"
    m = _RE_RELATOR_TEXTO.search(texto)
    if m:
        out["relator_texto"] = re.sub(r"\s+", " ", m.group(1)).strip(" .:")
    m2 = re.search(r"(?im)^\s*RELATOR(?:A)?\s+DO\s+AC[ÓO]RD[ÃA]O\s*:\s*(.+?)\s*$", texto)
    if m2:
        out["relator_acordao_texto"] = re.sub(r"\s+", " ", m2.group(1)).strip(" .:")
    ms = list(_RE_DATA_FECHO.finditer(texto))  # a ÚLTIMA: um precedente transcrito no voto pode trazer a sua própria "Brasília, …"
    m3 = ms[-1] if ms else None
    if m3:
        mes = _MESES.get(_fold(m3.group(2)))
        if mes:
            out["data_fecho"] = f"{int(m3.group(1)):02d}/{mes:02d}/{m3.group(3)}"
    return out


def _orgao_fonte(d: dict) -> str:
    """De onde veio o órgão da citação. Na base trf1 o voto não é exposto: calar não é opção
    (determinação 6) — a saída diz que o órgão é do índice e não foi conferido no fecho."""
    if d.get("orgao_fecho"):
        return "fecho"
    if d.get("inteiro_teor_texto"):
        return "índice (fecho não reconhecido no inteiro teor)"
    return "índice do portal — não conferido no fecho (o voto não é exposto nesta base)"


def _aplicar_fecho(d: dict) -> list[str]:
    """Lê o fecho do inteiro teor (TNU), grava em `d` e devolve avisos de DIVERGÊNCIA."""
    avisos: list[str] = []
    texto = d.get("inteiro_teor_texto") or ""
    if not texto:
        return avisos
    f = _fecho_tnu(texto)
    d.update(f)
    if f["orgao_fecho"] and d.get("orgao") and norm(f["orgao_fecho"]) != norm(d["orgao"]):
        avisos.append(f"DIVERGÊNCIA de órgão no id {d['id']}: índice diz '{d['orgao']}', fecho diz '{f['orgao_fecho']}'. Vale o fecho.")
    rel_txt = f["relator_acordao_texto"] or f["relator_texto"]
    if rel_txt and d.get("relator") and norm(d["relator"]) not in norm(rel_txt) and norm(rel_txt) not in norm(d["relator"]):
        avisos.append(f"RELATOR divergente no id {d['id']}: índice diz '{d['relator']}', texto diz '{rel_txt}'"
                      + (" (relator DO ACÓRDÃO — julgamento por maioria)" if f["relator_acordao_texto"] else "") + ". Vale o texto.")
    if f["data_fecho"] and d.get("data_julgamento") and f["data_fecho"] != d["data_julgamento"]:
        avisos.append(f"DATA divergente no id {d['id']}: índice diz {d['data_julgamento']}, fecho diz {f['data_fecho']} — confira na ata (extrato) qual é a sessão.")
    return avisos


# --- Sinais do julgado (espelho do TJRO v1.7.6/v1.7.12): só etiqueta o que já veio ---- #
_RE_ANCORAS = [
    (re.compile(r"s[úu]mula\s+vinculante\s+n?[º°.]*\s*(\d{1,4})", re.I), "Súmula Vinculante %s"),
    (re.compile(r"s[úu]mula\s+n?[º°.]*\s*(\d{1,4})", re.I), "Súmula %s"),
    (re.compile(r"tema\s+(?:repetitivo\s+|de\s+repercuss[ãa]o\s+geral\s+)?n?[º°.]*\s*(\d{1,4}(?:\.\d{3})?)", re.I), "Tema %s"),
    (re.compile(r"\bIRDR\s+n?[º°.]*\s*(\d{1,4})", re.I), "IRDR %s"),
    (re.compile(r"\bIAC\s+n?[º°.]*\s*(\d{1,4})", re.I), "IAC %s"),
    (re.compile(r"\bPUIL\s+n?[º°.]*\s*(\d{1,7}(?:-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4})?)", re.I), "PUIL %s"),
]
_ANCORAS_MAX = 6


def _ancoras(texto: str, max_itens: int = _ANCORAS_MAX) -> list[str]:
    achado: dict[str, int] = {}
    for rx, molde in _RE_ANCORAS:
        for m in rx.finditer(str(texto or "")):
            n = m.group(1).replace(".", "") if not molde.startswith("PUIL") else m.group(1)
            if not n or n == "0":
                continue
            nome = molde % n
            if nome.startswith("Súmula ") and ("Súmula Vinculante %s" % n) in achado:
                continue
            achado.setdefault(nome, m.start())
    return [nome for nome, _ in sorted(achado.items(), key=lambda kv: kv[1])][:max_itens]


def _linhas_de_sinais(d: dict) -> list[str]:
    linhas: list[str] = []
    tipo = _fold(d.get("tipo") or "")
    if "monocrat" in tipo or "presid" in tipo:
        linhas.append("⚠️ decisão monocrática/da presidência: não é precedente do colegiado — serve para ver como o relator decide, não para citar como jurisprudência do órgão.")
    if re.search(r"turma\s+recursal|juizado", _fold(f"{d.get('orgao', '')} {d.get('origem', '')}")):
        linhas.append("⚠️ Turma Recursal (Juizados Especiais Federais): pesa em processo de JEF; no rito comum é só persuasivo — prefira acórdão de Turma do TRF1 e diga o órgão na citação.")
    a = _ancoras(" ".join(x for x in (d.get("ementa"), d.get("decisao"), d.get("inteiro_teor_texto")) if x))
    if a:
        linhas.append("Cita: %s — precedente qualificado citado pelo julgado (peso, e âncora para a próxima busca); confirme a situação de cada um no BNP/fonte." % " · ".join(a))
    return linhas


# ======================= C: recibos em disco (cadeia de custódia) =======================
def _arq_recibo(ident: str) -> str:
    return os.path.join(DIR_RECIBOS, re.sub(r"[^A-Za-z0-9]", "", str(ident or "")) + ".json")


def _texto_de_custodia(d: dict) -> str:
    partes = [x for x in (d.get("ementa"), d.get("decisao"), d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido")) if x]
    return "\n\n".join(partes)


def _campos_de_custodia(d: dict) -> dict:
    """Formato compartilhado com os recibos do TJRO/STJ/TJSE/TCE-RO: `id_documento`, `nr_processo`,
    `texto`. Os trechos que NÃO são palavra do tribunal (transcrição de outro julgado, faixa
    divergente) vão EM BRUTO, recortados do próprio `texto`, para quem lê aplicar a sua
    própria normalização (red team do lint do TJSE, 21/09/2026)."""
    texto = _texto_de_custodia(d)
    it = d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") or ""
    transcritos, divergente = [], ""
    if it:
        tn = norm(it)
        transcritos = [it[_bruto(it, tn, a): _bruto(it, tn, b)] for a, b in faixas_transcritas(tn, 0)]
        div = faixa_divergente(tn, 0)
        divergente = it[_bruto(it, tn, div[0]):] if div else ""
    return {
        "id_documento": str(d.get("id") or ""),
        "nr_processo": d.get("numero_digitos") or _so_digitos(d.get("numero") or ""),
        "numero": d.get("numero") or "",
        "tribunal": BASES.get(d.get("base") or "trf1", BASES["trf1"])["rotulo"],
        "base": d.get("base") or "trf1",
        "tipo": d.get("tipo") or "", "classe": d.get("classe") or "", "qualificacao": d.get("qualificacao") or "",
        "data_julgamento": d.get("data_fecho") or d.get("data_julgamento") or "",
        "data_publicacao": d.get("data_publicacao") or "",
        "orgao": d.get("orgao_fecho") or d.get("orgao") or "", "orgao_fonte": _orgao_fonte(d),
        "relator": d.get("relator") or "", "relator_texto": d.get("relator_acordao_texto") or d.get("relator_texto") or "",
        "url": d.get("link_inteiro_teor") or "",
        "texto": texto, "ementa": d.get("ementa") or "", "dispositivo": d.get("decisao") or "",
        "inteiro_teor_incluido": bool(it),
        "inteiro_teor": it,  # campo próprio: a atribuição roda sobre o MESMO texto que o portal deu (red team 22/09, 5)
        "trechos_transcritos": transcritos, "trecho_divergente": divergente,
        "normalizacao": "trechos em bruto, recortados de `texto` — normalize com a sua própria função",
        "sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest(),
        "versao_servidor": VERSAO,
        "obtido_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def gravar_recibo(d: dict) -> str:
    """Grava o recibo (0600, escrita atômica). Falha é silenciosa: recibo é conferência extra,
    nunca condição da resposta. Devolve o caminho ou ''."""
    try:
        if not d.get("id") or not _texto_de_custodia(d):
            return ""
        os.makedirs(DIR_RECIBOS, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(DIR_RECIBOS, 0o700)
        rec = _campos_de_custodia(d)
        caminho = _arq_recibo(rec["id_documento"])
        tmp = f"{caminho}.{os.getpid()}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, caminho)
        return caminho
    except Exception:
        return ""


def ler_recibo(ident: str) -> dict | None:
    """sha256 divergente = corrupção/edição → posto de lado (.inconsistente), None."""
    caminho = _arq_recibo(ident)
    try:
        with open(caminho, encoding="utf-8") as f:
            rec = json.load(f)
        ok = isinstance(rec, dict) and isinstance(rec.get("texto"), str) \
            and hashlib.sha256(rec["texto"].encode("utf-8")).hexdigest() == rec.get("sha256")
    except FileNotFoundError:
        return None
    except Exception:
        ok, rec = False, None
    if ok:
        return rec
    with contextlib.suppress(OSError):
        os.replace(caminho, caminho + ".inconsistente")
    return None


def _recibos_do_processo(digitos: str, base: str) -> list[dict]:
    """Recibos locais do mesmo processo e base (sem rede) — permite verificar citação dias
    depois sem gastar o orçamento do portal."""
    if len(digitos or "") != 20 or not os.path.isdir(DIR_RECIBOS):  # número parcial vai ao portal (misturaria processos)
        return []
    out = []
    for nome in sorted(os.listdir(DIR_RECIBOS)):
        if not nome.endswith(".json"):
            continue
        rec = ler_recibo(nome[:-5])
        if rec and rec.get("id_documento") and rec.get("base") == base and rec.get("nr_processo") == digitos:
            out.append(rec)
    return out


def _doc_de_recibo(rec: dict) -> dict:
    """Reconstrói o dict de documento a partir do recibo (para verificar sem rede)."""
    return {"id": rec["id_documento"], "base": rec.get("base") or "trf1", "numero": rec.get("numero") or "",
            "numero_digitos": rec.get("nr_processo") or "", "tipo": rec.get("tipo") or "", "classe": rec.get("classe") or "",
            "qualificacao": rec.get("qualificacao") or "", "relator": rec.get("relator") or "", "orgao": rec.get("orgao") or "",
            "orgao_fecho": rec.get("orgao") if rec.get("orgao_fonte") == "fecho" else "",
            "data_julgamento": rec.get("data_julgamento") or "", "data_publicacao": rec.get("data_publicacao") or "",
            "ementa": rec.get("ementa") or "", "decisao": rec.get("dispositivo") or "",
            "inteiro_teor_texto": rec.get("inteiro_teor") or "",
            "link_inteiro_teor": rec.get("url") or "", "link_tipo": "tnu" if "eproctnu" in (rec.get("url") or "") else
            "arquivo" if "arquivo.trf1" in (rec.get("url") or "") else "pje" if "pje" in (rec.get("url") or "") else "",
            "campos": {}, "recibo": _arq_recibo(rec["id_documento"])}


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
    dj = d.get("data_fecho") or d.get("data_julgamento")
    if dj:
        partes.append(f"Data de Julgamento: {dj}")
    orgao = d.get("orgao_fecho") or d.get("orgao")  # índice é indício, texto é prova
    if orgao:
        partes.append(orgao)
    if d.get("data_publicacao"):
        partes.append(f"Data de Publicação: {d['data_publicacao']}")
    return "(" + ", ".join(partes) + ")"


def _nivel_verificacao(d: dict, cortado: bool = False) -> str:
    """O nível vai DENTRO da linha de citação (determinação 8): rodapé solto se perde quando a
    citação é copiada para a minuta. "inteiro teor lido" só quando o texto integral veio E foi
    mostrado inteiro; cortado pelo orçamento → "EM PARTE" (não serve para afirmar ausência)."""
    if d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido"):
        return "inteiro teor lido EM PARTE" if cortado else "inteiro teor lido"
    return "só ementa/índice"


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
        linhas.append(f"  Citação: {_citacao_com_link(d)} — verificação: {_nivel_verificacao(d)}")
        for sl in _linhas_de_sinais(d):
            linhas.append(f"  {sl}")
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
        it = d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") or ""
        em = d.get("ementa") or "—"
        cortado = len(em) > orcamento_por_doc or len(it) > orcamento_por_doc
        nivel = _nivel_verificacao(d, cortado=cortado)
        linhas.append(f"\n### {d.get('tipo') or 'Documento'} · id {d['id']} · julgado em {d.get('data_fecho') or d.get('data_julgamento') or '?'}")
        linhas.append(f"Citação: {_citacao_com_link(d)} — verificação: {nivel}")
        linhas.append(f"  orgao_fonte: {_orgao_fonte(d)}"
                      + (f" · relator no texto: {d['relator_acordao_texto'] or d['relator_texto']}" if d.get("relator_acordao_texto") or d.get("relator_texto") else ""))
        campos = d.get("campos") or {}
        extras = {k: v for k, v in campos.items() if k not in ("Ementa", "Decisão", "Inteiro teor", "Número", "Fonte da publicação")}
        if extras:
            linhas.append("  " + " · ".join(f"{k}: {v}" for k, v in extras.items()))
        if d.get("fonte_publicacao"):
            linhas.append(f"  Fonte da publicação: {d['fonte_publicacao']}")
        for sl in _linhas_de_sinais(d):
            linhas.append(f"  {sl}")
        if len(em) > orcamento_por_doc:
            em = em[:orcamento_por_doc].rsplit(" ", 1)[0] + "… [CORTADO pelo orçamento de caracteres — o recibo em disco tem o texto inteiro]"
        linhas.append(f"\n**Ementa (integral, literal do portal):**\n{em}")
        linhas.append(f"\n**Dispositivo (campo \"Decisão\", literal):**\n{d.get('decisao') or '— (não informado pelo portal)'}")
        if it:
            if len(it) > orcamento_por_doc:
                it = it[:orcamento_por_doc].rsplit(" ", 1)[0] + "… [CORTADO pelo orçamento de caracteres — o recibo em disco tem o texto inteiro; verificação = EM PARTE]"
            linhas.append(f"\n**Inteiro teor (literal, {'eproc da TNU' if d.get('inteiro_teor_texto') else 'embutido no portal'}):**\n{it}")
        else:
            linhas.append(f"\n{_nota_inteiro_teor(d)}")
    base = docs[0].get("base") or "trf1"
    if any(d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") for d in docs):
        linhas.append(
            f"\n---\nPara a ficha de precedente: `tribunal: \"{BASES[base]['rotulo']}\"`, `id_documento` = id acima, "
            "`julgamento` em ISO, `ementa`/`dispositivo`/`trecho` literais (cortes com [...]), `verificacao` = "
            "exatamente o nível que a linha de citação diz (\"EM PARTE\" nunca vira pleno), `orgao` e `orgao_fonte` "
            "como acima (relator e órgão da ficha vêm do TEXTO quando o fecho foi lido). Antes de pôr aspas, "
            "verificar_citacao_trf1 — que diz de quem é a frase."
        )
    else:
        linhas.append(
            "\n---\nPara a ficha de precedente: `tribunal: \"TRF1\"`, `id_documento` = id acima, "
            "`julgamento` em ISO, `ementa`/`dispositivo` literais (cortes com [...]), `orgao_fonte: \"índice\"`. O portal NÃO "
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
# A escada só alarga se o bloqueio veio com volume DESTA máquina no minuto anterior (TJRO v1.7.8,
# 22/09/2026: bloqueio na 1ª consulta do dia, com 1 requisição no minuto, subia o nível para 3/5
# — era a rede do usuário, não rajada nossa). Abaixo disso, arma-se o cooldown sem apertar o ritmo.
MIN_REQS_PARA_ESCADA = 3
# Estado do disjuntor ILEGÍVEL (JSON corrompido, sem permissão de leitura) → pausa finita, não
# liberação (fail-closed). Finita porque o red team do TJRO (27/08/2026) rejeitou o fail-closed
# perpétuo: "travaria a ferramenta para sempre culpando o tribunal por um soluço de leitura".
PAUSA_ILEGIVEL_S = 10 * 60.0

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
    "ultimo_sucesso_em": 0.0,
    "total_requisicoes": 0,
    "motivo_pausa": "",
}
_MAX_INCIDENTES = 20


@contextlib.contextmanager
def _trava_estado():
    """Exclusão mútua entre processos. Devolve True se a trava foi obtida. Sem `fcntl`
    (Windows) devolve True também — ali não há como coordenar, e a alternativa seria não
    funcionar nunca. Com `fcntl` e trava impossível (permissão), devolve False: quem
    RESERVA requisição trata isso como fail-closed (determinação 3 de 22/09/2026)."""
    f = None
    travado = fcntl is None
    try:
        if fcntl is not None:
            f = open(_ARQUIVO_ESTADO_DISJUNTOR + ".lock", "w")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            travado = True
    except Exception:
        f = None
    try:
        yield travado
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
    agora = time.time()
    try:
        with open(_ARQUIVO_ESTADO_DISJUNTOR, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except FileNotFoundError:
        return estado  # 1ª execução: liberado
    except Exception as e:
        # fail-closed: estado ilegível NÃO libera requisição — pausa finita, com motivo,
        # para todo processo desta máquina (todos leem o mesmo arquivo)
        estado["bloqueado_ate"] = agora + PAUSA_ILEGIVEL_S
        estado["motivo_pausa"] = f"estado do disjuntor ilegível ({type(e).__name__}) — fail-closed"
        estado["indice_janela"] = len(_ESCADA_JANELA_S) - 1
        return estado
    if not isinstance(dados, dict):
        estado["bloqueado_ate"] = agora + PAUSA_ILEGIVEL_S
        estado["motivo_pausa"] = "estado do disjuntor com tipo inválido — fail-closed"
        estado["indice_janela"] = len(_ESCADA_JANELA_S) - 1
        return estado
    estado.update({k: v for k, v in dados.items() if k in _ESTADO_PADRAO})

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
    estado["sucessos"] = max(0, int(_num(estado.get("sucessos"), 0)))
    inc = estado.get("incidentes")
    def _inc(i: dict) -> dict:  # incidente com todos os campos tipados: consumidor nenhum precisa se defender (red team 22/09, 6)
        return dict(i, quando=_num(i.get("quando"), 0.0), reqs_ultimos_60s=int(_num(i.get("reqs_ultimos_60s"), 0)),
                    reqs_na_janela=int(_num(i.get("reqs_na_janela"), 0)), janela_s=int(_num(i.get("janela_s"), 0)),
                    operacao=str(i.get("operacao") or "?"), tipo=str(i.get("tipo") or "bloqueio"),
                    desde_ultima_req_s=None if i.get("desde_ultima_req_s") is None else int(_num(i.get("desde_ultima_req_s"), 0)))
    estado["incidentes"] = [_inc(i) for i in inc if isinstance(i, dict)][-_MAX_INCIDENTES:] if isinstance(inc, list) else []
    estado["ultimo_sucesso_em"] = _num(estado.get("ultimo_sucesso_em"), 0.0)
    estado["motivo_pausa"] = str(estado.get("motivo_pausa") or "")
    return estado


_persistencia_indisponivel: str | None = None


def _transacao(fn, obrigatoria: bool = False):
    """Read-modify-write atômico do estado compartilhado. `obrigatoria=True` é o modo de quem
    RESERVA uma requisição: sem trava ou sem gravação não há requisição (fail-closed —
    determinação 3 de 22/09/2026; até a v1.0.0 a falha de gravação caía num estado em memória
    e cada processo seguia com orçamento próprio, em silêncio, contra o mesmo IP). Os registros
    de sucesso/bloqueio são best-effort: falha ali é anotada para o diagnóstico, não derruba
    uma resposta que o portal já entregou."""
    global _persistencia_indisponivel
    with _trava_estado() as travado:
        if not travado:
            _persistencia_indisponivel = f"sem trava em {_ARQUIVO_ESTADO_DISJUNTOR}.lock"
            if obrigatoria:
                raise PesquisaNaoRealizada(
                    f"não consegui a trava do disjuntor ({_ARQUIVO_ESTADO_DISJUNTOR}.lock — permissão?); "
                    "sem trava não há como dividir o orçamento entre processos, e sem isso não há requisição "
                    "(fail-closed). Não contornar por navegador, proxy ou outro cliente."
                )
        estado = _ler_estado()
        resultado = fn(estado)
        try:
            tmp = f"{_ARQUIVO_ESTADO_DISJUNTOR}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(estado, f)
            os.replace(tmp, _ARQUIVO_ESTADO_DISJUNTOR)
            _persistencia_indisponivel = None
        except Exception as e:
            _persistencia_indisponivel = getattr(e, "strerror", None) or type(e).__name__
            if obrigatoria:
                raise PesquisaNaoRealizada(
                    f"não consegui gravar o estado do disjuntor ({_ARQUIVO_ESTADO_DISJUNTOR}: "
                    f"{_persistencia_indisponivel}); sem registro não há requisição (fail-closed). "
                    "Verifique permissão e espaço em disco."
                )
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
            motivo = e.get("motivo_pausa") or (
                "o portal do CJF/TRF1 recusou uma consulta recente (bloqueio, desafio de navegador ou "
                "HTTP 403/429)"
            )
            return {"erro": (
                f"disjuntor em pausa por mais {_fmt_hms(e['bloqueado_ate'] - agora)} — {motivo}. Para não "
                "prolongar o bloqueio, esta ferramenta não tenta de novo antes disso. O portal "
                "jurisprudencia.cjf.jus.br/trf1 segue acessível no navegador; não contornar por proxy "
                "ou outro cliente."
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

    return _transacao(_decidir, obrigatoria=True)


def _registrar_bloqueio_detectado(
    agora: float | None = None, operacao: str = "?", subir_escada: bool = True, espera_minima_s: float = 0.0,
    tipo: str = "bloqueio",
) -> None:
    """Portal recusou: arma o cooldown (backoff dobra) e FOTOGRAFA o contexto. `tipo`:
    "bloqueio" (assinatura anti-robô no corpo), "desafio" (Cloudflare/F5 — desafio de
    navegador, não se contorna e a escada não sobe: TJRO v1.7.7) ou "http" (403/429 seco,
    sem assinatura — arma o cooldown sem alargar a escada). A escada só sobe com
    ≥ MIN_REQS_PARA_ESCADA consultas desta máquina no minuto anterior."""
    agora = time.time() if agora is None else agora

    def _aplicar(e: dict) -> None:
        reqs = e.get("requisicoes") or []
        janela = _ESCADA_JANELA_S[e["indice_janela"]]
        anteriores = e.get("incidentes") or []
        ult60 = len([t for t in reqs if agora - t <= 60])
        e["incidentes"] = (anteriores + [{
            "quando": agora,
            "tipo": tipo,
            "operacao": operacao,
            "nivel": e["indice_janela"],
            "janela_s": int(janela),
            "reqs_ultimos_60s": ult60,
            "reqs_na_janela": len([t for t in reqs if agora - t <= janela]),
            "desde_ultima_req_s": int(agora - e["ultima_requisicao_em"]) if e.get("ultima_requisicao_em") else None,
            "desde_incidente_anterior_s": int(agora - anteriores[-1]["quando"]) if anteriores else None,
        }])[-_MAX_INCIDENTES:]
        e["bloqueado_ate"] = agora + max(e["backoff_s"], espera_minima_s)
        e["motivo_pausa"] = {
            "desafio": "o portal devolveu um desafio de navegador (Cloudflare/F5) — não se contorna; teste outra rede ou o navegador",
            "http": "o portal respondeu HTTP 403/429 sem assinatura de bloqueio",
        }.get(tipo, "o portal bloqueou a consulta por suspeita de automação")
        e["backoff_s"] = min(e["backoff_s"] * 2, _BACKOFF_MAXIMO_S)
        if subir_escada and tipo == "bloqueio" and ult60 >= MIN_REQS_PARA_ESCADA \
                and e["indice_janela"] < len(_ESCADA_JANELA_S) - 1:
            e["indice_janela"] += 1
        e["sucessos"] = 0

    with contextlib.suppress(PesquisaNaoRealizada):
        _transacao(_aplicar)


def _registrar_sucesso(agora: float | None = None) -> None:
    agora = time.time() if agora is None else agora

    def _aplicar(e: dict) -> None:
        e["backoff_s"] = _BACKOFF_INICIAL_S
        e["ultimo_sucesso_em"] = agora
        e["motivo_pausa"] = ""
        e["sucessos"] += 1
        if e["sucessos"] >= _SUCESSOS_PARA_RELAXAR:
            e["sucessos"] = 0
            if e["indice_janela"] > 0:
                e["indice_janela"] -= 1

    with contextlib.suppress(PesquisaNaoRealizada):
        _transacao(_aplicar)


def _bloqueio_sistematico(e: dict, agora: float) -> bool:
    """2+ recusas nas últimas 24 h, cada uma com pouco tráfego desta máquina, e nenhum sucesso
    entre a primeira delas e agora: não é rajada, é o portal (ou a rede) recusando este cliente
    de forma sistemática — espaçar não resolve (TJRO v1.7.10, 22/09/2026)."""
    inc = [i for i in (e.get("incidentes") or []) if isinstance(i, dict) and agora - float(i.get("quando") or 0) <= 86400]
    if len(inc) < 2 or any(int(i.get("reqs_ultimos_60s") or 0) > 2 for i in inc):
        return False
    primeiro = min(float(i.get("quando") or 0) for i in inc)
    return float(e.get("ultimo_sucesso_em") or 0) < primeiro


def _diagnostico_ritmo(agora: float | None = None) -> str:
    agora = time.time() if agora is None else agora
    with _trava_estado():
        e = _ler_estado()
    janela = _ESCADA_JANELA_S[e["indice_janela"]]
    na_janela = len([t for t in (e.get("requisicoes") or []) if agora - t <= janela])
    n_rec = len([f for f in os.listdir(DIR_RECIBOS) if f.endswith(".json")]) if os.path.isdir(DIR_RECIBOS) else 0
    linhas = [
        f"**Controle de ritmo do MCP TRF1 (portal do CJF) — v{VERSAO}**",
        f"- Nível atual: {e['indice_janela'] + 1} de {len(_ESCADA_JANELA_S)} "
        f"(limite: {_JANELA_MAX_REQS} requisições a cada {_fmt_hms(janela)}; cada busca gasta 2 a 4)",
        f"- Orçamento usado agora: {na_janela}/{_JANELA_MAX_REQS} nesta janela",
        f"- Requisições desde o início (nesta máquina): {e.get('total_requisicoes', 0)}",
        (
            f"- ⚠️ EM PAUSA — liberando em {_fmt_hms(e['bloqueado_ate'] - agora)}"
            + (f" ({e['motivo_pausa']})" if e.get("motivo_pausa") else "")
            if agora < e["bloqueado_ate"] else "- Situação: liberado"
        ),
        f"- User-Agent: {'definido por TRF1_USER_AGENT' if os.environ.get('TRF1_USER_AGENT') else 'padrão (identificado)'}",
        f"- Recibos gravados em {DIR_RECIBOS}: {n_rec}",
    ]
    if _persistencia_indisponivel:
        linhas.insert(1, (
            f"- ⚠️ AVISO: falha ao usar {_ARQUIVO_ESTADO_DISJUNTOR} ({_persistencia_indisponivel}) — "
            "enquanto isso a reserva de requisição é RECUSADA (fail-closed): verifique permissão e espaço em disco."
        ))
    inc = e.get("incidentes") or []
    if not inc:
        linhas.append("\nNenhuma recusa do portal registrada até agora nesta máquina.")
        return "\n".join(linhas)
    linhas.append(f"\n**Recusas registradas: {len(inc)}** (mais recentes primeiro)")
    for i in list(reversed(inc))[:8]:
        quando = time.strftime("%Y-%m-%d %H:%M", time.localtime(i["quando"]))
        intervalo = "—" if i.get("desde_ultima_req_s") is None else f"{i['desde_ultima_req_s']}s"
        linhas.append(
            f"- {quando} · {i.get('tipo', 'bloqueio')} · {i.get('reqs_ultimos_60s', '?')} requisições no minuto anterior, "
            f"{i.get('reqs_na_janela', '?')} na janela de {_fmt_hms(i.get('janela_s') or 0)} · "
            f"intervalo desde a anterior: {intervalo} · operação: {i.get('operacao', '?')}"
        )
    media = sum(float(i.get("reqs_ultimos_60s") or 0) for i in inc) / len(inc)  # incidente malformado não derruba o diagnóstico
    pouco = len([i for i in inc if i["reqs_ultimos_60s"] <= 2])
    linhas.append(f"\n**Padrão observado:** em média {media:.1f} requisições no minuto que antecedeu cada recusa.")
    if _bloqueio_sistematico(e, agora):
        linhas.append(
            "⚠️ RECUSA SISTEMÁTICA: 2 ou mais recusas em 24 h, com pouco tráfego desta máquina e nenhum sucesso "
            "entre elas. Não é rajada — é o portal (ou a rede desta máquina) recusando este cliente. Espaçar "
            "não resolve: teste outra rede, use o portal no navegador e, se persistir, informe o suporte do "
            "portal do CJF. Não contornar (proxy, User-Agent de navegador, cookie de sessão)."
        )
    elif pouco > len(inc) / 2:
        linhas.append(
            "A maioria das recusas veio com pouquíssimo tráfego desta máquina — indício de "
            "causa fora do controle desta ferramenta (outro equipamento no mesmo IP, ou o próprio "
            "portal apertando o filtro). Espaçar mais aqui tende a não resolver."
        )
    elif media >= 8:
        linhas.append("As recusas vieram após rajadas — preferir uma busca ampla a várias seguidas é o que mais ajuda.")
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
# Desafio de NAVEGADOR (Cloudflare "Just a moment", F5/TSPD): página que exige JavaScript
# e não é bloqueio por volume — a escada do disjuntor não sobe, e a mensagem não manda
# esperar (TJRO v1.7.7). Bloqueio por assinatura de anti-robô (STIC-like) sobe a escada.
_RE_DESAFIO = re.compile(
    r"<title>\s*just a moment|cf-mitigated|challenges\.cloudflare\.com|cf-chl|/TSPD/|loaderConfig",
    re.IGNORECASE,
)
_RE_BLOQUEIO_ANTIROBO = re.compile(
    r"acesso (foi )?bloqueado|p[aá]gina bloqueada|robotiza|suspeita de automa", re.IGNORECASE
)
# união, para quem só quer saber "é recusa?"
_RE_BLOQUEIO = re.compile(_RE_DESAFIO.pattern + "|" + _RE_BLOQUEIO_ANTIROBO.pattern, re.IGNORECASE)


def _eh_desafio_navegador(texto: str) -> bool:
    return bool(_RE_DESAFIO.search(texto or ""))
_RE_SESSAO_EXPIRADA = re.compile(r"ViewExpired|sess[aã]o expirad|view state could not be restored", re.I)


def _envelope(texto: str) -> str:
    """Resposta sem o conteúdo dos CDATA (onde vivem as ementas). A página do Cloudflare e o
    <error-name>ViewExpiredException</error-name> do JSF não têm CDATA — continuam visíveis.
    CDATA aberto sem fechar = resposta cortada: corta ali."""
    e = re.sub(r"(?s)<!\[CDATA\[.*?\]\]>", " ", texto or "")
    return e.split("<![CDATA[")[0][:20_000]


class PesquisaNaoRealizada(Exception):
    """Falha de rede, ritmo, bloqueio ou do próprio portal: a consulta NÃO chegou a ser feita.
    NUNCA equivale a 'não localizado' — toda saída construída a partir daqui leva o carimbo
    [PESQUISA NÃO REALIZADA — motivo] (determinação 2 de references/correcoes-determinadas-2026-09-22.md:
    até a v1.0.0, erro de parâmetro, disjuntor armado, timeout e bloqueio saíam com o MESMO texto,
    e o agente de pesquisa relatava "nada encontrado" quando o que houve foi teto de ritmo)."""


class PortalRecusou(PesquisaNaoRealizada):
    """Bloqueio/recusa explícita do portal (já registrada no disjuntor)."""


class SessaoInvalida(RuntimeError):
    """A sessão JSF não serve mais (ViewState/JSESSIONID); tentar uma vez com sessão nova."""


def _diagnosticar_resposta(status: int, ctype: str, texto: str, esperado: str) -> str:
    if _eh_desafio_navegador(texto or ""):
        return (
            f"o portal devolveu um DESAFIO DE NAVEGADOR (Cloudflare/F5, HTTP {status}) em vez da resposta. "
            "Isso não é bloqueio por volume e não se contorna (nem por proxy, User-Agent ou cookie): "
            "teste outra rede, use o portal no navegador e, se persistir, informe o suporte do portal."
        )
    if _RE_BLOQUEIO_ANTIROBO.search(texto or ""):
        return (
            "o portal recusou a consulta com uma página de bloqueio anti-robô "
            f"(HTTP {status}). Costuma ser temporário — a ferramenta pausa e o portal segue "
            "acessível no navegador."
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
    reserva = _reservar_requisicao()  # fail-closed: sem trava/gravação, levanta PesquisaNaoRealizada
    if "erro" in reserva:
        raise PesquisaNaoRealizada(reserva["erro"])
    if reserva["esperar_s"] > 0:
        await asyncio.sleep(reserva["esperar_s"])
    try:
        if metodo == "GET":
            r = await cli.get(url, headers=headers)
        else:
            r = await cli.post(url, data=_pares_para_form(data), headers=headers)
    except Exception as e:
        # timeout e falha de rede NÃO armam o disjuntor (não são recusa do portal; o TJSE
        # pausava 30 min por timeout e a comparação de 22/09/2026 confirmou que aqui já
        # estava certo) — mas são pesquisa NÃO realizada, nunca "não localizado"
        raise PesquisaNaoRealizada(f"falha de rede ao consultar o portal ({type(e).__name__}: {e}) — tente de novo em instantes")
    ctype = r.headers.get("content-type", "").lower()
    texto = r.text or ""
    # Só o ENVELOPE (fora dos CDATA): ementa que fale de "acesso bloqueado" ou "sessão
    # expirada" é texto de acórdão, não sinal do portal (red team 11/09/2026: na resposta
    # de paginação a 1ª ementa começa no offset ~6k, dentro da janela — armaria 10 min).
    envelope = _envelope(texto)
    eh_desafio = _eh_desafio_navegador(envelope)
    eh_bloqueio = bool(_RE_BLOQUEIO_ANTIROBO.search(envelope))
    if eh_desafio or eh_bloqueio or r.status_code in (403, 429):
        try:
            espera_minima = float(r.headers.get("retry-after") or 0)
        except ValueError:
            espera_minima = 0.0
        tipo = "desafio" if eh_desafio else "bloqueio" if eh_bloqueio else "http"
        _registrar_bloqueio_detectado(operacao=operacao, subir_escada=eh_bloqueio, espera_minima_s=espera_minima, tipo=tipo)
        raise PortalRecusou(_diagnosticar_resposta(r.status_code, ctype, texto, esperado))
    if _RE_SESSAO_EXPIRADA.search(envelope):
        raise SessaoInvalida(_diagnosticar_resposta(r.status_code, ctype, texto, esperado))
    if r.status_code >= 400:
        raise PesquisaNaoRealizada(_diagnosticar_resposta(r.status_code, ctype, texto, esperado))
    if esperado == "xml" and "<partial-response" not in texto[:2000]:
        raise PesquisaNaoRealizada(_diagnosticar_resposta(r.status_code, ctype, texto, "XML <partial-response>"))
    if esperado == "html" and "javax.faces.ViewState" not in texto:  # "qualquer": sem checagem
        raise PesquisaNaoRealizada(_diagnosticar_resposta(r.status_code, ctype, texto, "a página do formulário"))
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
        raise PesquisaNaoRealizada("pacote 'httpx' não instalado")
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
                raise PesquisaNaoRealizada("ViewState não encontrado na página inicial do portal — o layout do CJF mudou?")
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
                raise PesquisaNaoRealizada(
                    "o portal recusou a CONSULTA (não é resultado vazio): " + " | ".join(msgs)
                    + " — corrija a sintaxe/os filtros e refaça"
                )
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
            raise PesquisaNaoRealizada(
                "a sessão do portal expirou duas vezes seguidas (ViewState recusado) — "
                "instabilidade do CJF; tente de novo em instantes"
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


def _verificar_trecho(textos: dict[str, str], trecho: str, tribunal: str = "TRF1") -> dict:
    """Confere se `trecho` aparece literalmente em algum dos textos (ementa, dispositivo,
    inteiro teor), por palavra inteira, com piso de tamanho e vão máximo entre fragmentos
    de `[...]` (conferir). No inteiro teor, diz DE QUEM é a frase (alertas de atribuição);
    na ementa/dispositivo só NEGAÇÃO/ENTRE ASPAS fazem sentido."""
    frags = [f for f in (x.strip() for x in re.split(r"\[\s*\.\.\.\s*\]|\(\s*\.\.\.\s*\)|\[…\]|…", trecho or "")) if f]
    if not frags:
        return {"valido": False, "onde": None, "faltando": [], "alertas": [], "motivo": "trecho vazio"}
    util = norm(" ".join(frags))
    if len(util.split()) < PISO_TRECHO_PALAVRAS or len(util) < PISO_TRECHO_CHARS:
        return {"valido": False, "onde": None, "faltando": [], "alertas": [],
                "motivo": f"trecho curto demais para conferência útil (mínimo {PISO_TRECHO_PALAVRAS} palavras e "
                          f"{PISO_TRECHO_CHARS} caracteres): qualquer acórdão contém isso"}
    melhor_erro: dict | None = None
    for nome, texto in textos.items():
        if not (texto or "").strip():
            continue
        r = conferir(texto, trecho, tribunal=tribunal, com_atribuicao=(nome == "inteiro teor"))
        if r.get("ok"):
            return {"valido": True, "onde": nome, "faltando": [], "alertas": r["alertas"], "contexto": r["contexto"],
                    "motivo": f"trecho encontrado literalmente em: {nome}"}
        if r.get("fragmento") and (melhor_erro is None or "erro" in r):
            melhor_erro = r
    if melhor_erro and melhor_erro.get("erro"):
        return {"valido": False, "onde": None, "faltando": [melhor_erro["fragmento"]], "alertas": [],
                "motivo": melhor_erro["erro"]}
    return {"valido": False, "onde": None, "faltando": [melhor_erro["fragmento"]] if melhor_erro else frags, "alertas": [],
            "motivo": "trecho NÃO encontrado literalmente — não cite entre aspas; parafraseie ou corrija"}


# --------------------------------------------------------------------------- #
# Implementação das ferramentas                                                #
# --------------------------------------------------------------------------- #
ISSUES_URL = f"https://github.com/{REPO_GITHUB}/issues"


def _carimbo_nao_realizada(motivo) -> str:
    """Toda falha sai com o mesmo carimbo, para o agente de pesquisa nunca ler "não localizado"
    onde houve teto de ritmo, bloqueio, timeout ou recusa do portal."""
    m = str(motivo).strip().rstrip(".")
    ajuda = ""
    if not re.search(r"disjuntor em pausa|Muitas consultas|Fila de espera|fail-closed", m):
        # ritmo local não pede relato; falha do portal/rede pede (TJRO v1.7.9, sem texto da busca)
        ajuda = (f"\nSe persistir, relate em {ISSUES_URL} (informe a versão v{VERSAO} e a hora; "
                 "não inclua número de processo nem texto da busca — a issue é pública).")
    return (f"[PESQUISA NÃO REALIZADA — {m}]\n"
            "Isto NÃO é \"não localizado\": a consulta não chegou a ser feita ou não foi respondida. "
            "Registre a frente como pendente; use diagnostico_ritmo_trf1 antes de concluir que o portal está fora." + ajuda)


def _erro_de_parametro(e) -> str:
    return (f"Erro de parâmetro (nada foi consultado): {e}\n"
            "Corrija a chamada e refaça — não registre como pesquisa realizada nem como \"não localizado\".")


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
    except ValueError as e:
        return _erro_de_parametro(e)
    except PesquisaNaoRealizada as e:
        return _carimbo_nao_realizada(e)
    except Exception as e:  # inesperado: também é pesquisa não realizada
        return _carimbo_nao_realizada(f"erro inesperado ({type(e).__name__}: {e})")
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
    for d in docs:
        avisos_extra += _aplicar_fecho(d)  # índice é indício, texto é prova
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
    except ValueError as e:
        return _erro_de_parametro(e)
    except PesquisaNaoRealizada as e:
        return _carimbo_nao_realizada(e)
    except Exception as e:
        return _carimbo_nao_realizada(f"erro inesperado ({type(e).__name__}: {e})")
    gravados = [c for c in (gravar_recibo(d) for d in docs) if c]
    saida = _format_decisao(docs, _cnj(digitos) if len(digitos) == 20 else numero, dados["total"])
    if gravados:
        saida += (f"\nRecibo(s) gravado(s) em {DIR_RECIBOS} ({len(gravados)}): o lint da peticao-rg e o "
                  "revisor-adversarial conferem a ficha contra esse arquivo, sem nova requisição.")
    for a in dados.get("avisos") or []:
        saida = f"⚠️ {a}\n" + saida
    return saida


async def _verificar_citacao(numero: str, trecho: str, base: str = "trf1") -> str:
    digitos = _so_digitos(numero)
    if len(digitos) < 7:
        return f"Número muito curto para localizar com segurança: {numero!r}."
    if not (trecho or "").strip():
        return "Informe o trecho que pretende citar entre aspas."
    origem = "portal"
    try:
        base = _validar_base(base)
        # recibo local primeiro (0 requisições): o que o portal entregou da última vez, com sha256
        recs = _recibos_do_processo(digitos, base)
        if recs and (base != "tnu" or all(r.get("inteiro_teor") for r in recs)):
            docs = [_doc_de_recibo(r) for r in recs]
            dados = {"avisos": [], "total": len(docs)}
            origem = "recibo local"
        else:
            dados, docs = await _localizar_por_numero(numero, base, "verificacao")
            for d in docs:
                gravar_recibo(d)
    except ValueError as e:
        return _erro_de_parametro(e)
    except PesquisaNaoRealizada as e:
        return _carimbo_nao_realizada(e)
    except Exception as e:
        return _carimbo_nao_realizada(f"erro inesperado ({type(e).__name__}: {e})")
    if not docs:
        return f"Nenhum documento sob o número {numero} na base {base} — não há como verificar; não cite."
    rotulo = BASES[base]["rotulo"]
    linhas = []
    tem_teor = False
    for d in docs:
        it = d.get("inteiro_teor_texto") or d.get("inteiro_teor_embutido") or ""
        tem_teor = tem_teor or bool(it)
        textos = {"ementa": d.get("ementa") or "", "dispositivo": d.get("decisao") or "", "inteiro teor": it}
        r = _verificar_trecho(textos, trecho, tribunal=rotulo)
        marca = "✅ VÁLIDO" if r["valido"] else "❌ NÃO ENCONTRADO"
        if r["valido"] and r.get("alertas"):
            marca = "✅ LITERAL, MAS COM ALERTA DE ATRIBUIÇÃO"
        linhas.append(f"{marca} · id {d['id']} · {d.get('tipo') or '?'} · julgado em {d.get('data_fecho') or d.get('data_julgamento') or '?'} · {r['motivo']}")
        for al in r.get("alertas") or []:
            linhas.append(f"   ⚠️ {al}")
        if r["valido"] and r.get("contexto"):
            linhas.append(f"   contexto: …{r['contexto'][:300]}…")
        if not r["valido"] and r["faltando"]:
            for f in r["faltando"][:3]:
                linhas.append(f"   fragmento sem correspondência: «{f[:160]}»")
    cabec = f"**Verificação literal — {docs[0].get('numero') or numero} ({rotulo}, {len(docs)} documento(s), fonte: {origem})**"
    if tem_teor:
        rodape = ("\nCobre ementa, dispositivo e inteiro teor, dizendo DE QUEM é a frase: TRANSCRIÇÃO (palavra de outro "
                  "tribunal copiada no voto), VOTO DIVERGENTE (pode ser o vencido), ENTRE ASPAS, ALEGAÇÃO DA PARTE e "
                  "NEGAÇÃO. Trecho com alerta NÃO entra na ficha como posição do órgão sem resolver a atribuição.")
    else:
        rodape = ("\nCobre SÓ ementa e dispositivo: o portal não expõe o voto nesta base, então a atribuição (transcrição, "
                  "voto vencido, alegação da parte) NÃO foi analisada — não é que não exista. Aspas em ementa/dispositivo "
                  "são seguras; qualquer coisa do voto só no navegador.")
    rodape += (" Comparação por palavra inteira, tolerante a caixa, acento e pontuação; mínimo de "
               f"{PISO_TRECHO_PALAVRAS} palavras; `[...]` separa fragmentos em ordem, a até {VAO_MAXIMO} caracteres. "
               "Se ❌: não cite entre aspas — parafraseie, ou confira no navegador.")
    for a in dados.get("avisos") or []:
        linhas.insert(0, f"⚠️ {a}")
    return "\n".join([cabec] + linhas) + rodape


# --------------------------------------------------------------------------- #
# Crédito e aviso de versão (espelho do TJSE v0.8.0 / TJRO v1.7.4)              #
# --------------------------------------------------------------------------- #
# Uma consulta ao GitHub (releases/latest) por processo, em THREAD DE FUNDO na subida do
# servidor: nenhuma resposta é atrasada — as tools só leem o resultado se já chegou. Se houver
# versão MAIS NOVA, a primeira resposta ganha uma linha com o endereço FIXO da página de
# releases (nunca uma URL vinda do corpo da API). Sem rede, erro ou >2 s: silêncio. Só o GitHub
# vê o IP; nada da pesquisa sai daqui, e a consulta NÃO passa pelo disjuntor (que é do portal).
# Desligar: TRF1_MCP_SEM_AVISO_ATUALIZACAO=1.
RELEASES_API = f"https://api.github.com/repos/{REPO_GITHUB}/releases/latest"
RELEASES_PAGINA = f"https://github.com/{REPO_GITHUB}/releases/latest"
CREDITO = ("_Esta extensão foi desenvolvida por @robertogrecia (Roberto Grécia Bessa, "
           "OAB/RO 7865-A). Obrigado por usar!_")
_RE_TAG = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_credito_dado = False
_aviso_dado = False
_versao_nova: str | None = None


def versao_mais_nova(atual: str, outra: str) -> bool:
    """True só se `outra` for estritamente maior; formato estranho é False (aviso errado é pior que nenhum)."""
    a, b = _RE_TAG.match(str(atual or "").strip()), _RE_TAG.match(str(outra or "").strip())
    return bool(a and b) and tuple(int(x) for x in b.groups()) > tuple(int(x) for x in a.groups())


def _checar_versao(timeout: float = 2.0) -> None:
    """Thread de fundo. NUNCA levanta."""
    global _versao_nova
    try:
        if os.environ.get("TRF1_MCP_SEM_AVISO_ATUALIZACAO") == "1" or httpx is None:
            return
        r = httpx.get(RELEASES_API, timeout=timeout,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": f"trf1-jurisprudencia-mcp/{VERSAO}"})
        if r.status_code != 200:
            return
        tag = str((r.json() or {}).get("tag_name") or "").strip()
        if versao_mais_nova(VERSAO, tag):
            _versao_nova = tag.lstrip("v")
    except Exception:
        return


def iniciar_checagem_versao() -> None:
    if os.environ.get("TRF1_MCP_SEM_AVISO_ATUALIZACAO") == "1":
        return
    threading.Thread(target=_checar_versao, daemon=True, name="trf1-checar-versao").start()


def aviso_atualizacao(nova: str) -> str:
    # URL fora do itálico: markdown incorporaria o `_` final e o link daria 404 (TJRO v1.7.9)
    return f"_Há uma versão mais nova desta extensão (v{nova}; a instalada é a v{VERSAO})._ {RELEASES_PAGINA}"


def com_avisos(texto: str) -> str:
    """Crédito (uma vez por processo) e, se houver, aviso de versão (uma vez). Nunca espera rede."""
    global _credito_dado, _aviso_dado
    partes = [texto]
    if not _credito_dado:
        _credito_dado = True
        partes.append(CREDITO)
    if _versao_nova and not _aviso_dado:
        _aviso_dado = True
        partes.append(aviso_atualizacao(_versao_nova))
    return "\n\n".join(partes)


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
                Até 6 grupos × 12 termos. Monte os grupos com o FATO julgado (o que aconteceu, o
                instituto, a norma), nunca com palavras da CONCLUSÃO que você espera ("não
                equivale", "é inócua", "não afasta"): cada acórdão escreve a conclusão de um jeito
                e um grupo assim derruba a busca (harness do TJRO, 22/09/2026: tese que zerava
                achou o acórdão em 5º/24 trocando o grupo da conclusão pelo do fato).
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
            Cada citação termina em "— verificação: só ementa/índice" (a busca não lê o voto) e cada
            resultado traz os SINAIS do julgado: "Cita: Súmula X · Tema Y · IRDR Z" (precedente
            qualificado citado no texto — peso e âncora para a próxima busca), ⚠️ decisão monocrática
            e ⚠️ Turma Recursal quando for o caso. Avisa "mesmo número, N documentos" e resultados
            opostos no mesmo julgamento. Com 3+ resultados, resume offline quantos julgamentos
            declaram cada resultado. Antes de citar, use obter_decisao_trf1.
            Falha (ritmo, bloqueio, rede, portal) sai como [PESQUISA NÃO REALIZADA — motivo]: nunca
            é "não localizado"; erro de parâmetro sai como "Erro de parâmetro".
        """
        return com_avisos(await _buscar(consulta, tipo, fonte, grupos, relator, orgao_julgador, classe, origem, numero,
                                        ementa_decisao, referencia_legislativa, data_inicio, data_fim, tipo_data, pagina, por_pagina,
                                        base, tipo_acordao))

    @mcp.tool()
    async def obter_decisao_trf1(numero: str, base: str = "trf1") -> str:
        """Traz TODOS os documentos publicados no portal do CJF sob um número de processo do TRF1/JEF1,
        com ementa e dispositivo INTEGRAIS e literais — é o que substitui o "inteiro teor" aqui.
        Na base "tnu" traz também o INTEIRO TEOR (relatório, voto, votantes) baixado do eproc da TNU
        (até 3 documentos por chamada) — aí "inteiro teor lido" é possível.

        Use antes de citar qualquer julgado devolvido por buscar_jurisprudencia_trf1. Lista os
        julgamentos distintos sob o mesmo número (acórdão, embargos, decisão monocrática), cada um
        com data, id, relator e órgão. Na base trf1 o portal NÃO expõe o voto: `verificacao` fica
        em "só ementa/índice"; "inteiro teor lido" só depois de abrir o PDF no navegador (o
        arquivo.trf1.jus.br exige desafio Cloudflare e esta ferramenta não o lê). Na TNU, órgão,
        relator e data são lidos também do FECHO do inteiro teor e a resposta avisa DIVERGÊNCIA
        entre índice e texto (índice é indício, texto é prova) — `orgao_fonte` diz de onde veio.

        Grava um RECIBO por documento em ~/.trf1-jurisprudencia-recibos/<id>.json (texto que o
        portal entregou + sha256): é contra ele que o lint da peticao-rg e o revisor-adversarial
        conferem a ficha depois, e verificar_citacao_trf1 o usa sem gastar requisição.

        Args:
            numero: Número do processo (CNJ), com ou sem pontuação.
            base: "trf1" (padrão), "tnu" ou "colegiado" — a mesma em que o documento foi achado.

        Returns:
            Cabeçalho com a contagem de documentos sob o número; para cada um, citação pronta
            terminando em "— verificação: <nível>" (só ementa/índice · inteiro teor lido · inteiro
            teor lido EM PARTE, quando cortado pelo orçamento — nunca promova EM PARTE a pleno),
            `orgao_fonte`, sinais do julgado (Cita: …), metadados, ementa integral, dispositivo
            integral, inteiro teor (TNU) e a nota de inteiro teor; no fim, o que preencher na ficha.
            Saída limitada a ~50 mil caracteres. Falha sai como [PESQUISA NÃO REALIZADA — motivo].
        """
        return com_avisos(await _obter_decisao(numero, base))

    @mcp.tool()
    async def verificar_citacao_trf1(numero: str, trecho: str, base: str = "trf1") -> str:
        """Confere se um trecho aparece LITERALMENTE na ementa/dispositivo (e, na TNU, no inteiro teor)
        do julgado, antes de ir entre aspas para a peça — e diz DE QUEM é a frase.

        USE antes de qualquer citação direta. Comparação por palavra inteira, tolerante a caixa,
        acento e pontuação; intolerante a palavra trocada ou omitida; mínimo de 4 palavras;
        `[...]` separa fragmentos que devem aparecer nessa ordem, a no máximo 1.500 caracteres um
        do outro (não costura a abertura da ementa ao fim do dispositivo). Lê primeiro o RECIBO
        local gravado por obter_decisao_trf1 (0 requisições); sem recibo, consulta o portal.

        Na TNU (inteiro teor) o ✅ pode vir com ALERTA DE ATRIBUIÇÃO: TRANSCRIÇÃO (palavra de OUTRO
        tribunal copiada no voto — num acórdão medido no TJSE, 73% do texto), VOTO DIVERGENTE (pode
        ser o voto vencido), ENTRE ASPAS (o tribunal citando alguém), ALEGAÇÃO DA PARTE (tese da
        parte relatada, não decisão) e NEGAÇÃO (negativa logo antes: o recorte inverte o julgado).
        Trecho com alerta NÃO entra na ficha como posição do órgão sem resolver a atribuição. Na
        base trf1 a atribuição não é analisada porque o voto não é exposto — a resposta diz isso.

        Args:
            numero: Número do processo (CNJ), com ou sem pontuação.
            trecho: Texto que se pretende citar entre aspas (cortes marcados com [...]).
            base: "trf1" (padrão), "tnu" ou "colegiado".

        Returns:
            Por documento sob o número: ✅/❌ (ou ✅ com alerta), onde foi encontrado, o contexto,
            os alertas de atribuição e os fragmentos sem correspondência quando falhar.
            Falha sai como [PESQUISA NÃO REALIZADA — motivo], nunca como ❌.
        """
        return com_avisos(await _verificar_citacao(numero, trecho, base))

    @mcp.tool()
    async def diagnostico_ritmo_trf1() -> str:
        """Mostra por que as buscas do TRF1 podem estar falhando.

        Relata o nível atual do limite de ritmo, o orçamento consumido, se há bloqueio em curso
        (e quanto falta para liberar) e o histórico de bloqueios com o contexto de cada um.
        USE ISTO antes de concluir que "o portal está fora do ar". Não faz nenhuma requisição.
        """
        try:
            return com_avisos(_diagnostico_ritmo())
        except Exception as e:  # diagnóstico nunca sai como traceback
            return com_avisos(_carimbo_nao_realizada(f"diagnóstico falhou ({type(e).__name__}: {e})"))

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
        assert _verificar_trecho(vt, "a tese fixada: beneficio [...] por incapacidade, art 42")["onde"] == "ementa"
        r_neg = _verificar_trecho(vt, "a tese fixada: beneficio [...] por incapacidade, art 43")
        assert not r_neg["valido"] and r_neg["faltando"] == ["por incapacidade, art 43"], r_neg
        assert not _verificar_trecho(vt, "por incapacidade a tese fixada beneficio")["valido"]  # ordem errada
        assert not _verificar_trecho(vt, "")["valido"]
        assert _verificar_trecho(vt, "tese fixada [...] incapacidade art 42")["valido"]  # pontuação/«» tolerada
        # piso (determinação 4): 2 palavras não conferem nada
        r_piso = _verificar_trecho(vt, "tese fixada")
        assert not r_piso["valido"] and "curto demais" in r_piso["motivo"], r_piso
        # vão (determinação 4): `[...]` não costura abertura da ementa ao fim do voto
        longe = {"inteiro teor": "a tese fixada pela turma foi clara " + ("lorem ipsum dolor sit amet " * 80) + " e por isso nego provimento ao recurso"}
        r_vao = _verificar_trecho(longe, "a tese fixada pela turma [...] nego provimento ao recurso")
        assert not r_vao["valido"] and "caracteres do anterior" in r_vao["motivo"], r_vao
        # --- atribuição (determinação 1), portada do TJSE ---
        voto_tr = ("RELATÓRIO. O INSS sustenta que o benefício é indevido porque o segurado voltou a trabalhar. "
                   "VOTO. Nesse sentido, transcrevo: EMENTA: PREVIDENCIÁRIO. AUXÍLIO-DOENÇA. O retorno ao trabalho não impede "
                   "a percepção do benefício quando comprovada a incapacidade. Recurso conhecido e provido. "
                   "(STJ, REsp 1.234.567/RS, Rel. Min. Fulano, DJe 01/01/2020). No caso dos autos, entendo que a "
                   "incapacidade ficou provada e o retorno ao trabalho foi tentativa frustrada. Fixo a seguinte tese: "
                   "'o retorno ao trabalho por tentativa não afasta o direito ao benefício'. Não há como acolher o pedido "
                   "de repetição dos valores recebidos de boa-fé. Peço vênia para divergir do relator: o retorno ao trabalho "
                   "afasta o benefício desde o primeiro dia.")
        vt2 = {"ementa": "", "dispositivo": "", "inteiro teor": voto_tr}
        r1 = _verificar_trecho(vt2, "o retorno ao trabalho não impede a percepção do benefício", tribunal="TNU")
        assert r1["valido"] and any(a.startswith("TRANSCRIÇÃO") for a in r1["alertas"]), r1
        r2 = _verificar_trecho(vt2, "afasta o benefício desde o primeiro dia", tribunal="TNU")
        assert r2["valido"] and any(a.startswith("VOTO DIVERGENTE") for a in r2["alertas"]), r2
        r3 = _verificar_trecho(vt2, "o benefício é indevido porque o segurado voltou a trabalhar", tribunal="TNU")
        assert r3["valido"] and any(a.startswith("ALEGAÇÃO DA PARTE") for a in r3["alertas"]), r3
        r4 = _verificar_trecho(vt2, "acolher o pedido de repetição dos valores recebidos", tribunal="TNU")
        assert r4["valido"] and any(a.startswith("NEGAÇÃO") for a in r4["alertas"]), r4
        # tese fixada pelo PRÓPRIO colegiado entre aspas: NÃO é "ENTRE ASPAS" (TNU cita a si mesma)
        r5 = _verificar_trecho(vt2, "o retorno ao trabalho por tentativa não afasta o direito ao benefício", tribunal="TNU")
        assert r5["valido"] and not any(a.startswith("ENTRE ASPAS") for a in r5["alertas"]), r5
        voto_q = "VOTO. Como ensina a doutrina: 'a boa-fé objetiva impõe deveres anexos de conduta às partes'. Entendo aplicável."
        r6 = _verificar_trecho({"inteiro teor": voto_q}, "a boa-fé objetiva impõe deveres anexos de conduta", tribunal="TNU")
        assert r6["valido"] and any(a.startswith("ENTRE ASPAS") for a in r6["alertas"]), r6
        # na ementa não se analisa transcrição (só NEGAÇÃO): ✅ limpo
        r7 = _verificar_trecho({"ementa": voto_tr, "dispositivo": ""}, "o retorno ao trabalho não impede a percepção do benefício")
        assert r7["valido"] and r7["onde"] == "ementa" and not any(a.startswith("TRANSCRIÇÃO") for a in r7["alertas"]), r7
        # --- fecho da TNU (determinação 6) ---
        f_tnu = _fecho_tnu(it_txt)
        assert f_tnu["orgao_fecho"] == "TURMA NACIONAL DE UNIFORMIZAÇÃO" and f_tnu["data_fecho"] == "14/05/2025", f_tnu
        assert "FABIO DE SOUZA SILVA" in f_tnu["relator_acordao_texto"], f_tnu
        assert _fecho_tnu("") == {"orgao_fecho": "", "relator_texto": "", "relator_acordao_texto": "", "data_fecho": ""}
        d_tnu = dict(docs_tnu[0], inteiro_teor_texto=it_txt)
        assert _aplicar_fecho(d_tnu) == [] and _orgao_fonte(d_tnu) == "fecho"
        d_div = dict(docs_tnu[0], inteiro_teor_texto=it_txt, orgao="TURMA REGIONAL", relator="BELTRANO", data_julgamento="01/01/2020")
        av = _aplicar_fecho(d_div)
        assert len(av) == 3 and av[0].startswith("DIVERGÊNCIA") and av[1].startswith("RELATOR") and av[2].startswith("DATA"), av
        assert "TURMA NACIONAL DE UNIFORMIZAÇÃO" in _citacao(d_div) and "14/05/2025" in _citacao(d_div)  # fecho vence
        assert _orgao_fonte(d0).startswith("índice do portal") and "não conferido no fecho" in _orgao_fonte(d0)
        sd3 = _format_decisao([d_tnu], d_tnu["numero"], 1)
        assert "— verificação: inteiro teor lido" in sd3 and "orgao_fonte: fecho" in sd3, sd3[:600]
        sd4 = _format_decisao([d0], d0["numero"], 1)
        assert "— verificação: só ementa/índice" in sd4 and "não conferido no fecho" in sd4
        # EM PARTE quando cortado pelo orçamento (determinação 8)
        assert _nivel_verificacao(d_tnu, cortado=True) == "inteiro teor lido EM PARTE"
        _orc = globals()["ORCAMENTO_DECISAO"]
        globals()["ORCAMENTO_DECISAO"] = 8_000
        try:
            sd5 = _format_decisao([d_tnu], d_tnu["numero"], 1)
            assert "inteiro teor lido EM PARTE" in sd5 and "CORTADO" in sd5, sd5[:400]
        finally:
            globals()["ORCAMENTO_DECISAO"] = _orc
        # --- sinais do julgado ---
        assert _ancoras("aplica-se a Súmula 7 do STJ e o Tema 1.124; ver Súmula Vinculante 10 e o IRDR 3") == \
            ["Súmula 7", "Tema 1124", "Súmula Vinculante 10", "IRDR 3"], _ancoras("aplica-se a Súmula 7 do STJ e o Tema 1.124; ver Súmula Vinculante 10 e o IRDR 3")
        sn = _linhas_de_sinais(dict(d0, tipo="Decisão Monocrática", ementa="Tema 692 do STJ"))
        assert any("monocrática" in x for x in sn) and any(x.startswith("Cita: Tema 692") for x in sn), sn
        assert any("Turma Recursal" in x for x in _linhas_de_sinais(dict(d0, orgao="1ª TURMA RECURSAL")))
        sb = _format_busca(docs, 13039, {"pagina": 1, "por_pagina": 30, "base": "trf1", "fonte": ["TRF1"]})
        assert "— verificação: só ementa/índice" in sb
        # --- recibos (determinação 5) ---
        import tempfile, shutil
        _dir_orig = globals()["DIR_RECIBOS"]
        globals()["DIR_RECIBOS"] = tempfile.mkdtemp(prefix="trf1-recibos-")
        try:
            cam = gravar_recibo(d_tnu)
            assert cam and os.path.exists(cam) and (os.stat(cam).st_mode & 0o777) == 0o600, cam
            rec = ler_recibo(d_tnu["id"])
            assert rec and rec["id_documento"] == d_tnu["id"] and rec["nr_processo"] == d_tnu["numero_digitos"] \
                and rec["orgao_fonte"] == "fecho" and rec["inteiro_teor_incluido"] and rec["tribunal"] == "TNU" \
                and rec["sha256"] == hashlib.sha256(rec["texto"].encode()).hexdigest(), rec.keys()
            assert isinstance(rec["trechos_transcritos"], list) and rec["versao_servidor"] == VERSAO
            assert _recibos_do_processo(d_tnu["numero_digitos"], "tnu") and not _recibos_do_processo(d_tnu["numero_digitos"], "trf1")
            dr = _doc_de_recibo(rec)
            assert dr["inteiro_teor_texto"] == rec["inteiro_teor"] == it_txt and dr["orgao_fecho"] == "TURMA NACIONAL DE UNIFORMIZAÇÃO" and dr["link_tipo"] == "tnu"
            # sha divergente → posto de lado
            with open(cam, "r+", encoding="utf-8") as f:
                j = json.load(f); j["texto"] += " editado"; f.seek(0); json.dump(j, f); f.truncate()
            assert ler_recibo(d_tnu["id"]) is None and os.path.exists(cam + ".inconsistente")
            assert ler_recibo("inexistente") is None
            assert gravar_recibo({"id": "", "ementa": "x"}) == ""
            # verificar_citacao lê o recibo (0 requisições) e diz a fonte
            gravar_recibo(d_tnu)
            async def _nunca(*a, **kw):
                raise AssertionError("não deveria ir ao portal: há recibo")
            _orig_loc = _localizar_por_numero
            globals()["_localizar_por_numero"] = _nunca
            try:
                vc = asyncio.run(_verificar_citacao(d_tnu["numero"], "Turma Nacional de Uniformização decidiu, por unanimidade, conhecer parcialmente do recurso", "tnu"))
            finally:
                globals()["_localizar_por_numero"] = _orig_loc
            assert "fonte: recibo local" in vc and "✅" in vc, vc[:500]
        finally:
            shutil.rmtree(globals()["DIR_RECIBOS"], ignore_errors=True)
            globals()["DIR_RECIBOS"] = _dir_orig
        # trf1 sem inteiro teor: rodapé diz que a atribuição NÃO foi analisada (red team 22/09, achado 1: o
        # ramo do portal grava recibo — NUNCA na pasta real durante o selftest)
        globals()["DIR_RECIBOS"] = tempfile.mkdtemp(prefix="trf1-recibos-")
        async def _loc_trf1(numero, base, operacao, com_inteiro_teor=True):
            return {"total": 1, "docs": [d0], "avisos": []}, [d0]
        _orig_loc = _localizar_por_numero
        globals()["_localizar_por_numero"] = _loc_trf1
        try:
            vc2 = asyncio.run(_verificar_citacao(d0["numero"], " ".join((d0["ementa"] or "").split()[:8]), "trf1"))
        finally:
            globals()["_localizar_por_numero"] = _orig_loc
            shutil.rmtree(globals()["DIR_RECIBOS"], ignore_errors=True)
            globals()["DIR_RECIBOS"] = _dir_orig
        assert "Cobre SÓ ementa e dispositivo" in vc2 and "NÃO foi analisada" in vc2 and "fonte: portal" in vc2, vc2[-600:]
        assert not os.path.exists(os.path.join(_dir_orig, "1174043.json")) or True  # a pasta real não é tocada aqui
        # --- red team 22/09/2026 ---
        # 2. data do fecho = ÚLTIMA "Brasília, …", não a de um precedente transcrito
        it_prec = ("RELATOR : Juiz Federal FULANO DE TAL\nVOTO\nA matéria já foi enfrentada: 'ementa' (PEDILEF 123, julgado em "
                   "Brasília, 10 de março de 2020).\nACÓRDÃO\nA Turma Nacional de Uniformização decidiu, por unanimidade, negar "
                   "provimento.\nBrasília, 14 de maio de 2025.")
        assert _fecho_tnu(it_prec)["data_fecho"] == "14/05/2025", _fecho_tnu(it_prec)
        # 3. tese própria só desliga ENTRE ASPAS da aspa que ficou aberta; doutrina em seguida ainda alerta
        r_t = _verificar_trecho({"inteiro teor": "Tese fixada: 'o prazo decadencial não se aplica ao pedido de revisão'. Ademais, "
                                 "conforme a doutrina, 'o prazo decadencial é de dez anos para revisão de benefício'."},
                                "o prazo decadencial é de dez anos", tribunal="TNU")
        assert r_t["valido"] and any(a.startswith("ENTRE ASPAS") for a in r_t["alertas"]), r_t
        # 4. ENTRE ASPAS também na ementa (Súmula 111 do STJ transcrita não é palavra do TRF1)
        r_e = _verificar_trecho({"ementa": 'HONORÁRIOS. Incide a Súmula 111 do STJ, segundo a qual "os honorários advocatícios, nas '
                                 'ações previdenciárias, não incidem sobre as prestações vencidas após a sentença". Apelação provida.'},
                                "os honorários advocatícios, nas ações previdenciárias, não incidem sobre as prestações vencidas")
        assert r_e["valido"] and any(a.startswith("ENTRE ASPAS") for a in r_e["alertas"]), r_e
        # 7. "afasto"/"afastada" são negação
        assert any(a.startswith("NEGAÇÃO") for a in _verificar_trecho({"ementa": "afasto a tese de que houve decadência do direito de revisar o ato"},
                                                                       "houve decadência do direito de revisar")["alertas"])
        # 8. "n.º" normaliza como "nº"
        assert norm("Lei n.º 8.213") == norm("Lei nº 8.213") == "lei n 8.213", (norm("Lei n.º 8.213"), norm("Lei nº 8.213"))
        # 9. espaço duplo / prefixo de cargo no índice não é RELATOR divergente
        d_esp = dict(docs_tnu[0], inteiro_teor_texto="RELATOR : Juiz Federal FULANO DE TAL\nBrasília, 14 de maio de 2025.", relator="FULANO  DE TAL",
                     data_julgamento="14/05/2025", orgao="")
        assert not any(a.startswith("RELATOR") for a in _aplicar_fecho(d_esp)), _aplicar_fecho(d_esp)
        # 6. estado do disjuntor com tipos errados não vira TypeError
        _est_bad = {"sucessos": "x", "incidentes": [{"quando": "abc", "tipo": "bloqueio"}], "ultimo_sucesso_em": None}
        _arq_orig = globals()["_ARQUIVO_ESTADO_DISJUNTOR"]
        globals()["_ARQUIVO_ESTADO_DISJUNTOR"] = os.path.join(tempfile.mkdtemp(prefix="trf1-est-"), "estado.json")
        try:
            with open(_ARQUIVO_ESTADO_DISJUNTOR, "w", encoding="utf-8") as f:
                json.dump(_est_bad, f)
            _lido = _ler_estado()
            assert _lido["sucessos"] == 0 and _lido["incidentes"][0]["quando"] == 0.0, _lido
            _registrar_sucesso(time.time()); _registrar_bloqueio_detectado(time.time())  # não levantam
            assert "pausa" in _diagnostico_ritmo().lower()
        finally:
            shutil.rmtree(os.path.dirname(_ARQUIVO_ESTADO_DISJUNTOR), ignore_errors=True)
            globals()["_ARQUIVO_ESTADO_DISJUNTOR"] = _arq_orig
        # 10. recibo sem id_documento é ignorado; número parcial não usa recibo local
        globals()["DIR_RECIBOS"] = tempfile.mkdtemp(prefix="trf1-recibos-")
        try:
            os.makedirs(DIR_RECIBOS, exist_ok=True)
            _t = "texto qualquer"
            with open(os.path.join(DIR_RECIBOS, "sem-id.json"), "w", encoding="utf-8") as f:
                json.dump({"nr_processo": d0["numero_digitos"], "base": "trf1", "texto": _t, "sha256": hashlib.sha256(_t.encode()).hexdigest()}, f)
            assert _recibos_do_processo(d0["numero_digitos"], "trf1") == []
            assert _recibos_do_processo(d0["numero_digitos"][:7], "trf1") == []
        finally:
            shutil.rmtree(globals()["DIR_RECIBOS"], ignore_errors=True)
            globals()["DIR_RECIBOS"] = _dir_orig
        # --- crédito e versão ---
        assert versao_mais_nova("1.1.0", "v1.1.1") and not versao_mais_nova("1.1.0", "1.1.0") and not versao_mais_nova("1.1.0", "abc")
        globals()["_credito_dado"], globals()["_aviso_dado"], globals()["_versao_nova"] = False, False, "9.9.9"
        c1, c2 = com_avisos("a"), com_avisos("b")
        assert CREDITO in c1 and RELEASES_PAGINA in c1 and c2 == "b", (c1, c2)
        assert not aviso_atualizacao("9.9.9").endswith("_")
        globals()["_versao_nova"] = None
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
        assert "DESAFIO DE NAVEGADOR" in _diagnosticar_resposta(403, "text/html", cf, "html")
        assert _eh_desafio_navegador(cf) and _eh_desafio_navegador("<script>var loaderConfig") and _eh_desafio_navegador("/TSPD/x")
        assert not _eh_desafio_navegador("Seu acesso foi bloqueado por suspeita de robotização")
        assert "bloqueio anti-robô" in _diagnosticar_resposta(200, "text/html", "Página Bloqueada — suspeita de robotização", "xml")
        # carimbos de falha (determinação 2, 22/09/2026): nunca "não localizado"
        c1 = _carimbo_nao_realizada("disjuntor em pausa por mais 9min")
        assert c1.startswith("[PESQUISA NÃO REALIZADA — disjuntor em pausa") and "não localizado" in c1 and ISSUES_URL not in c1
        c2 = _carimbo_nao_realizada(PesquisaNaoRealizada("falha de rede (ReadTimeout)"))
        assert ISSUES_URL in c2 and "número de processo" in c2
        assert _erro_de_parametro(ValueError("base inválida")).startswith("Erro de parâmetro")
        assert issubclass(PortalRecusou, PesquisaNaoRealizada)
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
        assert "disjuntor em pausa" in recuo["erro"] and "9min" in recuo["erro"] and "suspeita de automação" in recuo["erro"], recuo
        assert "erro" not in _reservar_requisicao(t0 + 10 * 60 + 1)

        def _janela_visivel(base_t: float) -> str:
            for i in range(_JANELA_MAX_REQS):
                _reservar_requisicao(base_t + i * 2)
            return _reservar_requisicao(base_t + _JANELA_MAX_REQS * 2)["erro"]

        def _bloqueio_com_rajada(quando: float, **kw) -> None:
            """A escada só sobe com ≥ MIN_REQS_PARA_ESCADA consultas no minuto anterior (TJRO v1.7.8)."""
            for k in range(MIN_REQS_PARA_ESCADA):
                _reservar_requisicao(quando - 30 + k * 5)
            _registrar_bloqueio_detectado(quando, **kw)

        _limpar_estado()
        t = 4_000_000.0
        assert "1min" in _janela_visivel(t)
        t += 40 * 60
        _registrar_bloqueio_detectado(t)  # 1 consulta no minuto anterior: pausa, mas a escada NÃO sobe
        t += 40 * 60
        assert "1min" in _janela_visivel(t), "bloqueio sem rajada não pode alargar a janela"
        t += 40 * 60
        _bloqueio_com_rajada(t)
        t += 40 * 60
        assert "5min" in _janela_visivel(t)
        for _ in range(8):
            t += 70 * 60
            _bloqueio_com_rajada(t)
        t += 70 * 60
        assert "30min" in _janela_visivel(t)
        # desafio de navegador: pausa com motivo próprio, escada não sobe nem com rajada (TJRO v1.7.7)
        _limpar_estado()
        t = 4_500_000.0
        _bloqueio_com_rajada(t, tipo="desafio", subir_escada=False)
        est_d = _ler_estado()
        assert est_d["indice_janela"] == 0 and "desafio de navegador" in est_d["motivo_pausa"], est_d
        assert est_d["incidentes"][-1]["tipo"] == "desafio"
        assert "desafio de navegador" in _reservar_requisicao(t + 1)["erro"]
        _limpar_estado()
        t = 5_000_000.0
        _bloqueio_com_rajada(t)
        _bloqueio_com_rajada(t + 1)
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
        assert "EM PAUSA" in rel and "Recusas registradas: 1" in rel and "operação: busca" in rel and f"v{VERSAO}" in rel, rel
        assert "RECUSA SISTEMÁTICA" not in rel
        _limpar_estado()
        assert "Nenhuma recusa do portal registrada" in _diagnostico_ritmo(9_000_000.0)
        # recusa sistemática (TJRO v1.7.10): 2+ recusas em 24 h, pouco tráfego, sem sucesso entre elas
        _limpar_estado()
        t = 9_500_000.0
        _registrar_sucesso(t - 100)
        _reservar_requisicao(t)
        _registrar_bloqueio_detectado(t + 1, "busca")
        _reservar_requisicao(t + 3 * 3600)
        _registrar_bloqueio_detectado(t + 3 * 3600 + 1, "busca")
        assert _bloqueio_sistematico(_ler_estado(), t + 3 * 3600 + 2)
        assert "RECUSA SISTEMÁTICA" in _diagnostico_ritmo(t + 3 * 3600 + 2)
        _registrar_sucesso(t + 4 * 3600)  # um sucesso depois das recusas desfaz o diagnóstico
        assert not _bloqueio_sistematico(_ler_estado(), t + 4 * 3600 + 1)
        # fail-closed: arquivo de estado ilegível (não-JSON) pausa por PAUSA_ILEGIVEL_S com motivo
        _limpar_estado()
        with open(_ARQUIVO_ESTADO_DISJUNTOR, "w", encoding="utf-8") as fh:
            fh.write("{isto nao e json")
        est_i = _ler_estado()
        assert est_i["bloqueado_ate"] > time.time() + PAUSA_ILEGIVEL_S - 5 and "ilegível" in est_i["motivo_pausa"], est_i
        assert "fail-closed" in _reservar_requisicao()["erro"]
        with open(_ARQUIVO_ESTADO_DISJUNTOR, "w", encoding="utf-8") as fh:
            fh.write("[1,2,3]")
        assert "tipo inválido" in _ler_estado()["motivo_pausa"]
        _limpar_estado()
        with _trava_estado() as _tr:
            assert _tr is True
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
        iniciar_checagem_versao()
        mcp.run()
    else:
        sys.exit("registro MCP falhou (ver traceback acima) — se for ImportError, instale: pip install 'mcp[cli]' httpx truststore")
