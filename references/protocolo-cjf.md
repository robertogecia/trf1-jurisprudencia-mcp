# Protocolo do portal de jurisprudência do CJF — TRF1

Engenharia reversa documental de `https://jurisprudencia.cjf.jus.br/trf1/index.xhtml` (app Java/JSF + PrimeFaces 6.2, stateful). Feita em 11/09/2026. **Sessão interrompida no passo 6** por bloqueio Cloudflare em `arquivo.trf1.jus.br` (ver §6 e §10) — itens 7, 8 e 9 (parcial) ficam [NÃO TESTADO].

Nenhuma requisição foi feita a `pje2g.trf1.jus.br` nem a uma segunda página de `arquivo.trf1.jus.br`. Nenhuma requisição foi feita com `selectTiposDocumento=SUMULA`. Nenhum outro tribunal ou `juris.tjro.jus.br` foi tocado.

## Sumário de fixtures (`~/MCP/trf1-jurisprudencia/fixtures/`)

| Arquivo | Conteúdo |
|---|---|
| `headers_01_trf1.txt` | Headers do GET a `/trf1` (302) |
| `headers_02_index.txt`, `01_index.html` | Headers + corpo do GET a `/trf1/index.xhtml` |
| `headers_03_busca.txt`, `02_busca_dano_moral.xml` | Headers + partial-response da busca "dano moral" (ACORDAO/TRF1) |
| `headers_04_busca2.txt`, `03_busca_viewstate_reuse.xml` | Headers + partial-response da busca "prescrição", reusando o ViewState da requisição 2 |
| `headers_05_pagina2.txt`, `04_pagina2.xml` | Headers + partial-response da paginação (página 2 do DataGrid) |
| `headers_06_avancada.txt`, `05_ckbavancada.xml` | Headers + partial-response do toggle da pesquisa avançada |
| `headers_07_ajuda.txt`, `06_ajuda.html` | Headers + corpo da página de Ajuda |
| `headers_08_arquivo.txt`, `07_arquivo_menu.html` | Headers + corpo do bloqueio Cloudflare em arquivo.trf1.jus.br (403) |
| `cookies.txt` | Cookie jar final (Netscape format) |
| `_contador.txt` | Contador de requisições, uma linha por chamada |

Total de requisições HTTP consumidas: **8** de 20 (ao domínio jurisprudencia.cjf.jus.br: 6; a arquivo.trf1.jus.br: 1 bloqueada; ajuda.xhtml conta dentro do domínio principal — 1). Ver §10 para a tabela completa.

---

## 1. Sessão e ViewState

**Confirmado ao vivo.**

### 1.1 Handshake de sessão

`GET https://jurisprudencia.cjf.jus.br/trf1` (sem cookies) devolve:

```
HTTP/1.1 302
location: https://jurisprudencia.cjf.jus.br/trf1/index.xhtml
content-length: 0
set-cookie: 6adcc195299b516975bc215208fd1483=REDACTED_COOKIE_HASH_VALUE; path=/; HttpOnly; Secure; SameSite=None
Set-Cookie: Cookie-CJF=REDACTED_COOKIE_CJF_VALUE; path=/; Httponly; Secure
```

O nome do primeiro cookie (`6adcc195...`) é um hash que muda por deploy/config — trate como opaco, apenas replay o que o servidor mandar. `Cookie-CJF` é fixo nesse nome.

Em seguida, `GET https://jurisprudencia.cjf.jus.br/trf1/index.xhtml` (com os dois cookies acima) devolve `200`, adiciona **um terceiro cookie**:

```
set-cookie: JSESSIONID=REDACTED_JSESSIONID; Path=/; Secure; HttpOnly
```

e o corpo HTML (16.685 bytes) da página. Um cliente precisa manter os **três** cookies (`6adcc195...`/hash, `Cookie-CJF`, `JSESSIONID`) em todas as chamadas seguintes.

A `action` dos `<form>` já embute o `jsessionid` como matrix parameter:
```html
<form id="formulario" name="formulario" method="post"
  action="/trf1/index.xhtml;jsessionid=REDACTED_JSESSIONID" ...>
```
Não é necessário reproduzir esse `;jsessionid=...` na URL de POST — bastou enviar o cookie `JSESSIONID` e postar para `/trf1/index.xhtml` puro (confirmado: as buscas abaixo fizeram isso e funcionaram).

### 1.2 ViewState — id fixo, valor fixo, reutilizável

O HTML tem **dois** inputs idênticos de ViewState (mesmo `value`, ids diferentes):
```html
<input type="hidden" name="javax.faces.ViewState" id="j_id1:javax.faces.ViewState:0"
  value="-2568262019688462581:-5951574221483686648" autocomplete="off" />
<input type="hidden" name="javax.faces.ViewState" id="j_id1:javax.faces.ViewState:1"
  value="-2568262019688462581:-5951574221483686648" autocomplete="off" />
```

