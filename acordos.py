"""
Acordos: quem já está pagando, e quem quebrou.

FONTE: aba ACORDOS da planilha de negociação (ID em ACORDOS_SPREADSHEET_ID).
Somente leitura — é planilha de outro processo.

NÃO É FILTRO BINÁRIO. A coluna `STATUS ACORDO` tem quatro valores e cada um
leva a uma decisão diferente:

    Em Dia          está pagando o acordo          -> protege OS TÍTULOS dele
    A Vencer        parcela futura, acordo vigente -> protege OS TÍTULOS dele
    Quitado         acordo ENCERRADO               -> não protege nada
    Acordo Quebrado parou de pagar                 -> NOTIFICAR, prioritário

Tratar "tem acordo" como exclusão simples erraria nos dois sentidos: pouparia
quem quebrou o acordo (o caso mais grave, porque já teve uma chance) e pouparia
para sempre quem quitou um acordo antigo e voltou a atrasar.

O ACORDO COBRE TÍTULOS, NÃO O CLIENTE. Cada linha da aba tem um `DOC`. Um
cliente que negociou cinco títulos e deixou de pagar outros três deve ser
notificado pelos três. Por isso `titulos_protegidos` guarda os DOCs, e a
exclusão do cliente só acontece quando TODOS os títulos vencidos dele estão
cobertos por acordo vigente.

O STATUS É FÓRMULA, NÃO DIGITAÇÃO. Ele já compara a data de hoje com a data de
pagamento e devolve a cadência correta. Não reconferir: qualquer verificação
nossa por cima das datas de parcela seria refazer o que a fórmula faz, e
marcaria como suspeito exatamente o caso deliberado abaixo.

QUITADO LIBERA. A fórmula deixa de reconhecer o título como inadimplente, então
o que aparecer depois é dívida nova e conta normalmente.

O QUE NÃO FOR CLASSIFICÁVEL, SEGURA. Em vez de inferir intenção a partir de
campo ausente — "data de pagamento vazia deve significar renegociação" —, tudo
que a fórmula não classificar em quitado/quebrado/pagando cai em INDEFINIDO, e
INDEFINIDO não dispara.

Isso cobre o caso real que o time descreveu (deixar a data em branco de
propósito para firmar acordo novo por cima) sem depender de adivinhar o
motivo, e cobre de graça qualquer estado futuro que ninguém me contou. A
alternativa — inferir pela ausência — quebra em silêncio no dia em que alguém
preencher a data achando que estava faltando.

GRÃO: a planilha é por TÍTULO, a régua é por CLIENTE. Um mesmo cliente tem
linhas em status diferentes — a Viação São Jorge tem parcelas Quitado, Em Dia
e Acordo Quebrado ao mesmo tempo. A agregação abaixo resolve na direção que
não erra: qualquer linha quebrada marca o cliente como quebrado.

O CÓDIGO VEM COLADO AO NOME: "004964 - CONFER CONSTRUTORA FERNANDES LTDA".
Extraímos por regex. Não use o CNPJ desta planilha como chave — ele está
digitado à mão e aparece em formatos misturados.
"""

import re
import unicodedata
from dataclasses import dataclass, field

ACORDOS_SPREADSHEET_ID = "1Jv55BSk12YjKp273noi_MUL6X_3ao01VZiWHn7EVhTQ"
ACORDOS_ABA = "ACORDOS"

PAGANDO = "PAGANDO"        # em dia ou a vencer -> protege os títulos do acordo
QUITADO = "QUITADO"        # acordo ENCERRADO   -> não protege
QUEBRADO = "QUEBRADO"      # parou de pagar     -> notificar, prioritário
INDEFINIDO = "INDEFINIDO"  # a fórmula não classificou -> segura e reporta
SEM_ACORDO = "SEM ACORDO"


