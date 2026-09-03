"""
Configuração de uma campanha de aviso de bloqueio.

MODELO DE DATAS (duas, e elas fazem coisas diferentes)
  data_envio    -- quando o disparo roda. É A BASE DO CÁLCULO DE JUROS.
  data_limite   -- prazo para regularização ou acordo; depois dela, bloqueio.
                   Aparece no texto ("regularização deve ocorrer até o dia X")
                   e NÃO entra no cálculo.

Consequência aceita: o quadro é a POSIÇÃO na data de envio, não um valor de
quitação. Quem pagar exatamente esse número no último dia do prazo deixa
resíduo de juros. Por isso o quadro é rotulado "posição em DD/MM" e o texto
nunca promete que aquele valor quita — o acordo é negociado no retorno.

CÓPIAS FIXAS
copia_padrao entra em todos os e-mails da campanha (caixa do financeiro,
supervisão). Fica aqui, e não no template, porque muda por campanha.

LISTA DE ENTRADA
Aceita CNPJ, CPF ou código CIGAM, misturados. Resolve tudo para código.

  - Código no CIGAM tem zero à esquerda ('000479'). O Sheets converte para 479
    sozinho quando a coluna não é texto, então o zfill é o caso normal.
  - CNPJ e código são, na prática, intercambiáveis: dos 9 CNPJs com mais de um
    cadastro, só um tem cadastro cobrável, e só um dos dois. O ambíguo de
    verdade é produtor rural (um CPF, várias fazendas). Quando acontecer, esta
    lista REJEITA em vez de escolher — nunca dispara para o cadastro errado.
"""

from dataclasses import dataclass, field
from datetime import date
import re


