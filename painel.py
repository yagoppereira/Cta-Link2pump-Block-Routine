"""
Painel da régua de bloqueio: onde cada cliente está, agora.

A etapa NÃO é digitada. É derivada de três sinais que já existem:

  log de envio      diz se o aviso saiu, e quando
  saldo hoje        diz se pagou (releitura do BigQuery na reconciliação)
  serial na view    gold.bombas_alocadas some quando o bloqueio é CONCLUÍDO,
                    e volta quando é desfeito

Derivar em vez de digitar importa porque campo de status preenchido à mão
envelhece: alguém bloqueia, ninguém atualiza a planilha, e o painel passa a
mentir. Aqui o painel não tem como divergir do que aconteceu.

ETAPAS

  NA FILA        entrou na régua, aviso ainda não saiu
  AVISADO        aviso enviado, dentro do prazo
  PRAZO VENCIDO  passou da data e o saldo não mudou
  PAGOU          saldo zerou
  PAGOU EM PARTE saldo caiu mas não zerou -> decisão humana
  BLOQUEADO      os equipamentos sumiram da view
  DESBLOQUEADO   voltaram para a view depois de terem sumido
  SEM AVISO      estava na régua e o aviso não saiu (sem destinatário, falha)

O caso que mais importa é SEM AVISO. É o cliente que seria bloqueado sem ter
sido comunicado, e é o único que não se resolve sozinho com o tempo.
"""

from dataclasses import dataclass
from datetime import date

NA_FILA = "NA FILA"
AVISADO = "AVISADO"
PRAZO_VENCIDO = "PRAZO VENCIDO"
PAGOU = "PAGOU"
PAGOU_EM_PARTE = "PAGOU EM PARTE"
BLOQUEADO = "BLOQUEADO"
DESBLOQUEADO = "DESBLOQUEADO"
SEM_AVISO = "SEM AVISO"

# Ordem de exibição: o que exige ação primeiro.
ORDEM = [SEM_AVISO, PRAZO_VENCIDO, PAGOU_EM_PARTE, BLOQUEADO, DESBLOQUEADO,
         AVISADO, NA_FILA, PAGOU]

CORES = {
    SEM_AVISO:      {"red": 0.98, "green": 0.85, "blue": 0.82},   # vermelho claro
    PRAZO_VENCIDO:  {"red": 1.00, "green": 0.93, "blue": 0.78},   # âmbar
    PAGOU_EM_PARTE: {"red": 1.00, "green": 0.95, "blue": 0.85},
    BLOQUEADO:      {"red": 0.90, "green": 0.90, "blue": 0.94},
    DESBLOQUEADO:   {"red": 0.88, "green": 0.94, "blue": 0.99},
    AVISADO:        {"red": 0.95, "green": 0.97, "blue": 0.95},
    NA_FILA:        {"red": 1.00, "green": 1.00, "blue": 1.00},
    PAGOU:          {"red": 0.88, "green": 0.97, "blue": 0.93},   # verde
}


@dataclass
class Estado:
    etapa: str
    detalhe: str = ""


def calcular_etapa(*, avisado_em=None, falha_envio: str = "",
                   prazo: date = None, hoje: date = None,
                   saldo_no_aviso: float = 0.0, saldo_hoje: float = None,
                   seriais_no_aviso: set = None, seriais_hoje: set = None) -> Estado:
    """Deriva a etapa. Ordem de precedência deliberada: primeiro o que é fato
    consumado (bloqueado, pagou), depois o que é pendência."""
    hoje = hoje or date.today()
    seriais_no_aviso = seriais_no_aviso or set()

    if not avisado_em:
        if falha_envio:
            return Estado(SEM_AVISO, falha_envio)
        return Estado(NA_FILA)

    # Fato consumado tem precedência: o serial sumir da view é a confirmação de
    # que o bloqueio foi executado, independente de status em qualquer lugar.
    #
    # MAS SÓ SE OS SERIAIS DE HOJE FORAM CONSULTADOS. `seriais_hoje=None`
    # significa "não fui olhar", e antes virava set() — aí todo serial do aviso
    # "sumia" e a campanha inteira aparecia como BLOQUEADA no dia do envio,
    # com prazo ainda correndo. Ausência de informação não é informação: o
    # mesmo tratamento que `saldo_hoje` já tinha.
    if seriais_no_aviso and seriais_hoje is not None:
        sumiram = seriais_no_aviso - seriais_hoje
        if sumiram:
            return Estado(BLOQUEADO,
                          f"{len(sumiram)} de {len(seriais_no_aviso)} equipamento(s)")

    if saldo_hoje is not None:
        if saldo_hoje <= 0:
            return Estado(PAGOU, f"quitou {saldo_no_aviso:.2f}")
        if saldo_hoje < saldo_no_aviso:
            pago = saldo_no_aviso - saldo_hoje
            return Estado(PAGOU_EM_PARTE,
                          f"pagou {pago:.2f}, restam {saldo_hoje:.2f}")

    if prazo and hoje > prazo:
        return Estado(PRAZO_VENCIDO, f"venceu em {prazo:%d/%m}")

    return Estado(AVISADO, f"prazo até {prazo:%d/%m}" if prazo else "")


