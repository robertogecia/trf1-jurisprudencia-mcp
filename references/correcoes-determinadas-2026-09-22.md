# Correções determinadas no servidor do TRF1 — 22/09/2026

Origem: comparação linha a linha entre `servidor_trf1.py` (2.075 linhas, consulta ao vivo o
portal do CJF) e `servidor_tjse.py` (índice local do Boletim Jurídico), feita por agente Opus
que leu os dois arquivos inteiros. As quatro primeiras já foram implementadas e testadas no
TJSE — o código de referência existe e está commitado.

NÃO é sugestão de arquitetura. É lista de defeitos com local, consequência para quem cita
jurisprudência em peça, e ordem de execução.

---

## 1. 🔴 `verificar_citacao_trf1` não diz DE QUEM é a frase

`servidor_trf1.py:1325-1350` (`_verificar_trecho`) devolve apenas `valido: True/False`.

Por que é grave aqui e não em outro lugar: na base `tnu` o servidor baixa o inteiro teor com
relatório e voto (`_anexar_inteiro_teor_tnu`, 1295-1315) e confere o trecho contra esse texto
(1511-1513). É exatamente o texto onde um ✅ pode estar apontando para:
- ementa do STJ **transcrita dentro do voto** do relator;
- o **voto vencido**;
- o relatório **narrando o que a parte sustenta**.

Hoje a saída diz "✅ VÁLIDO · encontrado em: inteiro teor" e nada mais. A peça sai atribuindo
ao TRF1/TNU uma frase que é de outro tribunal, ou do vencido. É o erro que a parte contrária
desmonta em uma linha.

**Determinação:** portar de `servidor_tjse.py` as faixas de atribuição — `faixas_transcritas`
(1168-1199), `faixa_divergente` (1202-1206) e os alertas de `conferir` (1209-1264). As regex
`_RE_ATRIB`, `_RE_DIVERGENCIA` e `_RE_ALEGACAO` (1144-1164) **já são genéricas**: cobrem
`tj-?[a-z]{2}|stj|stf|trf-?\d|tst|trt-?\d+|tnu`. Não têm nada de Sergipe. Levar junto a função
`norm`. Rodar contra o fixture `12_tnu_inteiro_teor.html`.

Aplicar só na base `tnu`. Na base `trf1` o portal não expõe o voto, então não há o que analisar
— e ali a saída deve dizer isso, em vez de calar.

**Custo: médio.** ~120 linhas puras, sem I/O.

---

## 2. 🔴 Erro de parâmetro e disjuntor armado devolvem o MESMO texto

`servidor_trf1.py:1431-1434` achata tudo em `"Erro na consulta ao TRF1: {e}"` — vale para
`ValueError` de parâmetro, disjuntor armado, timeout e bloqueio do portal.

Consequência: o `pesquisador-juridico` lê isso e relata **"nada encontrado no TRF1"** quando o
que houve foi teto de ritmo. Vira "não localizado" numa peça. É o defeito mais barato de
corrigir e um dos piores de sofrer.

**Determinação:** criar a exceção `PesquisaNaoRealizada` nos moldes de `servidor_tjse.py:151-152`
— cujo docstring é literalmente "NUNCA equivale a 'não localizado'" — e carimbar toda saída de
falha, como o TJSE faz em 1679, 1714, 1298 e 1417. As três famílias já estão separadas no
código (`PortalRecusou` / `SessaoInvalida` / `RuntimeError`); falta só não fundi-las na saída.

**Custo: baixo.**

---

## 3. 🔴 Disjuntor é fail-OPEN

`servidor_trf1.py:875-893` (`_transacao`): se não consegue gravar o estado, guarda em memória e
**segue requisitando** (890-892). O aviso só aparece se alguém rodar `diagnostico_ritmo_trf1`
(1001-1005).

Com várias sessões do Claude abertas — que é o uso normal aqui — cada processo passa a ter
orçamento próprio em silêncio, e quem paga é o IP do escritório. O TJSE faz o inverso em três
pontos: estado ilegível → pausa (1268-1270), sem trava de arquivo → recusa (296-298), falha ao
gravar → recusa (317-319).

**Determinação:** inverter os dois `except`. Sem registro, não há requisição.

**Custo: baixo.**

---

## 4. 🟡 `[...]` costura pedaços distantes do acórdão

`servidor_trf1.py:1325-1350` aceita trecho de duas palavras e deixa `[...]` unir um pedaço da
abertura da ementa a outro do fim do dispositivo, bastando que a ordem bata. Um ✅ que não
sustenta as aspas.