def _norm(texto) -> str:
    s = str(texto or "").replace("\u00a0", " ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def _status_da_linha(valor) -> str | None:
    t = _norm(valor)
    if not t:
        return None
    if "QUEBRAD" in t:
        return QUEBRADO
    if "QUITAD" in t:
        return QUITADO
    if "EM DIA" in t or "A VENCER" in t:
        return PAGANDO
    return None


def extrair_codigo(empresa) -> str | None:
    """'004964 - CONFER CONSTRUTORA FERNANDES LTDA' -> '004964'."""
    m = re.match(r"\s*(\d{1,6})\s*-", str(empresa or ""))
    return m.group(1).zfill(6) if m else None


def _doc(valor) -> str | None:
    """'20287761/1' -> '20287761'.

    A planilha grava fatura + parcela colados; a titulos_cigam guarda só a
    fatura. Medido: 0 de 65 casam com o DOC inteiro, 65 de 65 cortando no '/'.

    Consequência assumida: o grão vira FATURA, não parcela. Se só a parcela 1
    de uma fatura estiver no acordo, as demais também ficam protegidas — erra
    para o lado de não notificar, que é o lado barato.
    """
    d = re.sub(r"\s+", "", str(valor or "")).split("/")[0].upper()
    return d or None


@dataclass
class Acordos:
    por_cliente: dict = field(default_factory=dict)      # codigo -> status
    # TODOS os títulos que aparecem no acordo do cliente, qualquer que seja o
    # status da linha. Antes eu guardava só as linhas "Em Dia", e isso estava
    # errado: parcela já QUITADA do mesmo acordo fazia o cliente parecer ter
    # título fora dele. A Paranapuan tem 8 títulos vencidos, 7 em dia e 1
    # quitado, e entrou na régua por causa do quitado.
    #
    # O acordo cobre um CONJUNTO de títulos. Linha quitada é progresso dentro
    # do acordo, não título descoberto. Quem decide se o conjunto protege é o
    # status agregado do cliente (ver `protege`).
    titulos_do_acordo: dict = field(default_factory=dict)  # codigo -> {doc, ...}
    indefinidos: dict = field(default_factory=dict)       # codigo -> motivo
    linhas_por_cliente: dict = field(default_factory=dict)
    sem_codigo: int = 0

    def status(self, codigo: str) -> str:
        return self.por_cliente.get(codigo, SEM_ACORDO)

    def protege(self, codigo: str, doc) -> bool:
        """Este título está coberto por um acordo VIGENTE do cliente?

        Duas condições, separadas de propósito: o acordo do cliente tem de
        estar vigente (PAGANDO) E o título tem de constar nele. Uma parcela
        quitada continua constando, porque quitar parcela é cumprir o acordo.
        """
        if self.status(codigo) != PAGANDO:
            return False
        d = _doc(doc)
        return bool(d) and d in self.titulos_do_acordo.get(codigo, set())

    def resumo(self) -> str:
        cont = {}
        for s in self.por_cliente.values():
            cont[s] = cont.get(s, 0) + 1
        L = [f"{len(self.por_cliente)} cliente(s) com acordo registrado."]
        for st in (QUEBRADO, INDEFINIDO, PAGANDO, QUITADO):
            if cont.get(st):
                L.append(f"   {st}: {cont[st]}")
        if self.indefinidos:
            L.append("   INDEFINIDO segura o cliente — confira estes:")
            for c, m in sorted(self.indefinidos.items())[:8]:
                L.append(f"      {c}: {m}")
        vigentes = sum(len(v) for c, v in self.titulos_do_acordo.items()
                       if self.status(c) == PAGANDO)
        L.append(f"   {vigentes} título(s) sob acordo vigente, "
                 f"{sum(len(v) for v in self.titulos_do_acordo.values())} no total")
        if self.sem_codigo:
            L.append(f"   {self.sem_codigo} linha(s) sem código extraível do campo EMPRESA")
        return "\n".join(L)


def carregar(gc, io_sheets, spreadsheet_id: str = ACORDOS_SPREADSHEET_ID,
             aba: str = ACORDOS_ABA) -> Acordos:
    """Lê a aba ACORDOS e agrega por cliente."""
    planilha = io_sheets.abrir(gc, spreadsheet_id)
    # O cabeçalho não está na linha 1: acima dele há uma linha de totais.
    linhas = io_sheets.ler_aba_procurando_cabecalho(
        planilha, aba, obrigatorias=["EMPRESA", "STATUS ACORDO", "DOC"])
    if not linhas:
        raise RuntimeError(f"Aba '{aba}' sem registros abaixo do cabeçalho.")

    a = Acordos()
    for l in linhas:
        empresa = l.get("empresa") or l.get("EMPRESA") or ""
        codigo = extrair_codigo(empresa)
        if not codigo:
            if str(empresa).strip():
                a.sem_codigo += 1
            continue

        st = _status_da_linha(l.get("status_acordo") or l.get("STATUS ACORDO"))
        a.linhas_por_cliente.setdefault(codigo, []).append(st)

        # Registra o título INDEPENDENTE do status da linha. Se o acordo do
        # cliente estiver vigente, ele cobre o conjunto todo — inclusive as
        # parcelas que ele já pagou.
        d = _doc(l.get("doc") or l.get("DOC"))
        if d:
            a.titulos_do_acordo.setdefault(codigo, set()).add(d)

        # Não classificável = segura. Cobre o caso conhecido (data em branco
        # para firmar acordo novo por cima) e qualquer estado futuro, sem
        # precisar adivinhar a intenção por trás de um campo vazio.
        pago_em = str(l.get("data_de_pagamento") or l.get("DATA DE PAGAMENTO") or "").strip()
        bruto = str(l.get("status_acordo") or l.get("STATUS ACORDO") or "").strip()
        if st is None and bruto:
            a.indefinidos[codigo] = f"status desconhecido: {bruto!r}"
        elif st is None and not pago_em:
            a.indefinidos[codigo] = "sem status e sem data de pagamento"
        elif st != QUITADO and not pago_em:
            a.indefinidos[codigo] = f"{st} sem data de pagamento"

    for codigo, estados in a.linhas_por_cliente.items():
        # Direção segura: quebrado vence tudo. Um cliente que quebrou o acordo
        # e depois quitou uma parcela solta continua sendo caso de cobrança.
        # Quebrado tem precedência sobre indefinido: descumprir acordo é fato
        # registrado, não ambiguidade.
        if QUEBRADO in estados:
            a.por_cliente[codigo] = QUEBRADO
        elif codigo in a.indefinidos:
            a.por_cliente[codigo] = INDEFINIDO
        elif PAGANDO in estados:
            a.por_cliente[codigo] = PAGANDO
        elif QUITADO in estados:
            a.por_cliente[codigo] = QUITADO

    return a


def aplicar(df, acordos: Acordos, docs_por_cliente: dict | None = None):
    """Anota o DataFrame de candidatos e devolve (elegiveis, excluidos).

    Sai SÓ quem tem acordo vigente cobrindo TODOS os títulos vencidos.
      quebrado     -> fica, marcado
      quitado      -> fica (o acordo encerrou)
      pagando      -> sai apenas se nenhum título dele estiver fora do acordo
      indefinido   -> sai e é reportado (estado não classificável)

    docs_por_cliente: {codigo: [doc, ...]} dos títulos vencidos. Sem isso, cai
    no grão do cliente — mais grosseiro, e sinalizado na coluna.
    """
    df = df.copy()
    df["status_acordo"] = df.codigo.map(acordos.status)

    def _coberto(codigo):
        if acordos.status(codigo) != PAGANDO:
            return False
        if not docs_por_cliente:
            return True                      # sem os DOCs, protege o cliente todo
        docs = [d for d in (docs_por_cliente.get(codigo) or []) if str(d).strip()]
        return bool(docs) and all(acordos.protege(codigo, d) for d in docs)

    df["coberto_por_acordo"] = df.codigo.map(_coberto)
    # Indefinido protege o cliente inteiro: não há acordo legível para casar
    # título a título, e disparar sobre estado desconhecido é o erro caro.
    df["indefinido"] = df.status_acordo == INDEFINIDO

    fora = df.coberto_por_acordo | df.indefinido
    return df[~fora], df[fora]