def _numero(valor) -> float:
    """Converte para float aceitando pt-BR e americano.

    A Campanha_Previa é lida do Sheets, que devolve "4.148,76" com vírgula
    decimal — `float()` direto estoura com ValueError. Remover o ponto
    cegamente também não serve: "64.80" viraria 6480.

    A vírgula decide. Se ela existe, o ponto é separador de milhar; se não
    existe, o ponto é o decimal.
    """
    import re as _re

    bruto = str(valor if valor not in (None, "") else 0).strip()
    # Tira "R$", espaço fino e qualquer coisa que não seja dígito, ponto,
    # vírgula ou sinal. Sem isso "R$ 1.011,55" caía em 0.0 em silêncio, e
    # saldo zero num painel de cobrança parece cliente que quitou.
    bruto = _re.sub(r"[^\d,.\-]", "", bruto)
    if not bruto:
        return 0.0
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        return float(bruto)
    except ValueError:
        print(f"  AVISO: '{valor}' não é número; tratando como 0")
        return 0.0


def montar(previa: list, log: list, campanha,
           saldo_hoje: dict = None, seriais_hoje: set = None,
           bombas_snapshot: list = None, hoje: date = None) -> list:
    """Uma linha por cliente da campanha, com a etapa calculada.

    previa:          aba Campanha_Previa
    log:             aba Campanha_Log
    saldo_hoje:      {codigo: saldo} da reconciliação. None = ainda não rodou
    seriais_hoje:    seriais presentes hoje em gold.bombas_alocadas
    bombas_snapshot: aba Campanha_Bombas (o que foi comunicado)
    """
    hoje = hoje or date.today()

    envio_por_cliente = {}
    for l in log:
        if l.get("id_campanha") != campanha.id_campanha:
            continue
        cod = l.get("codigo_cliente")
        atual = envio_por_cliente.get(cod)
        # Última linha do log vence: uma tentativa que falhou e depois deu
        # certo tem que aparecer como enviada.
        if atual is None or str(l.get("timestamp", "")) >= str(atual.get("timestamp", "")):
            envio_por_cliente[cod] = l

    seriais_por_cliente = {}
    for b in (bombas_snapshot or []):
        s = b.get("serial_equipamento")
        if s not in (None, ""):
            seriais_por_cliente.setdefault(b.get("codigo_cliente"), set()).add(str(s))

    linhas = []
    for p in previa:
        cod = p.get("codigo_cliente")
        env = envio_por_cliente.get(cod, {})
        enviado = env.get("status") == "ENVIADO"

        estado = calcular_etapa(
            avisado_em=env.get("timestamp") if enviado else None,
            falha_envio=("" if enviado else (env.get("erro") or env.get("status") or "")),
            prazo=campanha.data_limite,
            hoje=hoje,
            saldo_no_aviso=_numero(p.get("total")),
            saldo_hoje=(saldo_hoje or {}).get(cod) if saldo_hoje is not None else None,
            seriais_no_aviso=seriais_por_cliente.get(cod, set()),
            seriais_hoje=seriais_hoje,
        )

        linhas.append({
            "id_campanha": campanha.id_campanha,
            "etapa": estado.etapa,
            "detalhe": estado.detalhe,
            "codigo_cliente": cod,
            "cliente": p.get("nome_cliente", ""),
            "cnpj": p.get("cnpj_cpf", ""),
            "vendedor": p.get("vendedor", ""),
            "titulos": p.get("titulos", ""),
            "saldo_no_aviso": p.get("total", ""),
            "saldo_hoje": (saldo_hoje or {}).get(cod, "") if saldo_hoje is not None else "",
            "bombas": p.get("bombas", ""),
            "avisado_em": (env.get("timestamp") or "")[:16].replace("T", " "),
            "prazo": f"{campanha.data_limite:%d/%m/%Y}",
            "destinatarios": p.get("destinatarios", ""),
        })

    pos = {e: i for i, e in enumerate(ORDEM)}
    return sorted(linhas, key=lambda x: (pos.get(x["etapa"], 99), x["cliente"]))


def resumo(linhas: list) -> str:
    cont = {}
    for l in linhas:
        cont[l["etapa"]] = cont.get(l["etapa"], 0) + 1
    L = [f"{len(linhas)} cliente(s) na régua:"]
    for e in ORDEM:
        if cont.get(e):
            L.append(f"   {e:<15} {cont[e]}")
    if cont.get(SEM_AVISO):
        L.append("")
        L.append(f"ATENÇÃO: {cont[SEM_AVISO]} cliente(s) na régua sem aviso enviado. "
                 f"Bloquear qualquer um deles é bloqueio sem comunicação.")
    return "\n".join(L)


def escrever(planilha, linhas: list, io_sheets, nome_aba: str = "Painel_Bloqueio"):
    """Escreve o painel com cor por etapa."""
    import pandas as pd

    df = pd.DataFrame(linhas)
    ws = io_sheets.escrever_aba(planilha, nome_aba, df)
    if df.empty:
        return ws

    io_sheets.com_retry(ws.format, "D2:F", {"numberFormat": {"type": "TEXT"}})

    inicio = 2
    for etapa in ORDEM:
        qtd = int((df.etapa == etapa).sum())
        if qtd:
            io_sheets.com_retry(
                ws.format, f"B{inicio}:C{inicio + qtd - 1}",
                {"backgroundColor": CORES[etapa],
                 "textFormat": {"bold": etapa in (SEM_AVISO, PRAZO_VENCIDO)}})
            inicio += qtd

    print(resumo(linhas))
    return ws