@dataclass(frozen=True)
class Campanha:
    """Parâmetros de UMA rodada. Imutável: se algo muda, é outra campanha,
    com outro id e outro log."""

    id_campanha: str        # ex.: '2026-09-A' — entra no log e no snapshot
    data_envio: date
    data_limite: date       # prazo para regularização/acordo = data de bloqueio
    dry_run: bool = True    # padrão seguro: monta a planilha, não envia
    copia_padrao: tuple = ()   # em cópia em TODOS os e-mails
    teto_diario: int = 450     # destinatários/dia; o resto fica na fila

    # --- trava de segurança -------------------------------------------------
    # Enviar aviso de bloqueio não tem desfazer. Só existem dois estados
    # válidos, e nenhum é o default:
    #   TESTE     redirecionar_para preenchido -> tudo vai para um endereço só
    #   PRODUÇÃO  redirecionar_para vazio E confirmo_producao=True
    # Sem nenhum dos dois, disparar() se recusa a enviar.
    #
    # Isto é campo declarado de propósito. Antes era pendurado com
    # object.__setattr__ e lido com getattr(..., None): um erro de digitação no
    # nome criava o atributo, o getattr devolvia None e o disparo ia para os
    # clientes de verdade, em silêncio.
    redirecionar_para: str | None = None
    confirmo_producao: bool = False

    # Em modo TESTE, consultar o log de teste (padrão) faz a fila PROGREDIR:
    # com teto diário, a execução seguinte continua de onde parou em vez de
    # remandar os mesmos primeiros para sempre.
    #
    # reenviar=True ignora o log de teste e manda tudo de novo — serve para
    # reconferir renderização depois de mexer no template. Não afeta produção
    # em nenhum dos casos: os dois logs são independentes.
    reenviar: bool = False

    def __post_init__(self):
        if self.data_limite < self.data_envio:
            raise ValueError(
                f"data_limite ({self.data_limite}) é anterior ao envio "
                f"({self.data_envio}): o cliente receberia um prazo já vencido."
            )
        if self.redirecionar_para is not None:
            if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}",
                                self.redirecionar_para.strip().lower()):
                raise ValueError(
                    f"redirecionar_para={self.redirecionar_para!r} não é um e-mail "
                    f"válido. Em modo teste tudo vai para esse endereço; se ele "
                    f"estiver errado, o teste falha inteiro."
                )
            if self.confirmo_producao:
                raise ValueError(
                    "redirecionar_para e confirmo_producao juntos são contraditórios. "
                    "Ou é teste, ou é produção."
                )

    @property
    def modo(self) -> str:
        if self.dry_run:
            return "DRY_RUN"
        return "TESTE" if self.redirecionar_para else "PRODUCAO"

    def checar_pode_enviar(self):
        """Chamado por envio.disparar() antes da primeira mensagem."""
        if self.dry_run or self.redirecionar_para:
            return
        if not self.confirmo_producao:
            raise RuntimeError(
                f"Campanha '{self.id_campanha}' enviaria para CLIENTES REAIS.\n"
                f"Para testar:  redirecionar_para='seu.email@ctasmart.com.br'\n"
                f"Para valer:   confirmo_producao=True (leia a Previa antes)."
            )

    @classmethod
    def ensaio(cls, id_campanha: str, data_envio: date, data_limite: date, **kw):
        """ENSAIO: resolve tudo e não envia nada. É o portão de produção.

        Mandar 199 mensagens para a própria caixa a cada rodada é ruído que
        ninguém lê — e o que se quer garantir é o COMPORTAMENTO, não receber os
        e-mails. O ensaio percorre a fila inteira, aplica a idempotência de
        produção, o teto e a regra de "sem destinatário", e devolve o relatório
        do que sairia. Sem SMTP, sem senha, sem log.

        Diferença única em relação à produção: `enviar_fn` não é chamada e o
        log não é gravado. Todo o resto do caminho é o mesmo, porque é a mesma
        fila e a mesma função de disparo.
        """
        kw.pop("redirecionar_para", None)
        kw.pop("confirmo_producao", None)
        kw.pop("dry_run", None)
        return cls(id_campanha=id_campanha, data_envio=data_envio,
                   data_limite=data_limite, dry_run=True, **kw)

    @classmethod
    def teste(cls, id_campanha: str, data_envio: date, data_limite: date,
              para: str, **kw):
        """Atalho para a rodada de teste: nada sai para cliente."""
        kw.pop("confirmo_producao", None)
        return cls(id_campanha=id_campanha, data_envio=data_envio,
                   data_limite=data_limite, redirecionar_para=para,
                   dry_run=False, **kw)

    @property
    def data_base_calculo(self) -> date:
        """Substitui o TODAY() da fórmula da planilha. É a data de ENVIO."""
        return self.data_envio

    @property
    def dias_de_prazo(self) -> int:
        return (self.data_limite - self.data_envio).days


# ---------------------------------------------------------------------------
# Resolução da lista
# ---------------------------------------------------------------------------

CNPJ_PLACEHOLDER = "00000000000000"


def _digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or "").strip())


def classificar_entrada(valor) -> tuple[str, str] | tuple[None, str]:
    """Descobre se o que veio é código ou documento. Devolve (tipo, valor)."""
    d = _digitos(valor)
    if not d:
        return None, "não contém dígitos"
    if len(d) <= 6:
        return "codigo", d.zfill(6)
    if len(d) in (11, 14):
        if d == CNPJ_PLACEHOLDER:
            return None, "CNPJ placeholder (zeros) — não identifica cliente"
        return "documento", d
    return None, f"{len(d)} dígitos: não é código (≤6) nem CPF/CNPJ (11/14)"


@dataclass
class ListaResolvida:
    incluidos: list = field(default_factory=list)      # códigos, ordem preservada
    rejeitados: list = field(default_factory=list)     # (entrada, motivo)

    def resumo(self) -> str:
        linhas = [f"{len(self.incluidos)} cliente(s) na campanha."]
        if self.rejeitados:
            linhas.append(f"{len(self.rejeitados)} rejeitado(s) — CONFIRA antes de disparar:")
            linhas += [f"   {v!r}: {m}" for v, m in self.rejeitados]
        return "\n".join(linhas)


