# Jurisprudência do TRF1 e da TNU no Claude — instalação

Este projeto dá ao **Claude** a capacidade de pesquisar **julgados do Tribunal Regional Federal da 1ª Região**
(TRF1 e Turmas Recursais dos Juizados Especiais Federais) e da **Turma Nacional de Uniformização** no portal
oficial do Conselho da Justiça Federal, trazer a ementa e o dispositivo integrais, ler o inteiro teor da TNU e
conferir citação palavra por palavra — sem login e sem captcha.

## O que ele faz, em termos simples

Diferente de um índice local, este projeto **pergunta ao portal do CJF na hora** em que você pede uma pesquisa,
do mesmo jeito que você faria no navegador, só que com o Claude montando a consulta e lendo o resultado. Ele
**não baixa o acervo inteiro** para o seu computador: o único material que fica no disco são os **recibos** — uma
cópia do texto que o portal entregou para cada decisão que você abriu (poucos KB cada), para que a conferência de
citação depois não precise perguntar de novo.

Por isso ele respeita um **ritmo**: 24 requisições por janela, 1,5 segundo entre elas, e recua sozinho se o portal
recusar. Isso existe para o servidor do tribunal continuar de pé e para você não ser bloqueado.

> **Sobre o que ele não acessa:** o inteiro teor dos casos antigos do TRF1 fica num arquivo
> (`arquivo.trf1.jus.br`) protegido por verificação anti-robô. **Este projeto não passa por essa verificação** — o
> link fica na citação para você abrir no navegador. Na base do TRF1 o que o portal entrega é a ementa e o
> dispositivo, nunca o voto; na TNU o inteiro teor é aberto e o projeto o lê inteiro.

## Antes de começar: dois programas que faltam no computador

Este guia assume que você **nunca usou o Terminal**. Cada comando abaixo você **copia e cola**.

### O que é o "Terminal"

É um aplicativo que já vem no seu Mac, para digitar (ou colar) comandos de texto. Você vai usá-lo só nesta
instalação — depois disso, o dia a dia é 100% dentro do Claude, em linguagem normal.

**Para abrir o Terminal:** pressione `Cmd` + `Espaço`, digite `Terminal` e pressione `Enter`.

> **Como colar um comando:** copie o texto do bloco cinza, clique dentro da janela do Terminal, pressione
> `Cmd` + `V` e por fim `Enter`. Espere terminar antes de colar o próximo.

### 1. Instale o Python (se ainda não tiver)

```bash
python3 --version
```

