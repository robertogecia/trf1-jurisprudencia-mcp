# Arquitetura do MCP TJRO — o que copiar, adaptar ou descartar para o TRF1

Fonte lida na íntegra: `/Users/robertogrecia/MCP/tjro-jurisprudencia/servidor_tjro.py` (2.089 linhas)
e `/Users/robertogrecia/MCP/tjro-jurisprudencia/README.md`. Todas as referências de linha
abaixo são desse arquivo, salvo indicação contrária.

Diferença estrutural que domina toda a leitura: o TJRO fala HTTP+JSON puro (POST →
JSON de resposta, Elasticsearch cru). O TRF1 é um app **JSF/PrimeFaces com estado
de sessão** — cookies + `javax.faces.ViewState` (um token que muda a cada
requisição), resposta em XML `<partial-response>` com HTML dentro de `CDATA`. Isso
não é um detalhe de parsing: muda a forma de **quatro** das sete seções abaixo
(camada HTTP, disjuntor pode ficar quase igual, mas a "sessão" é um conceito novo
que o TJRO nem tem; montagem de consulta troca de sintaxe inteira; formatação de
saída troca de "ler campos de um `_source`" para "extrair de HTML").

---

## 1. Esqueleto MCP

- **Dependências declaradas inline (PEP 723, `uv run` script)**: `L1-L4` — nenhum
  `requirements.txt`, o próprio arquivo carrega `mcp[cli]>=1.4.0`, `httpx>=0.27`,
  `truststore>=0.9`. Copiar tal qual, trocando só o nome do arquivo no comentário
  de topo.
- **Docstring de módulo**: `L5-L19` — descreve o que o servidor expõe (as duas
  tools) e o backend real (URL + formato de resposta). Para o TRF1, adaptar a
  descrição do backend (JSF/ViewState/XML) mas manter o formato "o que expõe" /
  "backend (engenharia reversa)".
- **Imports**: `L20-L32` (stdlib: asyncio, contextlib, html, json, os, re, sys,
  time, unicodedata, urllib.parse). `L34-L37`: `fcntl` (POSIX, ausente no
  Windows) — só é usado pelo disjuntor (seção 3), copiável tal qual.
  `L41-L46`: injeção do `truststore` no SSL (comentário explica: resolve proxy
  TLS de rede que rejeitaria certificado). `L48-L51`: import defensivo de
  `httpx` (permite importar o módulo sem a lib instalada, útil para o
  `--selftest` offline). Todo este bloco é genérico — copiar tal qual.
- **Como a tool é declarada**: bloco `try/except` em torno de todo o registro
  (`L1404-L1557`), para permitir importar o módulo em teste mesmo sem o pacote
  `mcp` instalado. Instanciação do servidor: `L1407`
  (`mcp = FastMCP("Jurisprudência TJRO")` — troque só o nome). Decorator
  `@mcp.tool()` em cada função async; o schema JSON dos parâmetros **não é
  escrito à mão** — o FastMCP gera a partir da assinatura Python tipada
  (`str`, `list[str] | None`, `int | None`, `bool`, defaults) e da docstring
  (Google-style, com `Args:`/`Returns:`) vira a descrição exposta ao modelo.
  Flag de sucesso do registro: `L1554` (`_HAS_MCP = True`); fallback:
  `L1556-L1557`.

  Trecho literal da declaração de uma tool (schema completo, `buscar_jurisprudencia_tjro`,
  `L1409-L1511`):

  ```python
      @mcp.tool()
      async def buscar_jurisprudencia_tjro(
          consulta: str,
          tipo: list[str] | None = None,
          grau: int | None = None,
          classe_judicial: str | None = None,
          orgao_colegiado: str | None = None,
          relator: str | None = None,
          grupos: list[list[str]] | None = None,
          assunto: str | None = None,
          data_inicio: str | None = None,
          data_fim: str | None = None,
          nr_processo: str | None = None,
          termo_exato: bool = False,
          ordenacao: str = "relevantes",
          pagina: int = 1,
          por_pagina: int = 10,
      ) -> str:
          """Pesquisa jurisprudência do Tribunal de Justiça de Rondônia (TJRO) no portal público JURIS.
          ...
          """
          return await _buscar(...)
  ```
  (docstring completa em `L1427-L1507` — vale ler inteira antes de escrever a
  do TRF1, é o "ensino ao modelo" tratado na seção 7).

- **`main()` / subida em stdio**: não há função `main()` separada — tudo vive no
  bloco `if __name__ == "__main__":` (`L1560-L2089`). Três ramos:
  `--selftest` em `sys.argv` (`L1561-L2085`, todo offline + 1 chamada de rede
  no fim), `elif _HAS_MCP: mcp.run()` (`L2086-L2087`, transporte stdio — é
  isso que o Claude Desktop/Code fala com o processo), `else: sys.exit(...)`
  se o pacote `mcp` não estiver instalado (`L2088-L2089`). Copiável tal qual —
  é o único ponto de entrada e não depende de nada específico do TJRO.