**Testado ao vivo, na ordem:**
1. Busca "dano moral" com esse ViewState → `200`, resultado correto (25.943 documentos), e a `partial-response` devolveu `<update id="j_id1:javax.faces.ViewState:0">` com **o mesmo valor**, inalterado.
2. Uma segunda busca ("prescrição"), **reenviando o mesmo ViewState antigo** (não o "atualizado" — que aliás era idêntico) → `200`, resultado correto (133.849 documentos), ViewState devolvido de novo idêntico.
3. Um pedido de paginação (próxima página) com o **mesmo ViewState de sempre** → `200`, devolveu a página 2 corretamente (itens indexados `tabelaDocumentos:30` a `:59`).
4. O toggle da pesquisa avançada, de novo com o mesmo ViewState → `200`.

**Conclusão confirmada:** nesta configuração do CJF, o ViewState não roda (client-side state saving com um id essencialmente estático para a sessão, ou state saving server-side chaveado pela sessão HTTP e não por um token de view rotativo). Um cliente pode: (a) capturar o ViewState uma vez no GET inicial e (b) reutilizá-lo em todas as chamadas subsequentes da mesma sessão (mesmos cookies), sem precisar reparsear a resposta a cada POST para extrair um "novo" ViewState — mas o `partial-response` sempre devolve o bloco `<update id="j_id1:javax.faces.ViewState:0">`, então um cliente robusto deve mesmo assim ler esse campo e usar o valor mais recente (defensivo, caso o servidor decida rotacionar em algum cenário não testado — ex. após um tempo, após erro de sessão expirada, ou em outro tipo de operação não testada aqui).

**[NÃO TESTADO]:** o que acontece se o ViewState for reenviado depois de a sessão expirar (JSESSIONID inválido), ou depois de horas de uso. Também não foi testado se o mesmo ViewState funciona por uma segunda sessão (outro JSESSIONID) — é esperado que não, pois o valor é tipicamente amarrado à sessão no PrimeFaces/Mojarra.

### 1.3 Cabeçalhos usados nas chamadas AJAX (POST)

Confirmado que este conjunto funciona (o servidor não pareceu exigir mais que isso):
```
Faces-Request: partial/ajax
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
Referer: https://jurisprudencia.cjf.jus.br/trf1/index.xhtml
```
`[NÃO TESTADO]`: se `X-Requested-With` e `Referer` são realmente exigidos (não foi feito um teste de controle removendo-os, para não gastar requisição à toa) — mantenha-os por segurança/semelhança ao navegador real.

---

## 2. Estrutura do HTML de resultado

**Confirmado ao vivo**, a partir de `02_busca_dano_moral.xml` (fixture já salvo, análise abaixo não gastou requisição nova).

### 2.1 Requisição de busca simples

```
POST /trf1/index.xhtml HTTP/1.1
Host: jurisprudencia.cjf.jus.br
Faces-Request: partial/ajax
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Cookie: <hash>=...; Cookie-CJF=...; JSESSIONID=...

javax.faces.partial.ajax=true
&javax.faces.source=formulario%3AactPesquisar
&javax.faces.partial.execute=%40all
&javax.faces.partial.render=formulario
&formulario%3AactPesquisar=formulario%3AactPesquisar
&formulario=formulario
&formulario%3AtextoLivre=dano+moral
&formulario%3AselectTiposDocumento=ACORDAO
&formulario%3Aj_idt62=TRF1
&javax.faces.ViewState=-2568262019688462581%3A-5951574221483686648
```

Resposta: `200`, `Content-Type: text/xml;charset=UTF-8`, envelope PrimeFaces:
```xml
<?xml version='1.0' encoding='UTF-8'?>
<partial-response><changes>
  <update id="j_idt16:messages"><![CDATA[<div id="j_idt16:messages" class="ui-messages ui-widget" aria-live="polite"></div>]]></update>
  <update id="formulario"><![CDATA[ ...todo o HTML do form, incluindo os 30 resultados... ]]></update>
  <update id="j_id1:javax.faces.ViewState:0"><![CDATA[-2568262019688462581:-5951574221483686648]]></update>
</changes></partial-response>
```
Confirma o padrão descrito no prompt original: erro de validação viria em `j_idt16:messages` (aqui vazio = sem erro).

### 2.2 Componente de resultado: NÃO é DataTable/DataList — é um `ui-datagrid` PrimeFaces