- **Se aparecer `Python 3.11.x` ou mais novo:** já tem. Pule para o passo 2.
- **Se der erro, ou mostrar versão menor que 3.11:** baixe o instalador em
  **[python.org/downloads](https://www.python.org/downloads/)**, dê dois cliques no arquivo baixado e siga a
  instalação normal. Depois, feche e abra o Terminal de novo e repita o comando acima.

### 2. Instale o Git (se ainda não tiver)

```bash
git --version
```

- **Se aparecer um número de versão:** já tem, pule para o passo 3.
- **Se o Mac perguntar "Instalar as ferramentas de linha de comando do Xcode?":** clique em **Instalar** e aguarde.
  Quando terminar, repita o comando acima.

## Instalar o projeto (5 a 10 minutos)

### 3. Baixe o projeto para o seu computador

```bash
git clone https://github.com/robertogecia/trf1-jurisprudencia-mcp.git ~/MCP/trf1-jurisprudencia
cd ~/MCP/trf1-jurisprudencia
```

O primeiro comando copia o projeto para a pasta `MCP/trf1-jurisprudencia` dentro da sua pasta de usuário. O
segundo entra nessa pasta.

### 4. Instale as peças que o projeto precisa

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

O primeiro comando cria uma "caixa isolada" (`.venv`) só para este projeto. O segundo baixa, dentro dela, as três
bibliotecas de que o projeto precisa.

> **Por que a versão importa aqui:** em `requirements.txt` está escrito `mcp<2` — isso é proposital. Se alguém
> trocar por uma versão 2.x, o projeto conecta ao Claude só na aparência, e nenhuma ferramenta funciona, sem aviso.

### 5. Confira que deu tudo certo

```bash
.venv/bin/python servidor_trf1.py --selftest
```

Isso roda as verificações internas sobre respostas reais do portal guardadas no projeto, **sem acessar a
internet**. Espere aparecer, na última linha:

```
selftest offline OK
```

Se aparecer qualquer `AssertionError`, pare aqui e peça ajuda antes de usar em um caso real (veja "Autor", no fim).

### 6. Ligue o projeto ao Claude

**Se você usa o Claude Code** (a versão de terminal do Claude), cole:

```bash
claude mcp add trf1_jurisprudencia -- ~/MCP/trf1-jurisprudencia/.venv/bin/python ~/MCP/trf1-jurisprudencia/servidor_trf1.py
```

**Se você usa o Claude Desktop** (o aplicativo com janela):

1. Abra o Claude Desktop.
2. Clique em **Configurações** (ou o ícone de engrenagem).
3. Procure **Desenvolvedor** e clique em **Editar configuração** (abre o arquivo `claude_desktop_config.json`).
4. Dentro de `"mcpServers"`, acrescente o trecho abaixo. Troque `SEU-USUARIO` pelo nome da sua conta no Mac (cole
   `whoami` no Terminal para descobrir):

```json
{
  "mcpServers": {
    "trf1_jurisprudencia": {
      "command": "/Users/SEU-USUARIO/MCP/trf1-jurisprudencia/.venv/bin/python",
      "args": ["/Users/SEU-USUARIO/MCP/trf1-jurisprudencia/servidor_trf1.py"]
    }
  }
}
```

5. Salve e **feche e abra o Claude Desktop de novo** — ele só lê essa configuração ao iniciar.

Não há passo 7: não existe índice para baixar. A primeira pesquisa já funciona.

## Como usar (aqui não tem mais Terminal — é tudo dentro do Claude)

Exemplos do que pedir, em português normal:

> "O que o TRF1 entende sobre benefício por incapacidade quando a doença é anterior ao reingresso no RGPS?"
>
> "Acórdãos da 1ª Turma do TRF1 sobre desapropriação e juros compensatórios, julgados em 2026."
>
> "Traga todas as decisões do processo 1002249-04.2026.4.01.9999 no TRF1."
>
> "Na TNU, o inteiro teor do PUIL 0011441-43.2015.4.03.6301."
>
> "Confira se esta frase está literalmente no acórdão da TNU 0011441-43.2015.4.03.6301: «a Turma Nacional de
> Uniformização decidiu, por unanimidade, conhecer parcialmente do recurso»."

São quatro ferramentas. A que mais importa é a **conferência literal** (`verificar_citacao_trf1`): ela responde se
o trecho aparece palavra por palavra no texto que o portal entregou **e, na TNU, de quem é a frase** — porque um ✅
pode estar apontando para uma ementa do STJ transcrita dentro do voto, para o voto vencido ou para o relatório
narrando o que o INSS alegou. Os alertas `TRANSCRIÇÃO`, `VOTO DIVERGENTE`, `ALEGAÇÃO DA PARTE`, `ENTRE ASPAS` e
`NEGAÇÃO` existem para isso.

**Nunca cite entre aspas sem passar por ela.** Na base do TRF1 ela cobre ementa e dispositivo — e diz, na
resposta, que o voto não foi analisado porque o portal não o expõe.

## O que ele NÃO cobre

- **O voto dos acórdãos do TRF1.** O portal entrega só ementa e dispositivo; o inteiro teor antigo fica atrás de
  verificação anti-robô que este projeto não contorna. A ficha de precedente fica em "só ementa/índice" até você
  abrir o PDF no navegador.
- **Zero resultado nunca é "não existe no TRF1"** — o motor casa palavras, e palavras da conclusão que você
  espera ("não afasta", "é inócua") zeram a busca. Refaça com o fato julgado.
- **`[PESQUISA NÃO REALIZADA — …]` é outra coisa:** o portal não foi perguntado (ritmo, recusa, rede). A mensagem
  diz o motivo. Nunca escreva "não localizado" numa peça a partir disso.
- Não substitui base paga. Se você tem JusRatio ou equivalente, use os dois — e confira aqui o que achou lá.

## Avisos

- **Projeto não-oficial.** Se o portal do CJF mudar de formato, pode parar até ser atualizado.
- **Toda saída é rascunho.** Quem assina a peça confere. Vale em dobro para citação.
- **Ritmo.** 24 requisições por janela, 1,5 s entre elas, e o ritmo aperta sozinho se o portal recusar. Não
  contorne por navegador automatizado ou proxy.
- **Recibos.** Ficam em `~/.trf1-jurisprudencia-recibos/`, só legíveis pelo seu usuário. O inteiro teor da TNU
  nomeia partes: não publique essa pasta.
- **Aviso de atualização.** Na subida, o servidor consulta uma vez a página de releases deste repositório, em
  segundo plano, para avisar se há versão mais nova. Nada da sua pesquisa ou do seu caso sai daqui. Para desligar,
  defina `TRF1_MCP_SEM_AVISO_ATUALIZACAO=1`.

## Atualizar

```bash
cd ~/MCP/trf1-jurisprudencia && git pull && .venv/bin/python servidor_trf1.py --selftest
```

Depois reinicie o Claude. Os recibos não se perdem.

## Desinstalar

1. **Claude Desktop:** volte em Configurações → Desenvolvedor → Editar configuração, e apague o trecho
   `"trf1_jurisprudencia": { ... }`. **Claude Code:** cole `claude mcp remove trf1_jurisprudencia` no Terminal.
2. Apague a pasta do projeto (`rm -rf ~/MCP/trf1-jurisprudencia`) e, se quiser, os recibos
   (`rm -rf ~/.trf1-jurisprudencia-recibos`).

## Autor

**Roberto Grécia Bessa** — OAB/RO 7865-A
Instagram: [@robertogrecia](https://instagram.com/robertogrecia)