- **`--selftest`**: dividido em duas fases nitidamente separadas dentro do
  mesmo bloco. Fase 1 (offline, `L1568-L2074`): monta corpos de requisição e
  confere os campos (`_build_busca_body`, `_montar_grupos`, `_termo_para_query`),
  testa formatação (`_format_busca`, `_format_inteiro`, `_citacao`, `_link`),
  testa o disjuntor inteiro manipulando um arquivo de estado temporário
  (`L1620-L1776`, ver seção 3), testa extração de resultado/câmara/relator do
  texto (`L1784-L1927`). Tudo com `assert` simples — sem framework de teste,
  sem mocks de rede: `_RespostaFake` é uma classe mínima só com `.text` e
  `.headers` (`L1603-L1606`). Fase 2 (online, `L2077-L2085`, dentro de
  `async def _run()`): duas chamadas reais ao portal (`_buscar` e `_inteiro`),
  cujo resultado só é impresso — nenhum `assert`, porque a resposta de uma API
  externa muda. É invocado só via `python servidor_tjro.py --selftest`, nunca
  automaticamente. Para o TRF1: a fase offline (regressões de parsing/escaping/
  disjuntor) é o modelo a seguir; a fase online vai precisar simular sessão
  (cookie + ViewState) antes de bater na API de verdade — não dá para ser tão
  direta quanto aqui.

## 2. Camada HTTP (`_post`)

- **Cliente**: `httpx.AsyncClient(timeout=45.0)` criado a cada chamada dentro
  de `_post`, `L1281`. Não há client persistente entre chamadas — genérico,
  copiável, embora o TRF1 provavelmente precise de um client que **preserve
  cookies entre requisições** (sessão JSF), o que muda esse padrão: convém
  reusar um único `httpx.AsyncClient(cookies=...)` de vida mais longa em vez
  de recriar por chamada, ou passar explicitamente o cookie jar salvo em
  disco/memória a cada `post`.
- **Headers específicos do TJRO**: `HEADERS` em `L69-L79` — `Origin`/`Referer`
  apontando para `SITE`, `Content-Type: application/json`, `User-Agent` de
  navegador real, `Accept: application/json, text/plain, */*`. O comentário em
  `L60-L68` é uma decisão pessoal registrada por escrito: o WAF (STIC) bloqueia
  UA que não pareça navegador, e o autor documenta que essa escolha É consciente
  e NÃO deve ser replicada sem decisão própria em pacotes distribuídos a
  terceiros. **Isso é específico do TJRO só no valor (`Content-Type:
  application/json`); a ideia de "UA de navegador real + Origin/Referer do
  site" é genérica e quase certamente necessária para o TRF1 também** — mas o
  `Content-Type`/`Accept` do TRF1 será `application/x-www-form-urlencoded` (ou
  multipart) na ida e `text/xml` na volta, isso SIM é específico e precisa
  trocar. Vale replicar o aviso de decisão consciente no novo arquivo, adaptado
  ao TRF1.
- **truststore/TLS**: `L41-L46` — genérico, copiar tal qual.
- **Ler o corpo UMA vez e decidir por status/content-type**: núcleo em
  `L1281-L1310`. Comentário em `L1283-L1286` explica a ordem deliberada:
  inspecionar o corpo **antes** de `raise_for_status()`, porque se
  `raise_for_status()` rodasse primeiro um bloqueio 403/429 pularia a detecção
  de "página bloqueada" e o disjuntor nunca aprenderia com ele. Código:
  `ctype = r.headers.get("content-type", "").lower()` (`L1287`);
  `texto = r.text if (r.status_code >= 400 or "json" not in ctype) else ""`
  (`L1288`, só lê o texto quando pode ser preciso — evita custo de decodificar
  corpo grande à toa quando já é JSON válido); detecção de bloqueio por regex
  no texto (`L1289-L1291`); tratamento de 403/429 (`L1292`); só depois
  `r.raise_for_status()` (`L1305`); só depois checagem de content-type OK
  (`L1306-L1307`); só then `_registrar_sucesso()` e `r.json()` (`L1308-L1309`).
  **Esse desenho (ler corpo cru primeiro, decidir depois) é 100% genérico e
  deveria ser copiado tal qual para o TRF1** — só que em vez de `r.json()`, o
  TRF1 vai extrair o XML e depois o HTML do `CDATA` (ver seção 5).