Contêiner: `<div id="formulario:tabelaDocumentos" class="ui-datagrid ui-widget">`, inicializado com:
```js
PrimeFaces.cw("DataGrid","widget_formulario_tabelaDocumentos",{
  id:"formulario:tabelaDocumentos",
  paginator:{
    id:['formulario:tabelaDocumentos_paginator_top','formulario:tabelaDocumentos_paginator_bottom'],
    rows:30, rowCount:25943, page:0,
    currentPageTemplate:'(Exibindo {startRecord} - {endRecord} de  {totalRecords}, Página: {currentPage}/{totalPages})'
  }
});
```
Dentro dele: `<div id="formulario:tabelaDocumentos_content" class="ui-datagrid-content ...">`, e cada resultado é um `<div class="ui-g"><div class="ui-datagrid-column ui-g-12 ui-md-12">...</div></div>`.

### 2.3 Total e linha "Exibindo"

Duas ocorrências do total (uma em um link "ver sem formatação", outra no cabeçalho de paginação):
```html
<a class="ui-commandlink ui-widget" ...>25943 Documento(s) encontrado(s)</a>
...
<span class="ui-paginator-current">(Exibindo 1 - 30 de  25943, Página: 1/865)</span>
```
Nota: há **dois espaços** entre "de" e o número total (`de  25943`) — vem do template `{totalRecords}` do PrimeFaces com um espaço fixo antes. Um parser deve tolerar isso (regex `de\s+(\d+)`).

### 2.4 Critério de pesquisa ecoado

Logo no topo do painel de resultado:
```html
<div id="formulario:resultado" class="ui-outputpanel ui-widget">
  <div style="clear: left;">
    <table summary="Informações da pesquisa">
      <tr><td><span class="label">Critério de pesquisa: </span></td><td><span>dano moral</span></td></tr>
    </table>
  </div>
```

### 2.5 Bloco literal de UM resultado completo (primeiro item da busca "dano moral")

```html
<table class="table_pesquisa_lista" id="doc_1174043" summary="Lista de jurisprudência" cellspacing="0" cellpadding="3">
  <tr><td class="titulo_doc"><span><b>Acórdão 0001369-17.2017.4.01.3606</b></span></td></tr>
  <tr class="tr_doc"><td>
    <div id="formulario:tabelaDocumentos:0:j_idt253" class="ui-tabs ...">
      <ul class="ui-tabs-nav ..."><li ...><a href="#formulario:tabelaDocumentos:0:j_idt253:j_idt254">Documento</a>
        <li class="ui-tabs-actions">
          <div style="text-align: center; display: block;">
            <a target="_blank" href="&lt;a href=&quot;https://arquivo.trf1.jus.br/PesquisaMenuArquivo.asp?p1=00013691720174013606&quot; target=&quot;_blank&quot; &gt;Acesse Aqui&lt;/a&gt;">Inteiro Teor</a>
            <button ...>ui-button</button> <!-- "ver sem formatação" -->
            <button ...>ui-button</button> <!-- imprimir -->
          </div>
        </li>
      </ul>
      <div class="ui-tabs-panels">
        <div id="formulario:tabelaDocumentos:0:j_idt253:j_idt254" class="ui-tabs-panel ...">
          <div id="formulario:tabelaDocumentos:0:j_idt253:tabDetalhesProcesso" class="ui-outputpanel ui-widget">
            <div id="item_resultado-1174043">
              <table class="table_resultado" summary="Lista detalhada de jurisprudência" cellpadding="5" cellspacing="0">
                <tbody>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Tipo</span></td></tr>
                    <tr><td>Acórdão</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Número</span></td></tr>
                    <tr><td>0001369-17.2017.4.01.3606<br/> 00013691720174013606</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Classe</span></td></tr>
                    <tr><td>AÇÃO CIVIL PUBLICA (ACP)</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Relator(a)</span></td></tr>
                    <tr><td>JUIZ FEDERAL CHARLES RENAUD FRAZAO DE MORAES (CONV.)</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Origem</span></td></tr>
                    <tr><td>TRF - PRIMEIRA REGIÃO</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Órgão julgador</span></td></tr>
                    <tr><td>QUINTA TURMA</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Data</span></td></tr>
                    <tr><td>14/08/2026</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Data da publicação</span></td></tr>
                    <tr><td>14/08/2026</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Fonte da publicação</span></td></tr>
                    <tr><td>PJe 14/08/2026 PAG<br/> PJe 14/08/2026 PAG</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Ementa</span></td></tr>
                    <tr><td><div id="painel_ementa-1174043">AMBIENTAL. CONSTITUCIONAL. CIVIL. ... <font color="blue"><b>DANO</b></font> AMBIENTAL ... (ementa completa)</div></td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Decisão</span></td></tr>
                    <tr><td>Decide a Quinta Turma, por unanimidade, negar provimento às Apelações, nos termos do voto do Relator.
Brasília/DF, data e assinatura eletrônicas.
Juiz Federal CHARLES RENAUD FRAZÃO DE MORAES
Relator Convocado</td></tr>
                  </div>
                  <div class="ui-outputpanel ui-widget">
                    <tr><td><span class="label_pontilhada">Inteiro teor</span></td></tr>
                    <tr><td><a href="https://arquivo.trf1.jus.br/PesquisaMenuArquivo.asp?p1=00013691720174013606" target="_blank" >Acesse Aqui</a></td></tr>
                  </div>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </td></tr>
</table>
```

