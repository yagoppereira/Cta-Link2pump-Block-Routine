"""
Ingestão da lista de inadimplência e montagem da aba Campanha_Input.

A lista chega no formato da planilha "Frequência de Vencimento":

    Cliente | CNPJ | Serial | Frequência (Meses) | Ainda Usa Equipamento |
    Renegociado | Ultima Ultilização | Observação | Recorrente | Em atraso |
    Data referência de valores

Três colunas dela decidem quem NÃO recebe aviso, e nenhuma existia no pipeline:

  Renegociado = SIM        acordo fechado. Mandar aviso de bloqueio para quem
                           renegociou é o erro mais caro do processo inteiro.
  Observação               log manual da campanha anterior: "ACORDO FEITO",
                           "EM NEGOCIAÇÃO", "NOTIFICAÇÃO EXTRAJUDICIAL",
                           "PAGAMENTO RECEBIDO", "CONTRATO CIGAM ENCERRADO".
  Ainda Usa Equipamento    NÃO significa que não há o que bloquear.

DOIS PESOS DELIBERADOS
Campo estruturado (Renegociado) é regra dura: exclui.
Texto livre (Observação) é só sinalização: manda para CONFERIR, nunca decide
sozinho. Busca por palavra-chave em texto escrito à mão erra — "não houve
acordo" casa com "acordo". Um humano lê antes.

A aba resultante é a superfície de trabalho: você cola a lista, roda, e a
própria aba mostra em cor o que está pronto, o que precisa de olho e o que
está fora, com o motivo escrito ao lado.
"""

import re
import unicodedata
from dataclasses import dataclass, field

# Cores da empresa
COR_ESCURA = "#1a1a2e"
COR_DESTAQUE = "#19e098"

PRONTO = "PRONTO"
CONFERIR = "CONFERIR"
FORA = "FORA"

# Sinais no texto livre. Cada um é um MOTIVO diferente para não disparar —
# por isso a mensagem acompanha o termo, em vez de um "excluído" genérico.
SINAIS = [
    ("ACORDO",            "acordo mencionado na observação"),
    ("NEGOCIA",           "negociação mencionada"),          # negociação, negociado, renegociação
    ("EXTRAJUDICIAL",     "jurídico assumiu"),
    ("JURIDIC",           "jurídico assumiu"),
    ("PAGAMENTO RECEBIDO", "pagamento registrado"),
    ("DESBLOQUEAD",       "já foi desbloqueado"),
    ("CONTRATO CIGAM ENCERRADO", "contrato encerrado"),
    ("SOLICITACAO DE BLOQUEIO", "bloqueio já solicitado"),
    ("RECUPERACAO JUDICIAL", "recuperação judicial"),
    ("FALENCIA",          "falência"),
]


def _texto(v) -> str:
    s = str(v if v is not None else "").replace("\u00a0", " ")
    if s.strip().lower() in ("nan", "none", "null", "<na>"):
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def _sim_nao(v) -> str | None:
    t = _texto(v).upper()
    if t in ("SIM", "S", "TRUE", "VERDADEIRO", "1"):
        return "SIM"
    if t in ("NAO", "N", "FALSE", "FALSO", "0"):
        return "NAO"
    return None


def normalizar_documento(v) -> str:
    """'19.758.842/0003-05' -> '19758842000305'. Reconstrói zero à esquerda:
    o Sheets converte CNPJ para número e come os zeros da frente."""
    d = re.sub(r"\D", "", str(v or "").split(".0")[0] if isinstance(v, float) else str(v or ""))
    if not d:
        return ""
    return d.zfill(14) if len(d) > 11 else d.zfill(11)


@dataclass
class Linha:
    cliente: str = ""
    documento: str = ""
    serial: str = ""
    frequencia: str = ""
    usa_equipamento: str = ""
    renegociado: str = ""
    observacao: str = ""
    em_atraso: str = ""
    status: str = PRONTO
    motivo: str = ""

    def como_dict(self) -> dict:
        return {
            "status": self.status,
            "motivo": self.motivo,
            "cliente": self.cliente,
            "cnpj": self.documento,
            "renegociado": self.renegociado,
            "usa_equipamento": self.usa_equipamento,
            "freq_meses": self.frequencia,
            "em_atraso": self.em_atraso,
            "serial": self.serial,
            "observacao": self.observacao,
        }