def resolver_lista(entrada: list,
                   cadastros: dict,
                   codigos_com_titulo: set,
                   codigos_com_destinatario: set) -> ListaResolvida:
    """Resolve a lista para códigos e explica cada exclusão.

    cadastros: {codigo: documento_limpo} de todos os cadastros de cliente.

    Nada é descartado em silêncio. Se alguém colocou um cliente na lista, é
    porque decidiu bloqueá-lo — precisa saber quando o aviso não sai, senão o
    corte acontece sem comunicação.
    """
    por_documento: dict[str, list[str]] = {}
    for codigo, doc in cadastros.items():
        if doc and doc != CNPJ_PLACEHOLDER:
            por_documento.setdefault(doc, []).append(codigo)

    resultado = ListaResolvida()
    ja_incluidos: set[str] = set()

    for bruto in entrada:
        tipo, valor = classificar_entrada(bruto)

        if tipo is None:
            resultado.rejeitados.append((bruto, valor))
            continue

        if tipo == "codigo":
            candidatos = [valor] if valor in cadastros else []
            if not candidatos:
                resultado.rejeitados.append((bruto, f"código {valor} não existe no CIGAM"))
                continue
        else:
            candidatos = por_documento.get(valor, [])
            if not candidatos:
                resultado.rejeitados.append((bruto, f"documento {valor} não está no CIGAM"))
                continue
            # Só é ambiguidade real se mais de um cadastro tiver o que cobrar.
            cobraveis = [c for c in candidatos if c in codigos_com_titulo]
            if len(cobraveis) > 1:
                resultado.rejeitados.append(
                    (bruto, f"documento {valor} tem {len(cobraveis)} cadastros com título "
                            f"em aberto ({', '.join(sorted(cobraveis))}) — informe o código")
                )
                continue
            candidatos = cobraveis or candidatos[:1]

        codigo = candidatos[0]

        if codigo in ja_incluidos:
            resultado.rejeitados.append((bruto, f"duplicado na lista ({codigo})"))
        elif codigo not in codigos_com_titulo:
            resultado.rejeitados.append((bruto, f"{codigo} não tem título em aberto"))
        elif codigo not in codigos_com_destinatario:
            resultado.rejeitados.append(
                (bruto, f"{codigo} não tem contato com recebeEmailCartaCobranca=S "
                        f"— o bloqueio aconteceria sem aviso")
            )
        else:
            ja_incluidos.add(codigo)
            resultado.incluidos.append(codigo)

    return resultado


if __name__ == "__main__":
    c = Campanha(
        id_campanha="2026-09-A",
        data_envio=date(2026, 9, 1),
        data_limite=date(2026, 9, 10),
        copia_padrao=("contato.financeiro@ctasmart.com.br", "paul.bioh@ctasmart.com.br"),
    )
    print(f"Campanha {c.id_campanha}: posição em {c.data_base_calculo}, "
          f"prazo até {c.data_limite} ({c.dias_de_prazo} dias), "
          f"dry_run={c.dry_run}, teto {c.teto_diario}/dia")
    print(f"Cópia fixa: {', '.join(c.copia_padrao)}\n")

    r = resolver_lista(
        entrada=[
            "479",                  # código com zero comido pelo Sheets
            "08.077.872/0003-21",   # CNPJ formatado
            "000479",               # repetido
            "111.111.111-11",       # CPF de fazendeiro, dois cadastros cobráveis
            "ABC",
        ],
        cadastros={
            "000479": "08077872000321",
            "006381": "11111111111",
            "006382": "11111111111",
        },
        codigos_com_titulo={"000479", "006381", "006382"},
        codigos_com_destinatario={"000479", "006381", "006382"},
    )
    print(r.resumo())