**Achados importantes para o parser:**

- **Contêiner de cada resultado**: `<table class="table_pesquisa_lista" id="doc_<N>">` — `N` é um id interno do documento (não é o número do processo). Contar `id="doc_\d+"` no HTML dá o número de itens na página (confirmado: 30 por página).
- **Cada campo** vem num `<div class="ui-outputpanel ui-widget">` contendo duas `<tr>`: a primeira só com `<span class="label_pontilhada">NomeDoCampo</span>`, a segunda com o valor. **Um parser deve iterar por esses pares label→valor**, não por nomes fixos de campo — a lista de campos presentes varia (nem todo resultado tem "Decisão", por exemplo súmulas provavelmente não têm).
- **Número do processo**: aparece nas DUAS formas dentro do mesmo `<td>`, separadas por `<br/>`: `0001369-17.2017.4.01.3606<br/> 00013691720174013606` (primeiro com pontuação, segundo só dígitos — são os 20 dígitos usados no `p1=` do link de arquivo).
- **Ementa**: dentro de um `<div id="painel_ementa-<N>">`; os termos buscados vêm destacados em `<font color="blue"><b>TERMO</b></font>` (herança de grafia — maiúscula/minúscula variando conforme apareceu no texto original, ex. `DANO`/`danos`/`morais`).
- **Link "Inteiro Teor" no cabeçalho da aba (topo) está QUEBRADO**: o `href` desse link contém, como texto **duas vezes escapado**, uma tag `<a>` inteira (`href="&lt;a href=&quot;https://...&quot; ... &gt;Acesse Aqui&lt;/a&gt;"`). Isso parece um bug do template do CJF — clicar nesse link específico levaria a uma URL malformada (o navegador tentaria ir para o texto literal `<a href="...">Acesse Aqui</a>` como URL, o que falha). **O link que funciona de verdade é o outro**, dentro do campo "Inteiro teor" na tabela de detalhes (`<a href="https://arquivo.trf1.jus.br/...">Acesse Aqui</a>`, bem formado, sem escaping duplo). Confirmado nos dois casos: link de arquivo.trf1 (ver §2.5 acima) e link de pje2g (ver §7).
- **Botões auxiliares**: "ver sem formatação" (`j_idt256`, abre `formulario:dialogSemFormatacao` via PrimeFaces.ab) e "imprimir" (`j_idt257`, chama `jqprint()` no client, sem ajax ao servidor).

### 2.6 Lista completa de campos (`label_pontilhada`) vistos no item acima, na ordem em que aparecem
1. Tipo
2. Número
3. Classe
4. Relator(a)
5. Origem
6. Órgão julgador
7. Data
8. Data da publicação
9. Fonte da publicação
10. Ementa
11. Decisão
12. Inteiro teor

`[INFERIDO]`: a ordem e presença de campos pode variar por tipo de documento (Súmula, Decisão Monocrática) e por caso (nem todo acórdão tem "Decisão" preenchida) — não confirmado com uma segunda amostra fora dessa busca. Item 8 do plano original (Súmulas) ficou [NÃO TESTADO] por causa da parada no passo 6.

---

## 3. Paginação

**Confirmado ao vivo.**

### 3.1 Requisição (página 2, offset 30)

```
POST /trf1/index.xhtml HTTP/1.1
Faces-Request: partial/ajax
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

javax.faces.partial.ajax=true
&javax.faces.source=formulario%3AtabelaDocumentos
&javax.faces.partial.execute=formulario%3AtabelaDocumentos
&javax.faces.partial.render=formulario%3AtabelaDocumentos
&formulario%3AtabelaDocumentos_pagination=true
&formulario%3AtabelaDocumentos_first=30
&formulario%3AtabelaDocumentos_rows=30
&formulario%3AtabelaDocumentos_encodeFeature=true
&formulario=formulario
&javax.faces.ViewState=-2568262019688462581%3A-5951574221483686648
```

Note o **id do componente é `formulario:tabelaDocumentos`**, não `formulario:resultado` como cogitado no roteiro original — `formulario:resultado` é o painel externo (que só contém o "Critério de pesquisa"), o DataGrid em si é `formulario:tabelaDocumentos`.

### 3.2 Resultado