@dataclass
class Ingestao:
    linhas: list = field(default_factory=list)

    def por_status(self, s: str) -> list:
        return [l for l in self.linhas if l.status == s]

    def documentos_prontos(self) -> list:
        vistos, saida = set(), []
        for l in self.por_status(PRONTO):
            if l.documento and l.documento not in vistos:
                vistos.add(l.documento)
                saida.append(l.documento)
        return saida

    def resumo(self) -> str:
        L = [f"{len(self.linhas)} linha(s) na lista recebida."]
        for s in (PRONTO, CONFERIR, FORA):
            n = len(self.por_status(s))
            if n:
                L.append(f"  {s}: {n}")
        fora = self.por_status(FORA) + self.por_status(CONFERIR)
        if fora:
            por_motivo = {}
            for l in fora:
                por_motivo.setdefault(l.motivo, []).append(l.cliente)
            L.append("")
            for motivo, clientes in sorted(por_motivo.items(), key=lambda x: -len(x[1])):
                L.append(f"  {motivo}: {len(clientes)}")
                for c in clientes[:3]:
                    L.append(f"     {c[:46]}")
                if len(clientes) > 3:
                    L.append(f"     (+{len(clientes) - 3})")
        L.append("")
        L.append(f"{len(self.documentos_prontos())} documento(s) distinto(s) prontos "
                 f"para o preparar().")
        return "\n".join(L)


def _campo(linha: dict, *nomes):
    """Casa nome de coluna ignorando acento, caixa e espaço — o cabeçalho da
    lista recebida varia ('Ultima Ultilização' tem typo na origem)."""
    normalizados = {re.sub(r"[^a-z]", "", _texto(k).lower()): v for k, v in linha.items()}
    for n in nomes:
        chave = re.sub(r"[^a-z]", "", n.lower())
        if normalizados.get(chave) not in (None, ""):
            return normalizados[chave]
    return ""


def consolidar_por_documento(lista: list) -> list:
    """A lista chega por EQUIPAMENTO; a campanha é por CLIENTE.

    O Orlando Bertoldi aparece duas vezes com o mesmo CNPJ: uma linha da
    Viação Mercês com 'usa equipamento = NÃO' e outra com 'SIM'. Classificando
    linha a linha, o mesmo cliente sai em dois status contraditórios.

    Agregação, cada campo na direção segura:
      renegociado      qualquer SIM  -> SIM   (na dúvida, não dispara)
      usa_equipamento  qualquer SIM  -> SIM   (uma bomba aposentada não
                                               significa cliente sem operação)
      observação       tudo junto, sem repetir
    """
    grupos = {}
    for bruta in lista:
        doc = normalizar_documento(_campo(bruta, "cnpj", "cnpjcpf", "documento"))
        chave = doc or f"__sem_doc_{len(grupos)}"
        grupos.setdefault(chave, []).append(bruta)

    saida = []
    for doc, linhas in grupos.items():
        reneg = [_sim_nao(_campo(l, "renegociado")) for l in linhas]
        usa = [_sim_nao(_campo(l, "aindausaequipamento", "usaequipamento")) for l in linhas]

        obs, vistas = [], set()
        for l in linhas:
            for parte in _texto(_campo(l, "observacao", "obs")).split(". "):
                p = parte.strip()
                if p and p.upper() not in vistas:
                    vistas.add(p.upper())
                    obs.append(p)

        seriais = [_texto(_campo(l, "serial")) for l in linhas]
        nomes = [_texto(_campo(l, "cliente", "nome", "empresa")) for l in linhas]

        saida.append({
            "cliente": max((n for n in nomes if n), key=len, default=""),
            "cnpj": doc,
            "serial": "; ".join(sorted({s for s in seriais if s})),
            "frequencia": _texto(_campo(linhas[0], "frequenciameses", "frequencia")),
            "aindausaequipamento": "SIM" if "SIM" in usa else ("NAO" if "NAO" in usa else ""),
            "renegociado": "SIM" if "SIM" in reneg else ("NAO" if "NAO" in reneg else ""),
            "observacao": " | ".join(obs),
            "ematraso": _texto(_campo(linhas[0], "ematraso", "atraso")),
            "_linhas_originais": len(linhas),
        })
    return saida


