# Aviso de bloqueio por inadimplência — CTA Smart

Dispara o aviso de regularização para clientes inadimplentes do CIGAM, com o
extrato de títulos, os encargos calculados e os equipamentos vinculados; e
prepara o card de bloqueio no Pipefy para quem não regularizar.

## Ciclo

| Fase | Quando | Função | Consulta o BigQuery? |
|---|---|---|---|
| Preparar | D0 | `preparar(c, gc, bq)` | sim — e **congela** o resultado |
| Conferir | D0 | humano, nas abas `Campanha_Previa` e `Triagem` | — |
| Disparar | D0 | `disparar(c, gc, enviar_fn)` | **não** |
| Prazo | D0..D+n | — | — |
| Reconciliar | D+n | `reconciliar(c, gc, bq)` | sim — única releitura |
| Cards | D+n | `gerar_cards(c, gc, codigos)` | **não** |

O congelamento não é zelo: `gold.inadimplencia` é reescrita durante o dia
(medimos 23% dos clientes mudando entre duas consultas com 15 minutos de
diferença), e o serial some de `gold.bombas_alocadas` quando o bloqueio é
concluído. Reconsultar na hora do disparo enviaria coisa diferente da que foi
conferida.

## Planilhas

**Planilha 1 — Lista de Vendedores e Contatos CIGAM** (`ID_DESTINO`, leitura e escrita)

| Aba | Grão | Quem escreve |
|---|---|---|
| `Campanha_Input` | 1 por cliente | você |
| `Email_Vendedores` | 1 por vendedor | você (`Vendedor \| Email \| Ativo`) |
| `Campanha_Previa` | 1 por cliente | `preparar()` |
| `Campanha_Titulos` | 1 por título | `preparar()` (congelado) |
| `Campanha_Bombas` | 1 por bomba | `preparar()` (congelado) |
| `Campanha_Log` | 1 por e-mail | `disparar()` (append, nunca limpa) |
| `Triagem` | 1 por caso | `preparar()` |

**Planilha 2 — Planilha Central Inadimplência por Carteira de Vendedor**
(`ID_VENDEDORES`, **somente leitura**) — a aba `Base_Clientes`, saída do Apps
Script `consolidarBaseInteligente()`. Nunca escrever aqui: o Apps Script
sobrescreve na próxima execução.

## Notebook

```python
!git clone https://github.com/<org>/<repo>.git && cd <repo> && pip install -q -r requirements.txt
import sys; sys.path.insert(0, '<repo>')

from datetime import date
import main, campanha_v2
from google.colab import userdata

gc, bq = main.conectar()

c = campanha_v2.Campanha(
    id_campanha="2026-09-A",
    data_envio=date.today(),
    data_limite=date(2026, 9, 10),
    copia_padrao=("contato.financeiro@ctasmart.com.br",),
    teto_diario=450,
    dry_run=True,
)

main.preparar(c, gc, bq)          # congela e escreve a prévia
```

Confira `Campanha_Previa` e `Triagem`. Depois, o teste redirecionado:

```python
c = campanha_v2.Campanha.teste(
    id_campanha="2026-09-A",
    data_envio=date.today(),
    data_limite=date(2026, 9, 10),
    para="yago.gutterres@ctasmart.com.br",   # tudo vai só para cá
    copia_padrao=("contato.financeiro@ctasmart.com.br",),
)

import envio
enviar = envio.criar_enviador_smtp(
    remetente="yago.gutterres@ctasmart.com.br",
    senha_app=userdata.get("SMTP_APP_PASSWORD"),   # Secrets do Colab, nunca na célula
    nome_exibicao="Cta Smart - Cobrança",
)
main.disparar(c, gc, enviar)
```

Tudo chega na sua caixa, com os destinatários reais impressos no topo de cada
mensagem.

Para valer, não basta remover o `redirecionar_para`: é preciso
`confirmo_producao=True`. São dois estados válidos e nenhum é o default —
`disparar()` se recusa a enviar se você não escolher um deles. Aviso de
bloqueio não tem desfazer.

## Módulos

| Arquivo | Papel | Testado |
|---|---|---|
| `encargos.py` | juros e multa, porte 1:1 da planilha | sim, paridade no centavo |
| `campanha_v2.py` | datas e resolução da lista | sim |
| `vendedores.py` | vendedor em cópia, por confiança da regra | sim |
| `template.py` | corpo do e-mail e quadros | sim |
| `envio.py` | fila, teto diário, log, redirecionamento | sim (SMTP não) |
| `pipefy.py` | texto do card de bloqueio | sim |
| `io_sheets.py` | leitura e escrita no Sheets | não |
| `main.py` | orquestração | não |

O que não está testado depende de credencial e de rede. Rode a fase A com dois
ou três clientes antes de confiar.

## Cuidados que já custaram caro

- **`resize=True`** no `set_with_dataframe`. `clear()` não redimensiona a
  grade; sem isso o que não cabe é descartado sem erro.
- **Zero à esquerda.** Código CIGAM é `000479`; o Sheets converte para `479`
  se a coluna não for texto. O `zfill(6)` é o caso normal, não defensivo.
- **`diasAtraso` do warehouse não serve** para o cálculo: conta a partir de
  `data_inicio_inadimplencia`, que já embute carência de dia útil.
- **`round()` do Python é bancário.** O `ROUND()` do Sheets é meio-pra-cima.
  Por isso `Decimal` com `ROUND_HALF_UP` em `encargos.py`.
- **O texto não declara a taxa.** O juro diário é arredondado antes de
  multiplicar pelos dias, o que dá efetivo ~1,04% a.m. em vez de 1,00%.
- **Nome da bomba:** limpo no e-mail (cliente lê), cru no card (suporte precisa
  localizar no sistema CTA).