`200`, `text/xml`. A `partial-response` trouxe:
```xml
<update id="j_idt16:messages">...(vazio)...</update>
<update id="formulario:tabelaDocumentos"><![CDATA[ <div class="ui-g">...30 itens... ]]></update>
<update id="j_id1:javax.faces.ViewState:0">...(mesmo valor)...</update>
```
Confirmado que os itens retornados são os de índice `30` a `59` — o primeiro item da página tem `id="formulario:tabelaDocumentos:30:j_idt253"` (o índice `30` no meio do id é o índice absoluto do item na lista completa, não reinicia por página). O número do processo do primeiro item da página 2 (`0001999-78.2014.4.01.3315`) é diferente do da página 1, confirmando que avançou de fato.

**Nota**: essa resposta **não incluiu a barra do paginador** (`ui-paginator-current`, "Exibindo X - Y de Z") — só o conteúdo dos 30 itens. Isso é esperado no PrimeFaces DataGrid: o paginador é atualizado no cliente via JS (o widget já sabe `rowCount`/`rows` desde a carga inicial), então o servidor só reenvia o miolo. Um cliente headless que precisa saber "Exibindo X - Y" a cada página deve calcular isso sozinho a partir de `first`/`rows`/`rowCount` (o `rowCount` só vem na resposta da BUSCA original, não se repete na paginação) — ou repetir o parsing do total já obtido na primeira busca.

### 3.3 Trocar para 50 por página

`[NÃO TESTADO ao vivo]`, mas o `<select>` do rows-per-page já está visível no HTML da busca:
```html
<select id="formulario:tabelaDocumentos:j_id16" name="formulario:tabelaDocumentos_rppDD" ...>
  <option value="10">10</option>
  <option value="30" selected="selected">30</option>
  <option value="50">50</option>
</select>
```
`[INFERIDO]`: por analogia ao padrão de paginação confirmado acima, trocar rows-per-page deveria ser uma requisição parecida trocando `formulario:tabelaDocumentos_rows=50` e possivelmente `javax.faces.source=formulario:tabelaDocumentos:j_id16` (o próprio select) em vez de `formulario:tabelaDocumentos`. Não testado.

---

## 4. Pesquisa avançada (campos específicos)

**Confirmado ao vivo.**

### 4.1 Requisição do toggle (checkbox "Pesquisa avançada")

No HTML do index, o `onchange` do checkbox é:
```html
<input id="formulario:ckbAvancada_input" name="formulario:ckbAvancada_input" type="checkbox"
  onchange="PrimeFaces.ab({s:&quot;formulario:ckbAvancada&quot;,e:&quot;change&quot;,
                           p:&quot;formulario:ckbAvancada&quot;,u:&quot;formulario:pesquisaAvancada&quot;});" />
```
Reproduzido como:
```
javax.faces.partial.ajax=true
&javax.faces.source=formulario%3AckbAvancada
&javax.faces.partial.execute=formulario%3AckbAvancada
&javax.faces.partial.render=formulario%3ApesquisaAvancada
&formulario%3AckbAvancada_input=on
&formulario=formulario
&javax.faces.ViewState=-2568262019688462581%3A-5951574221483686648
```
(Note: `u:` no `PrimeFaces.ab` do index.html é `formulario:pesquisaAvancada`, **não** `formulario:pesquisa` como estava no roteiro original — o id certo é `pesquisaAvancada`.)

### 4.2 Resultado — todos os campos da pesquisa avançada

`200`, `text/xml`, `render` devolveu o painel `formulario:pesquisaAvancada` completo. **Todos os campos são `<input type="text">` livres**, exceto o combo de tipo de data — não há autocomplete/typeahead nem `<select>` para relator/classe/órgão (diferente do que o roteiro cogitava):

| Label | name do input | Tipo |
|---|---|---|
| Número | `formulario:j_idt28` | texto livre |
| Classe | `formulario:j_idt30` | texto livre |
| Relator | `formulario:j_idt32` | texto livre |
| Revisor | `formulario:j_idt34` | texto livre |
| Relator Convocado | `formulario:j_idt36` | texto livre |
| Relator para Acórdão | `formulario:j_idt38` | texto livre |
| Órgão Julgador | `formulario:j_idt40` | texto livre |
| Origem | `formulario:j_idt42` | texto livre |
| Ementa/Decisão | `formulario:j_idt44` | texto livre |
| Referência legislativa | `formulario:j_idt46` | texto livre |
| Data (de) | `formulario:j_idt48_input` | calendário PrimeFaces, `dd/mm/yy`, popup |
| Data (até) | `formulario:j_idt50_input` | calendário PrimeFaces, `dd/mm/yy`, popup |
| Tipo (data) | `formulario:combo_tipo_data_input` | `<select>` com `DTDP` (Julgamento) / `DTPP` (Publicação) |

Os `id`/`name` (`j_idt28`, `j_idt30`...) são gerados automaticamente pelo JSF e **podem mudar entre deploys** — um cliente real deve extrair esses `name` do HTML/partial-response ao vivo (por posição/label), não hardcodá-los como constantes permanentes.