**Determinação:** portar as três constantes de `servidor_tjse.py:1165` —
`PISO_TRECHO_PALAVRAS, PISO_TRECHO_CHARS, VAO_MAXIMO = 4, 25, 1500` — e as comparações de
1216-1218 e 1227-1229. O TRF1 já guarda `pos` entre fragmentos na linha 1343, então falta só
comparar.

**Custo: baixo.**

---

## 5. 🟡 Nenhum recibo sobrevive ao processo

Só existe cache em memória de 5 min (`_CACHE_TTL_S`, 1035). Sem recibo em disco, o
`revisor-adversarial` e o `lint_citacoes.py` não têm contra o que conferir a peça dias depois,
e cada reconferência gasta 4 requisições do orçamento do portal.

**Determinação:** portar `gravar_recibo` (tjse 1130-1141), `ler_recibo` (1043-1077 — que põe de
lado recibo com sha256 divergente ou HTML de outro acórdão) e `_campos_de_custodia` (1107-1127).
O formato `id_documento` / `nr_processo` / `texto` **já é compartilhado com TJRO e STJ**, então o
lint reconhece sem alteração. Levar `trechos_transcritos` e `trecho_divergente` em bruto, para o
lint aplicar a própria normalização.

**Custo: médio.** O TRF1 já tem `id`, ementa, dispositivo e, na TNU, o texto integral.

---

## 6. 🟡 Órgão julgador sem procedência declarada

`servidor_trf1.py:400` tira o órgão só do índice (`meta("Órgão julgador")`) e o costura na
citação (566-567) sem dizer de onde veio.

É o mesmo defeito que já pôs quatro peças no ar com a câmara errada do TJRO.

**Determinação, em duas partes:**
- base `tnu`: ler o fecho do inteiro teor e emitir `orgao_fonte`, como o TJSE faz em 567-590,
  1655-1671 e 1701, inclusive a linha de DIVERGÊNCIA quando índice e fecho discordam.
- base `trf1`: **NÃO É PORTÁVEL** — o CJF não expõe o voto, e o próprio cabeçalho do arquivo
  admite isso (linhas 26-29). Ali a determinação é outra: rotular a citação como
  "órgão: do índice do portal, não conferido no fecho". Calar não é opção.

**Custo: médio na `tnu`; na `trf1` é texto.**

---

## 7. 🟡 User-Agent falso de Chrome

`HEADERS_BASE`, 98-104, com o bloco "DECISÃO PESSOAL, NÃO REPLICAR EM PACOTE DISTRIBUÍDO"
(91-97). Só que estes servidores **já viraram pacote para outro escritório**.

**Determinação:** adotar a convenção do TJSE (`99-105`) — identificação honesta por padrão,
override por variável de ambiente. O comentário do próprio TRF1 já concorda.

**Custo: baixo.**

---

## 8. ⚪ Nível de verificação fora da citação

O TRF1 põe a orientação num rodapé solto (770-775), que se perde no instante em que a citação é
copiada para a minuta. O TJSE termina a própria citação em "— verificação: {nivel}"
(1274-1277), e rebaixa para "inteiro teor lido EM PARTE" quando a saída foi cortada (1691-1694).

**Custo: baixo.**

---

## O que o TJSE aprendeu do TRF1, e já está feito

Para referência de quem for mexer — o código está commitado em `~/MCP/tjse-jurisprudencia`:

- **timeout deixou de armar o disjuntor** (c9a7afc). O TJSE pausava 30 min por qualquer exceção
  de rede; com seções acima de 2 MB o timeout é evento esperado. A separação veio de
  `servidor_trf1.py:1136-1151`, que já fazia certo.
- **escada de ritmo adaptativa** (46c9530), portada de `_ESCADA_JANELA_S` / `_SUCESSOS_PARA_RELAXAR`
  (trf1 790-796, 944-981): recusa do portal aperta um degrau, 100 consultas limpas afrouxam.
- **docstring em `Args:`/`Returns:`** (46c9530), no modelo de trf1 1582-1628, inclusive a prática
  de documentar o incidente real dentro do parâmetro que ele afeta.

## O que foi REJEITADO por medição

Contar resultado uma vez por julgamento em vez de por documento (trf1 521-540). Medido no índice
do TJSE: **40 processos com mais de um acórdão em 38.283 — 0,1%**. Além de irrelevante, agrupar
esconderia o resultado dos embargos, que é informação útil. No TRF1 o número pode ser outro:
**medir antes de implementar**, não portar por simetria.

## Não portável do TJSE para o TRF1 (natureza da fonte, não mérito)

Busca por campo da ementa CNJ, panorama/lift de vocabulário, `diagnostico_zero` e o grafo de
citações dependem de varrer o corpus inteiro a custo zero. No TRF1 cada uma custaria 2 a 4
requisições ao CJF (`_JANELA_MAX_REQS = 24`). Não cabe.