- **Detecção de bloqueio (assinaturas STIC)**: regex compartilhada entre
  `_diagnosticar_resposta_nao_json` (`L902`) e `_post` (`L1290`):
  `r'robotiza|p[aá]gina bloqueada|\bstic\b'`. **Específico do TJRO** — o TRF1
  não tem STIC; será preciso descobrir (por teste real) qual página de erro,
  captcha ou redirecionamento o WAF do TRF1 devolve (ex.: página de login
  forçada, mensagem JSF de sessão expirada, HTTP 500 do PrimeFaces) e trocar a
  regex — mas a ARQUITETURA "regex de assinatura + mensagem própria ao invés
  de erro cru" é reaproveitável.
- **403/429 e `Retry-After`**: `L1292-L1303` — trata os dois tipos de bloqueio
  (WAF disfarçado de 200-com-HTML vs. status HTTP explícito) de forma
  diferenciada: `eh_bloqueio` (WAF/robotização) sobe a escada do disjuntor,
  um 403/429 puro **não** sobe a escada sozinho (`subir_escada=eh_bloqueio`,
  `L1301`) — ver seção 3. `Retry-After` vira piso mínimo do cooldown
  (`L1296-L1298`, `espera_minima`). Genérico, copiável tal qual — o JSF do
  TRF1 pode devolver 403 de sessão expirada, que é um caso diferente de "WAF
  bloqueou por robotização", então vale manter a distinção.
- **`_diagnosticar_resposta_nao_json`**: `L896-L913` — mensagem legível ao
  modelo (e por extensão ao advogado) quando a resposta 200 não é JSON.
  Genérica na estrutura (regex de assinatura → mensagem 1; senão, mensagem
  genérica de "resposta inesperada, tente depois" com o `content-type`
  observado, `L909-L913`). Para o TRF1 o equivalente seria "resposta não é
  XML `<partial-response>` válido" ou "resposta não contém o `CDATA`
  esperado" — mesma estrutura, condição de disparo diferente.

## 3. Disjuntor / limitador de ritmo em arquivo

Praticamente todo este bloco (`L916-L1265`) é escrito para ser genérico —
o comentário de cabeçalho (`L916-L928`) já explica por que vive em arquivo e
não em memória: múltiplos processos do mesmo servidor MCP (Claude Desktop +
sessões do Claude Code) sob o MESMO IP precisam de um orçamento
**compartilhado**, senão cada processo conta sozinho e a soma estoura o
limite do tribunal.

**Veredito: copiável quase sem mudança — só trocando nomes de arquivo e,
opcionalmente, as constantes numéricas** (o TRF1 pode tolerar ritmo diferente
do TJRO; ninguém sabe até testar). Nenhuma linha deste bloco depende de JSON,
Elasticsearch ou do vocabulário do TJRO.

Funções e constantes, com linhas:

| Nome | Linhas | O que faz |
|---|---|---|
| `_JANELA_MAX_REQS` | `L929` | teto de requisições por janela (10) |
| `_ESCADA_JANELA_S` | `L931-L932` | escada de janelas em segundos (1/5/10/20/30 min) |
| `_SUCESSOS_PARA_RELAXAR` | `L933` | nº de sucessos seguidos para descer 1 degrau |
| `_BACKOFF_INICIAL_S` / `_BACKOFF_MAXIMO_S` | `L934-L935` | cooldown de bloqueio: 10 min inicial, dobra a cada novo bloqueio, teto 1h |
| `_ESPACAMENTO_MIN_S` / `_ESPERA_MAXIMA_S` | `L940-L941` | espaçamento mínimo 2s entre requisições; acima de 30s de fila, erro em vez de travar |
| `_ARQUIVO_ESTADO_DISJUNTOR` | `L951-L953` | path do JSON de estado, ao lado do próprio script — **é a linha a trocar por servidor** (ex. `.disjuntor_estado_trf1.json`) |
| `_ESTADO_PADRAO` | `L955-L969` | schema do estado (requisições, próximo horário livre, bloqueado_até, índice da escada, sucessos, backoff, incidentes, contadores) |
| `_MAX_INCIDENTES` | `L971` | histórico curto (20) só para diagnosticar padrão |
| `_trava_estado()` | `L974-L996` | context manager de `fcntl.flock` exclusivo entre processos; se a trava falhar, segue sem ela (degradação consciente) |
| `_ler_estado()` | `L999-L1034` | lê o JSON, aplica **saneamento de relógio** (NTP/fuso, valores absurdos) descrito em `L1017-L1021`: uma margem que soma `_ESPERA_MAXIMA_S + _ESPACAMENTO_MIN_S` domina tanto o caso "relógio andou para frente" quanto "arquivo corrompido com valor absurdo" |
| `_estado_memoria` / `_persistencia_indisponivel` | `L1041-L1042` | **fallback em memória**: se o disco não aceitar escrita (permissão, `ENOSPC`), o estado passa a viver só neste processo — degradado, nunca desligado |
| `_transacao(fn)` | `L1045-L1069` | read-modify-write atômico: relê SEMPRE o disco mais recente (nunca cache de processo — evitaria corrida "last write wins"), escreve em arquivo temporário + `os.replace` (atômico no mesmo volume) |
| `_fmt_hms` | `L1072-L1080` | formata segundos em `Xh`, `Xmin`, `Xs` para mensagem ao usuário |
| `_reservar_requisicao()` | `L1083-L1120` | **ponto único de admissão**: bloqueio + orçamento + espaçamento numa transação só (evita duas chamadas concorrentes passarem juntas); devolve `{"esperar_s": x}` ou `{"erro": msg}` |
| `_registrar_bloqueio_detectado()` | `L1123-L1161` | ativa cooldown, dobra backoff, sobe a escada (exceto quando `subir_escada=False`), **fotografa o contexto do incidente** (quantas reqs no minuto anterior, na janela, tempo desde a última requisição e desde o incidente anterior) — é o "diário de bordo" citado no enunciado |
| `_registrar_sucesso()` | `L1164-L1175` | zera backoff reativo, conta sucessos consecutivos para relaxar a escada |
| `_diagnostico_ritmo()` | `L1178-L1240` | relatório legível (nível atual, orçamento usado, se está bloqueado, histórico de incidentes, leitura de padrão "rajada nossa vs. causa externa" comparando tráfego no minuto anterior a cada bloqueio) |
| cache de consultas idênticas | `L1243-L1264` (`_cache_ler`/`_cache_gravar`) | TTL de 5 min, no máx. 32 entradas em memória — evita 2ª requisição para a MESMA busca na mesma conversa |
| tool `diagnostico_ritmo_tjro` | `L1540-L1552` | expõe `_diagnostico_ritmo()` como ferramenta MCP, sem fazer nenhuma requisição de rede |