`[NÃO TESTADO]`: uma busca de fato usando esses campos (ex. `formulario:j_idt32=Silva` para Relator). A sintaxe de campo alternativa documentada em §5 (`termo[REL]` dentro de `textoLivre`) provavelmente é equivalente e mais simples de reproduzir num cliente (um único campo de texto), então priorizar essa via.

---

## 5. Sintaxe de busca (página de Ajuda)

**Confirmado ao vivo**, `GET https://jurisprudencia.cjf.jus.br/ajuda.xhtml` → `200`, HTML, 19.100 bytes. (O link no index é `<a href="javascript:abrePopup('ajuda','/ajuda.xhtml')">Ajuda</a>` — o alvo real é o caminho `/ajuda.xhtml`, raiz do domínio, fora do path `/trf1/`.)

### 5.1 Regras gerais
- Não usar preposições, conjunções ou artigos na expressão de busca.
- Não usar pontuação.
- Busca não diferencia maiúsculas/minúsculas nem acentuação.

### 5.2 Sintaxe de campo (paragraph search)
`termo[PARAGRAFO]` ou `termo.PARAGRAFO.` — aceita nome curto ou longo. Tabela completa nome-curto → nome-longo:

| Curto | Longo | Curto | Longo |
|---|---|---|---|
| ID | ID_DOCUMENTO | REL | RELATOR |
| ORIG | ORIGEM | REV | REVISOR |
| TIPO | TIPO_DOCUMENTO | RELA | RELATOR_ACORDAO |
| CLAS | CLASSE | RELC | RELATOR_CONVOCADO |
| UF | UF | RESP | RELATOR_SUPLENTE |
| TRIB | TRIBUNAL | OBS | OBSERVACOES |
| ORGA | ORGAO_JULGADOR | REFL | REF_LEGISLATIVA |
| DTDE | DATA_DECISAO | PREC | PRECEDENTES |
| DTDP | DATA_DECISAO_PESQ | SUCE | SUCESSIVOS |
| DTPP | DATA_PUBLICACAO_PESQ | DOUT | DOUTRINA |
| PROC | PROCESSO | INDE | INDEXACAO |
| PRFO | PROCESSO_FORMATADO | CATA | CATALOGO |
| EMEN | EMENTA | FONT | FONTE_PUBLICACAO |
| DECI | DECISAO | OUTF | OUTRAS_FONTES |
| TXTO | TXT_ORIGEM | OURE | OUTRAS_REFERENCIAS |
| ITEO | INTEIRO_TEOR | | |

### 5.3 Busca por data
Formato `AAAAMMDD` sem separadores, junto do campo de data:
```
19950102[DTDP]   (data de decisão)
19950102[DTPP]   (data de publicação)
```

### 5.4 Operadores (tabela completa da Ajuda)

| Operador | Uso | Descrição |
|---|---|---|
| `" "` | `"termo1 termo2"` | busca por frase exata |
| `( )` | `(termo1 E termo2) NAO termo3` | agrupamento/precedência |
| `E` | `termo1 E termo2` | AND |
| `OU` | `termo1 OU termo2` | OR |
| `NAO` | `termo1 NAO termo2` | AND NOT |
| `ADJ[n]` | `termo1 ADJ2 termo2` | termos adjacentes, na ordem dada; `n` = máx. de termos entre eles (padrão 1, máx. 99) |
| `[campo]` | `termo[campo]` | restringe a um parágrafo/campo |
| `XOU` | `termo1 XOU termo2` | OR exclusivo |
| `PROX[n]` | `termo PROX3 termo` | mesma sentença, qualquer ordem, até n termos entre eles |
| `COM` | `termo1 COM termo2` | ambos na mesma sentença |
| `MESMO` | `termo1 MESMO termo2` | ambos no mesmo parágrafo/subparágrafo |
| `NAO ADJ[n]` | | segundo termo não adjacente ao primeiro |
| `NAO PROX[n]` | | segundo termo não próximo do primeiro |
| `NAO COM` | | primeiro termo sem o segundo na mesma sentença |
| `NAO MESMO` | | primeiro termo sem o segundo no mesmo parágrafo |
| `[-campo]` | `termo[-DOUT]` | exclui documentos com o termo nesse campo |
| `?` | `MA??`, `A??Z` | um caractere curinga cada `?` |
| `$[n]` | `A$`, `A$3Z` | zero ou mais caracteres (opcionalmente limitado a n) |
| `{ }` | `{V,C,"C",C,V}` | classes de caracteres: `{?}` todos, `{A}` alfabéticos, `{C}` consoantes, `{D}` dígitos, `{V}` vogais |
| `:` | `lei:9612 COM art:1` | separador para pesquisa em legislação (exceto STJ, que usa `E`: `lei E 8112`) |