def ingerir(lista: list, consolidar: bool = True) -> Ingestao:
    """Lê a lista recebida e classifica cada cliente em PRONTO / CONFERIR / FORA."""
    ing = Ingestao()
    if consolidar:
        lista = consolidar_por_documento(lista)

    for bruta in lista:
        l = Linha(
            cliente=_texto(_campo(bruta, "cliente", "nome", "empresa")),
            documento=normalizar_documento(_campo(bruta, "cnpj", "cnpjcpf", "documento")),
            serial=_texto(_campo(bruta, "serial")),
            frequencia=_texto(_campo(bruta, "frequenciameses", "frequencia")),
            observacao=_texto(_campo(bruta, "observacao", "obs")),
            em_atraso=_texto(_campo(bruta, "ematraso", "atraso")),
        )
        l.renegociado = _sim_nao(_campo(bruta, "renegociado")) or ""
        l.usa_equipamento = _sim_nao(_campo(bruta, "aindausaequipamento",
                                            "usaequipamento")) or ""

        if not l.documento:
            l.status, l.motivo = FORA, "sem CNPJ/CPF na linha"
        elif l.renegociado == "SIM":
            # Campo estruturado: regra dura.
            l.status, l.motivo = FORA, "renegociado — acordo já fechado"
        else:
            # Texto livre: só sinaliza. Busca por palavra-chave em texto escrito
            # à mão erra ("não houve acordo" casa com "acordo"), então quem
            # decide é a pessoa.
            obs = l.observacao.upper()
            achados = [msg for termo, msg in SINAIS if termo in obs]
            if achados:
                l.status = CONFERIR
                l.motivo = achados[0]
            elif l.usa_equipamento == "NAO":
                l.status = CONFERIR
                l.motivo = "não usa mais o equipamento — nada a bloquear"

        ing.linhas.append(l)

    return ing


def escrever_campanha_input(planilha, ingestao: Ingestao, io_sheets,
                            nome_aba: str = "Campanha_Input"):
    """Monta a aba com cor por status. Verde pronto, âmbar confere, cinza fora."""
    import pandas as pd

    df = pd.DataFrame([l.como_dict() for l in ingestao.linhas])
    ordem = {PRONTO: 0, CONFERIR: 1, FORA: 2}
    df = df.sort_values(by="status", key=lambda s: s.map(ordem)).reset_index(drop=True)

    ws = io_sheets.escrever_aba(planilha, nome_aba, df)
    n = len(df)
    if n == 0:
        return ws

    io_sheets.com_retry(ws.format, "C2:C", {"numberFormat": {"type": "TEXT"}})  # cnpj
    io_sheets.com_retry(ws.format, f"J2:J{n + 1}", {"wrapStrategy": "WRAP"})     # observação

    cores = {
        PRONTO:   {"red": 0.88, "green": 0.97, "blue": 0.93},
        CONFERIR: {"red": 1.00, "green": 0.95, "blue": 0.80},
        FORA:     {"red": 0.93, "green": 0.93, "blue": 0.93},
    }
    inicio = 2
    for status in (PRONTO, CONFERIR, FORA):
        qtd = int((df.status == status).sum())
        if qtd:
            io_sheets.com_retry(ws.format, f"A{inicio}:B{inicio + qtd - 1}",
                                {"backgroundColor": cores[status],
                                 "textFormat": {"bold": status != FORA}})
            inicio += qtd

    print(f"'{nome_aba}': {n} linha(s) — "
          + ", ".join(f"{int((df.status == s).sum())} {s}"
                      for s in (PRONTO, CONFERIR, FORA)))
    return ws