Nota de manutenção registrada no próprio código (`L943-L950`): o estado deste
script **não é compartilhado** com a versão Node/.mcpb do mesmo projeto — cada
implementação tem seu próprio orçamento; rodar duas ao mesmo tempo na mesma
máquina soma dois orçamentos independentes perante o mesmo IP. Vale o mesmo
aviso para o TRF1 se um dia existir uma segunda implementação dele.

Testes deste bloco no `--selftest`: `L1620-L1776` cobrem espaçamento mínimo,
teto de janela, orçamento compartilhado entre "processos" (reler o arquivo sem
resetar), bloqueio faz todos recuarem, escada sobe a cada bloqueio até o teto
de 30 min, sucessos relaxam 1 degrau (nunca abaixo do nível 0), e regressões de
red-team (`L1738-L1776`): saneamento de estado corrompido, escrita atômica sem
`.tmp` residual, 403/429 seco não alarga a janela mas `Retry-After` vira piso.
Esse conjunto de testes é praticamente um checklist pronto para copiar e
adaptar ao nome do disjuntor do TRF1.

## 4. Montagem de consulta

Este bloco é o mais dependente do motor de busca de baixo nível e por isso o
que **menos** se aporta ao TRF1 tal qual, mas a parte de validação/tetos é
reaproveitável como padrão de projeto.

- **`_LUCENE`** (`L116`): regex de caracteres especiais do **Elasticsearch/
  Lucene** a escapar. **Não se aplica ao TRF1.** O motor do TRF1 usa outra
  sintaxe (operadores `e`/`ou`/`adj`/`não`/`prox`/`mesmo`/`com`/`$`, aspas para
  frase exata) — precisa de uma tabela de escape própria, com os caracteres
  especiais DESSA sintaxe (aspas, e possivelmente os próprios operadores como
  palavras reservadas).
- **`_escape()`** (`L121-L131`): escapa Lucene mas preserva curinga final
  (`consign*`) — a ideia de "curinga final preservado, curinga inicial
  bloqueado (mais lento no servidor, vetor de sobrecarga)" é uma decisão de
  produto reaproveitável; a sintaxe do curinga do TRF1 pode ser diferente
  (`$` em vez de `*`, conforme o enunciado) e precisa ser conferida contra a
  documentação/comportamento real do motor do TRF1 antes de portar.