Todo esse conteúdo é texto simples da página, não JS/imagem — reproduzido literalmente acima (parafraseado onde necessário para não copiar blocos extensos verbatim).

---

## 6. Inteiro teor — arquivo.trf1.jus.br (casos antigos)

**BLOQUEADO. Parada obrigatória acionada aqui.**

```
GET https://arquivo.trf1.jus.br/PesquisaMenuArquivo.asp?p1=00013691720174013606
→ HTTP/2 403
  server: cloudflare
  cf-mitigated: challenge
  content-security-policy: ...challenges.cloudflare.com...
  <title>Just a moment...</title>  (página de desafio Cloudflare Turnstile/JS challenge)
```
Corpo confirmado como página de challenge Cloudflare padrão ("Just a moment..."), não um bloqueio de aplicação (não é 404, não é erro do ASP). **Conforme a regra de orçamento, a sessão de reverse engineering parou imediatamente neste ponto** — nenhuma nova requisição foi feita a `arquivo.trf1.jus.br`, `pje2g.trf1.jus.br` ou a qualquer outra URL depois deste bloqueio.

**Implicação para quem for construir o cliente**: um `GET` direto e simples (mesmo com User-Agent de navegador) a `arquivo.trf1.jus.br` é bloqueado por Cloudflare com desafio JS — não dá para buscar automaticamente o PDF final por essa rota sem alguma forma de navegador real (ex. Playwright/Selenium) resolvendo o challenge, ou sem que o domínio esteja na allowlist de algum IP/sessão específica. Isso é uma limitação de infraestrutura do lado de `arquivo.trf1.jus.br`, distinta do comportamento de `jurisprudencia.cjf.jus.br` (que não mostrou nenhum sinal de bloqueio nas 7 chamadas anteriores).

O padrão de URL, contudo, está confirmado a partir do HTML de resultado (§2.5): `https://arquivo.trf1.jus.br/PesquisaMenuArquivo.asp?p1=<20 dígitos do CNJ sem pontuação>`. O que essa página normalmente devolve (lista de documentos? link direto a PDF? frameset?) fica **[NÃO TESTADO]**.

---

## 7. Inteiro teor — PJe (pje2g.trf1.jus.br)

**[NÃO TESTADO ao vivo]** — a sessão foi interrompida no passo anterior (§6) antes de chegar a este item, por força da regra de parada em bloqueio HTTP.

O que dá para afirmar **só a partir do HTML já capturado** (fixture `02_busca_dano_moral.xml`, sem gastar requisição nova):

- É um `<a href="...">` puro, **não** um `commandLink`/`onclick` com `PrimeFaces.ab`. Confirmado nos dois pontos onde aparece por item (aba "Documento" no topo — com o bug de escaping duplo já descrito em §2.5 — e no campo "Inteiro teor" da tabela de detalhes, bem formado):
  ```html
  <a href="https://pje2g.trf1.jus.br/consultapublica/ConsultaPublica/listView.seam" target="_blank" >Acesse Aqui</a>
  ```
- **O href é idêntico em TODOS os 15 resultados de origem PJe** encontrados na busca "dano moral" (confirmado por regex sobre o fixture: 15 ocorrências, todas apontando para exatamente essa mesma URL, sem nenhum parâmetro — nem número de processo, nem id de documento, nem token). Ou seja, **o link não carrega nenhuma informação que identifique o processo específico** — é um link genérico para a página de busca pública do PJe de 2º grau do TRF1. Um cliente que quiser o inteiro teor de um caso PJe via esse link terá que, depois de chegar lá, pesquisar manualmente pelo número do processo (que ele já tem, do campo "Número" do próprio resultado) — a integração aqui é só "leve o usuário à porta certa", não um deep link.
- Como é um `GET` sem parâmetros que identifiquem nada, e o objetivo desta etapa era só verificar "é GET simples com parâmetros claros?" — a resposta é: é GET simples, mas **sem** parâmetros específicos do processo, então o item "faça UMA requisição para ver o que volta" do roteiro original perde a utilidade (o retorno seria sempre a mesma tela de busca genérica do PJe, útil só para confirmar que o domínio responde, não para extrair dado nenhum). Combinado com o bloqueio do passo 6, essa chamada não foi feita.

---

## 8. Súmulas (`selectTiposDocumento=SUMULA`)

**[NÃO TESTADO]** — não alcançado; a sessão parou no passo 6 (arquivo.trf1.jus.br bloqueado) antes de chegar a esta etapa de menor prioridade, conforme a regra de orçamento/parada em bloqueio.

---

## 9. Headers de resposta, cache, cookies e indícios de rate limit

**Confirmado a partir do que já foi capturado** (sem requisição nova):

- `jurisprudencia.cjf.jus.br` não devolveu, em nenhuma das 7 chamadas feitas a esse domínio, nenhum header de rate-limit explícito (`Retry-After`, `X-RateLimit-*` etc. — ausentes), nem `Cache-Control` nas respostas HTML/XML de aplicação (a página inicial não tem `Cache-Control`; as respostas AJAX trazem `cache-control: no-cache`). Não há CDN/WAF aparente nesse domínio (sem header `server:`, sem `cf-ray`) — parece servido direto por um app server (provavelmente WildFly/JBoss, mas não confirmado por header explícito — `Server` ausente nas respostas capturadas).
- Cookies: `Cookie-CJF` e o cookie-hash (`6adcc195...`) são `Secure; HttpOnly`; o hash tem `SameSite=None` explícito (permite uso cross-site, ex. de um iframe — não relevante aqui mas documentado). `JSESSIONID` é `Secure; HttpOnly`, sem `SameSite` explícito.
- `Strict-Transport-Security: max-age=16070400; includeSubDomains` presente em toda resposta de `jurisprudencia.cjf.jus.br`.
- **Contraste direto**: `arquivo.trf1.jus.br` está atrás de **Cloudflare** com um conjunto extenso de headers de segurança (CSP restritiva, COEP/COOP/CORP, Permissions-Policy, X-Frame-Options) e demonstrou ativamente um desafio anti-bot (`cf-mitigated: challenge`) já na primeira requisição, sem qualquer padrão de "N requisições e depois bloqueia" percebido — bloqueou de cara. Isso é forte indício de que esse subdomínio trata tráfego automatizado (sem cookies/sessão de navegador real ou sem execução de JS) como suspeito por padrão, independente de volume.
- `[NÃO TESTADO]`: comportamento de rate limit em `jurisprudencia.cjf.jus.br` sob volume maior — as 6 chamadas feitas (espaçadas em 3s) não geraram nenhum sinal de limitação, mas a amostra é pequena de propósito (orçamento).

---

## 10. Contagem final de requisições (todas ao domínio jurisprudencia.cjf.jus.br, exceto a última)

| # | Método/URL | Status | Content-Type | Tamanho |
|---|---|---|---|---|
| 1 | GET `https://jurisprudencia.cjf.jus.br/trf1` | 302 | (sem corpo) | 0 |
| 2 | GET `https://jurisprudencia.cjf.jus.br/trf1/index.xhtml` | 200 | text/html;charset=UTF-8 | 16.685 |
| 3 | POST `.../trf1/index.xhtml` (busca "dano moral", ACORDAO/TRF1) | 200 | text/xml;charset=UTF-8 | 367.746 |
| 4 | POST `.../trf1/index.xhtml` (busca "prescrição", ViewState reaproveitado) | 200 | text/xml;charset=UTF-8 | 420.938 |
| 5 | POST `.../trf1/index.xhtml` (paginação, página 2 do DataGrid) | 200 | text/xml;charset=UTF-8 | 356.466 |
| 6 | POST `.../trf1/index.xhtml` (toggle pesquisa avançada) | 200 | text/xml;charset=UTF-8 | 8.095 |
| 7 | GET `https://jurisprudencia.cjf.jus.br/ajuda.xhtml` | 200 | text/html;charset=UTF-8 | 19.100 |
| 8 | GET `https://arquivo.trf1.jus.br/PesquisaMenuArquivo.asp?p1=...` | **403** | text/html;charset=UTF-8 (Cloudflare challenge) | 5.843 |

**Total: 8 de 20 requisições usadas.** Sessão encerrada por bloqueio (regra de parada), não por esgotamento de orçamento — sobraram 12 requisições não usadas, deliberadamente não gastas em `arquivo.trf1.jus.br`/`pje2g.trf1.jus.br` depois do 403, nem em SUMULA/rows=50, por serem de menor prioridade e por prudência diante do bloqueio observado.

### O que ficou pendente para uma sessão futura (se o bloqueio de arquivo.trf1.jus.br for contornado, ex. por um operador humano/browser real)
- §3.3: trocar rows-per-page para 50 (baixo risco, mesmo padrão da paginação já confirmada).
- §4: uma busca de fato usando um campo específico da pesquisa avançada.
- §6: o que `PesquisaMenuArquivo.asp` retorna de fato quando não bloqueado — provavelmente uma lista HTML de documentos/anexos do processo, com link(s) para PDF; precisa de acesso via navegador real ou de uma allowlist de IP para confirmar.
- §7: abrir `pje2g.trf1.jus.br/consultapublica/ConsultaPublica/listView.seam` uma vez, para confirmar que é mesmo uma tela de busca genérica (sem pré-preenchimento) e não, por exemplo, um redirecionamento com algum parâmetro de sessão que só aparece em runtime real de navegador.
- §8: estrutura do bloco de resultado para Súmulas (`selectTiposDocumento=SUMULA`), para confirmar se os campos (`label_pontilhada`) diferem dos de Acórdão.