- **Motivação de `grupos` (comentário `L134-L144`)**: por que agrupar sinônimos
  em vez de um termo só (teste real: +37% de resultados) — a motivação e o
  desenho ("consulta livre escapa parênteses e aspas por segurança, então o
  agrupamento é montado programaticamente, nunca a partir de sintaxe crua")
  são **lógica de produto reaproveitável**, mesmo que a sintaxe final mude.
- **Tetos** `GRUPOS_MAX = 6`, `TERMOS_POR_GRUPO_MAX = 12`, `TERMO_MAX_CHARS = 80`
  (`L145-L147`): reaproveitáveis como está (ou recalibrados por teste real
  contra o TRF1).
- **`_termo_para_query()`** (`L151-L169`): normaliza um termo — trim, colapsa
  espaços, trunca, decide se vira frase entre aspas (expressão com espaço),
  escapa palavra reservada isolada (`AND`/`OR`/`NOT` maiúsculas viram operador
  no Lucene — no TRF1 o equivalente seria `E`/`OU`/`NÃO`/`ADJ`/`PROX`/`MESMO`/
  `COM` sozinhos, cujo escape precisa da MESMA lógica, sintaxe diferente:
  provavelmente aspas em volta do termo, a checar). **A ideia** "termo
  isolado que colide com uma palavra reservada precisa virar frase exata para
  não virar operador" é a parte lógica que se porta; a lista de palavras
  reservadas (`_RESERVADAS`, `L148`) precisa ser reescrita para o vocabulário
  do TRF1.
- **`_montar_grupos()`** (`L172-L187`): grupos em OR entre parênteses, grupos
  somados por AND, dedupe preservando ordem, grupo vazio descartado, entrada
  que não é lista devolve `""`. **Lógica 100% reaproveitável** — só a sintaxe
  de OR/AND muda de "OR"/"AND" (Lucene) para "OU"/"E" (motor do TRF1, a
  confirmar a grafia exata e se aceita minúsculas).
- **`termo_exato`**: aplicado em `_build_busca_body`, `L513-L514` — comentário
  explica a ORDEM (escapar antes de aspear, senão as aspas da frase exata
  virariam `\"` literais e a API trataria como busca solta). Essa ARMADILHA
  de ordem é o tipo de coisa que vale re-testar manualmente contra o TRF1
  antes de assumir que a mesma ordem funciona — sintaxes diferentes podem
  exigir ordem inversa.
- **`assunto`** (`L545-L553`) e **`relator`** (`L530-L544`): comentários
  registram investigação empírica real contra o portal do TJRO (datas e
  números de teste) para decidir se o filtro é server-side, se exige
  `.raw` (campo Elasticsearch sem análise), se exige maiúsculas. **Isso é
  Elasticsearch-específico na implementação (`.raw`), mas o MÉTODO —
  descobrir empiricamente, documentar a data e o resultado do teste, e
  registrar "não force uppercase, já vimos caso misto" — é o padrão de
  investigação a repetir para o TRF1** (provavelmente via formulário JSF com
  campos nomeados, não filtro Elasticsearch).
- **`_build_busca_body()`** (`L494-L567`): monta o corpo JSON da requisição
  Elasticsearch. **Não se aplica ao TRF1 na forma** (o TRF1 não envia JSON,
  envia um POST `application/x-www-form-urlencoded` com o `ViewState` e os
  nomes de campo do formulário JSF/PrimeFaces) — mas a FUNÇÃO como unidade
  pura testável (sem rede, `L1568-L2059` no `--selftest`) é o padrão a manter:
  ter uma função `_build_busca_form(...)` equivalente, que devolve o dict de
  campos do formulário, testável offline.

## 5. Formatação de saída

Toda a formatação vive em `_format_busca` (`L584-L787`) e `_format_inteiro`
(`L790-L890`), apoiadas por várias funções pequenas. Tabela item a item do
enunciado:

| Item | Linhas | Portável ao TRF1? |
|---|---|---|
| Cabeçalho com total de documentos | `L611-L614` | Sim, na estrutura — o TRF1 devolve HTML de tabela/paginação, então "total" vem de outro lugar do parsing, mas a mensagem-modelo (`"**N documento(s)** ... · tipo: ... · ordenação: ... · página N"`) é reaproveitável |
| Aviso de OR contando documentos separadamente | `L607-L610` | Sim, texto genérico — vale a mesma explicação se o motor do TRF1 também for por palavra |
| "Id. do documento" (chave única, não o nº do processo) | `L690` (uso), conceito em `L286-L293` (`_id_documento`) | A NECESSIDADE é maior ainda no TRF1 se o portal também aninhar várias decisões sob o mesmo nº — mas o "id" em si terá que vir de outro campo do HTML (não há `id_processo_documento` de Elasticsearch); confirmar se o HTML do TRF1 expõe algum id estável por decisão |
| Linha "Citação:" pronta no padrão forense | `_citacao()` `L251-L274`, uso em `L694` | Sim — a lógica de montar "(TR - Classe: nº CNJ, Relator, Data de Julgamento, Órgão, Data de Publicação)" com segmentos ausentes omitidos é genérica; só troca `TJ-RO` pelo rótulo do TRF1 e os nomes de campo de origem (que virão de parsing de HTML, não de um dict `_source`) |
| Rótulo "Ementa (trecho)" vs. "Trecho do TIPO" | `L678-L685` | Sim, lógica genérica (varia o artigo "da"/"do" conforme o tipo de peça) — precisa dos tipos de peça do TRF1 (o vocabulário de `TIPOS_VALIDOS`, `L82-L90`, é específico do TJRO e deve ser levantado de novo) |
| Aviso de corte em 800 chars | `_limpar()` `L198-L209` (trunca), aviso agregado `L737-L743` | Sim, tal qual — é só string handling; útil do mesmo jeito para HTML extraído do TRF1 |
| "Mesmo número, N documentos" | `L697-L714` (agrupamento por `_chave_proc`, `L652-L655`) | Sim na lógica; depende de conseguir extrair um "número de processo" normalizado do HTML do TRF1 para servir de chave de agrupamento |
| Detecção de resultado oposto (provável voto vencido) | `_resultado_de()` `L322-L341`, `_OPOSTOS` `L303-L306`, uso em `L715-L730` | A ARQUITETURA (regex sobre a cauda do texto, pares opostos independentes, nunca força lado quando ambíguo) é reaproveitável; os REGEXES (`L310-L319`) foram calibrados por red-team especificamente no vocabulário de acórdãos do TJRO — precisam de nova calibração e novo red-team no vocabulário do TRF1 (que pode usar outros verbos/fórmulas) |
| Resumo/jurimetria no rodapé (contagem por julgamento) | `_resumo_resultados_pagina()` `L359-L391`, uso `L745-L770` | Lógica de dedupe por (processo+data) e "ambíguo nunca escolhe lado" é reaproveitável; depende dos mesmos regexes de resultado acima |
| Dica automática quando total > 5000 | `L615-L623` | Sim, tal qual — troca só o texto sugerido (grupos/AND) se a sintaxe mudar |
| Mensagem de zero resultado ensinando grupos/âncora | `L625-L647`, sugestões "você quis dizer" via `_sugestoes()` `L462-L491` | A mensagem-modelo e a ideia de "âncore por súmula/tema" são portáveis; `_sugestoes()` depende do campo `suggest` do Elasticsearch (correção ortográfica) — **não existe equivalente automático no TRF1** a menos que o próprio PrimeFaces devolva sugestão, o que é improvável; provavelmente vira **não se aplica**, remover a chamada |
| Cache de consultas idênticas | `L1243-L1264`, integração em `_post` `L1270-L1273` | Sim, tal qual — mas cuidado: com sessão/ViewState, a MESMA consulta pode exigir um ViewState novo a cada tentativa; cachear a RESPOSTA formatada continua válido, só a chave de cache não pode incluir o ViewState (que muda sempre) |
| Linha de crédito | não encontrada | **Não existe no `servidor_tjro.py`** (busquei `crédito`/`credito`/`gerado por`/`fonte:` no arquivo inteiro e não há tal linha) — se o TRF1 quiser essa linha, é decisão nova, não herdada do TJRO |

## 6. Inteiro teor

- **`obter_inteiro_teor_tjro`** (tool, `L1514-L1538`) chama `_inteiro()`
  (`L1389-L1398`), que valida o número do processo, normaliza `tipo` (padrão
  `["ACÓRDÃO","EMENTA","VOTO","RELATÓRIO"]`, `L1392-L1393`), constrói o corpo
  via `_build_inteiro_body()` (`L570-L581` — filtra só por `nr_processo` e
  `tipo`, sem `query` de texto) e formata com `_format_inteiro()`.
- **`_format_inteiro()`** (`L790-L890`):
  - **Dedupe de peças**: mesma peça pode vir duplicada com ids distintos e
    texto idêntico — dedupe por `(tipo, corpo_limpo)` (`L797-L807`).
    Reaproveitável na lógica; o "corpo" do TRF1 virá de HTML dentro de
    `CDATA`, então o dedupe precisa comparar o HTML já limpo/normalizado, não
    o XML cru (o XML tem timestamps/ids de componente que mudam a cada
    resposta mesmo para o "mesmo" conteúdo).
  - **Orçamento de ~50k chars** (`ORCAMENTO_INTEIRO`, `L113`): teto GLOBAL da
    resposta; teto POR PEÇA é dinâmico — `max(15_000, ORCAMENTO_INTEIRO //
    len(unicos))` (`L845-L847`), permitindo que uma única peça grande (acórdão
    de ~80k chars) use o orçamento inteiro quando há poucas peças. Genérico,
    copiável tal qual.
  - **Cabeçalho por peça**: tipo, data de julgamento, relator, id
    (`L849-L857`) — genérico na estrutura; o "id" de novo depende do que o
    HTML do TRF1 realmente expuser.
  - **Aviso câmara/relator do índice vs. cabeçalho do texto**
    (`L864-L877`, funções `_extrair_orgao_do_texto` `L409-L416` e
    `_extrair_relator_do_texto` `L431-L440`): compara o que o **cadastro**
    (campo do Elasticsearch) diz contra o que o **próprio texto do acórdão**
    declara no cabeçalho, e avisa quando divergem (regra: "índice é indício,
    texto é prova", comentário em `L394-L397`). Esse padrão de checagem
    cruzada É especialmente valioso de portar — mas no TRF1 talvez não exista
    um "cadastro" separado do "texto" se tudo vier do mesmo HTML; nesse caso
    a checagem perde sentido (não se aplica) a menos que o TRF1 também exiba
    metadados de listagem separados do texto do inteiro teor.
- **Dependência específica da API JSON do TJRO**: absolutamente tudo que lê
  campos como `s.get("ds_modelo_documento")`, `s.get("nome_relator_acordao")`,
  `s.get("dtjulgamento_str")` etc. depende do schema do `_source` do
  Elasticsearch do TJRO. No TRF1 esses mesmos dados terão que ser extraídos
  por parsing de HTML (provavelmente com regex ou um parser HTML leve sobre o
  conteúdo do `CDATA`) — reescrever essa camada de extração é o grosso do
  trabalho novo, mesmo reaproveitando a arquitetura em volta.

## 7. Convenções gerais

- **Nomes em português** em 100% do código (funções, variáveis, docstrings,
  mensagens) — `_escape`, `_montar_grupos`, `_diagnostico_ritmo`,
  `buscar_jurisprudencia_tjro` etc. Convenção a manter: `buscar_jurisprudencia_trf1`,
  `obter_inteiro_teor_trf1`, `diagnostico_ritmo_trf1`.
- **Docstrings Google-style com `Args:`/`Returns:`** nas tools (ex.
  `L1427-L1507`) — viram o schema exposto ao modelo; mensagens de erro sempre
  em prosa completa dirigida ao usuário final (advogado), nunca stack trace
  cru: ex. `"Informe a consulta ou pelo menos um grupo de termos."` (`L1355`),
  `f"Erro ao consultar o TJRO ({type(e).__name__}): {e}"` (`L1382`, `L1397`).
- **Como a descrição da tool ensina o modelo a pesquisar** — a docstring de
  `buscar_jurisprudencia_tjro` não é só um schema, é um manual de uso
  compacto: explica que a busca casa PALAVRAS (não sentido), ensina a técnica
  de `grupos` de sinônimos com o resultado de um teste real (+37%), ensina a
  técnica de ancorar por súmula/tema, e ensina o "ciclo de pesquisa" (buscar →
  abrir inteiro teor do mais certeiro → colher vocabulário para refinar).
  Trecho literal (parte inicial da docstring, `L1427-L1445`):

  ```
          """Pesquisa jurisprudência do Tribunal de Justiça de Rondônia (TJRO) no portal público JURIS.

          Cobre ~4 milhões de documentos (ementas, acórdãos, sentenças, votos) de 1º e 2º grau.
          Fonte dos precedentes LOCAIS de Rondônia, de todos os períodos.

          A busca casa PALAVRAS, não sentido, e julgados do mesmo assunto usam vocabulários
          diferentes ("negativação", "inscrição indevida", "cadastro de inadimplentes") — monte
          a busca com `grupos`: cada grupo é uma lista de sinônimos ou expressões equivalentes
          (combinados por OR) e os grupos se somam por AND (e somam por AND à consulta, se
          houver); ex.: grupos=[["dano moral"],["negativação","inscrição indevida","cadastro
          de inadimplentes","apontamento"]] (teste real: +37% de julgados frente ao termo
          único, na mesma requisição). Duas técnicas que rendem mais que várias buscas:
          (1) ancorar pela súmula, tema ou IRDR que os julgados do assunto costumam citar
          (ex.: um grupo ["Súmula 385"]) — acha quem fala do mesmo tema com outras palavras;
          (2) depois da 1ª busca, abrir o inteiro teor do resultado mais certeiro e colher as
          palavras e citações que ele usa para a próxima. O portal limita acesso automatizado:
          prefira UMA busca bem construída (com por_pagina maior) a várias seguidas; um ciclo
          completo cabe em 4 a 6 consultas.
          """
  ```

  Para o TRF1, essa MESMA estrutura de ensino vale — só troca o vocabulário
  Lucene por instruções da sintaxe real do motor (`e`/`ou`/`adj`/`prox`/etc.)
  e o "teste real de +37%" precisa vir de um teste real novo contra o TRF1
  (não repetir o número do TJRO por analogia).
- **Onde ficam constantes de versão**: **não há constante de versão formal no
  arquivo** (nenhum `__version__`, nenhuma constante `VERSAO`). O único
  "versao" no código é um campo do schema de estado do disjuntor
  (`_ESTADO_PADRAO["versao"] = 1`, `L956`), que é versão do FORMATO do JSON de
  estado, não do pacote. O histórico de versões do servidor (v1.4.1, v1.5.0,
  v1.6.0, v1.7.0) vive só como marcadores em comentários dentro do bloco
  `--selftest`, ao lado de cada leva de testes de regressão (ex. `L1738`,
  `L1778`, `L1929`, `L1994`) — funciona como um changelog inline. Se for
  desejável ter isso mais formal no TRF1, é uma melhoria nova, não uma
  convenção herdada.

## 8. Recomendação final de porte

**Copiar tal qual (só trocando nomes de arquivo/servidor):**
- Bloco de dependências PEP 723 e imports (`L1-L51`)
- truststore + import defensivo de httpx (`L41-L51`)
- Todo o disjuntor de arquivo (`L916-L1265`): estado, trava, transação
  atômica, escada de backoff, log de incidentes, tool de diagnóstico
- Cache de consultas idênticas (`L1243-L1264`, integração em `_post`)
- Esqueleto de registro MCP + `main()` com `--selftest`/`mcp.run()`
  (`L1404-L1409`, `L1560-L2089`)
- Padrão de teste offline com `assert` simples e `_RespostaFake` mínima

**Adaptar (mesma ideia, implementação nova):**
- Camada HTTP: manter "ler corpo antes de decidir por status" e a distinção
  WAF-disfarçado-de-200 vs. 403/429 explícito, mas trocar JSON→XML/CDATA,
  adicionar gestão de cookies + `ViewState` (a sessão é conceito NOVO — nada
  no TJRO se compara), e reescrever as regex de assinatura de bloqueio
- Montagem de consulta: manter a arquitetura de `grupos` (validação, tetos,
  dedupe, OR-dentro-de-AND, curinga só no fim), reescrever toda a sintaxe
  (Lucene → operadores do motor do TRF1) e a lista de palavras reservadas
- Formatação de saída: manter a estrutura de mensagens (cabeçalho com total,
  aviso de corte, "mesmo número N documentos", resumo/jurimetria, dica de
  refino), reescrever a EXTRAÇÃO de cada campo a partir de parsing de
  HTML em vez de leitura de dict `_source`
- Detecção de resultado oposto/câmara/relator divergente: manter a lógica
  (regex sobre a cauda, pares independentes, nunca força lado ambíguo),
  recalibrar os regexes com red-team no vocabulário real do TRF1
- Inteiro teor: manter orçamento global + teto dinâmico por peça e dedupe;
  reescrever a extração de metadados de dentro do HTML

**Não se aplica:**
- `_LUCENE`, `_escape`, `.raw` do Elasticsearch, `HIGHLIGHT`, `ORDENACOES`
  como sort do ES, `_sugestoes` ("você quis dizer" via campo `suggest` do ES)
- Headers `Content-Type: application/json` / `Accept: application/json` (o
  TRF1 provavelmente usa `application/x-www-form-urlencoded` na ida e recebe
  `text/xml` na volta)
- Linha de crédito no rodapé — nunca existiu no TJRO, não há o que herdar

**Ordem sugerida de implementação:**
1. Descobrir manualmente (browser + devtools) o fluxo JSF real: GET inicial
   para pegar `ViewState` + cookies, POST do formulário de busca com o
   `ViewState`, parsing do `<partial-response>`/`CDATA` de volta. Sem isso
   nada mais roda.
2. Portar o disjuntor de arquivo tal qual (seção 3) — é isolado, testável
   offline, e protege o TRF1 desde a primeira chamada real.
3. Escrever a extração de campos do HTML (equivalente a `_relator`, `_orgao`,
   `_citacao`, `_cnj` etc.) como funções puras testáveis com HTML fixo — sem
   rede, do jeito que o TJRO testa `_format_busca`/`_format_inteiro` com
   fixtures em `--selftest`.
4. Portar a camada HTTP (`_post` equivalente) com gestão de sessão/ViewState,
   detecção de bloqueio adaptada, e o mesmo padrão de "ler corpo antes de
   decidir por status".
5. Escrever a montagem de consulta (`_montar_grupos` equivalente) na sintaxe
   do motor do TRF1, com os mesmos tetos e a mesma validação.
6. Registrar as tools MCP com docstrings que ensinem o modelo a pesquisar
   (seção 7), espelhando o tom do TJRO.
7. Implementar `obter_inteiro_teor_trf1` por último — depende de tudo acima
   já funcionar para uma busca simples.
