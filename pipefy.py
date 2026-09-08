import re
"""
Conteúdo do card de bloqueio no Pipefy.

O card é MANUAL: alguém do suporte lê e executa. Este módulo só monta o texto;
a abertura do card sai por fora (API GraphQL do Pipefy, n8n ou Zapier).

UM CARD POR CLIENTE, bombas em texto nas Observações. Decidido assim porque o
processo é humano: 19 cards para a AUTOTRANS seriam 19 filas para tratar o que
é um único pedido.

O NOME DA BOMBA AQUI É O CRU, NÃO O LIMPO — ao contrário do e-mail.
  e-mail   -> quem lê é o cliente, quer legibilidade -> limpar_nome_bomba()
  card     -> quem lê é o suporte, precisa LOCALIZAR a bomba no sistema CTA
              -> nome exatamente como está lá, com prefixo e tudo
O campo se chama "Nome da Bomba no Sistema CTA". Ele tem que casar com o
sistema, não ficar bonito.

A FONTE É O SNAPSHOT DA CAMPANHA, NUNCA UMA CONSULTA NOVA.
Serial some de gold.bombas_alocadas quando o bloqueio é concluído. Se o card
relesse o DW, um cliente com bloqueio parcial anterior geraria pedido de menos
bombas do que o aviso comunicou — e você teria avisado sobre equipamento que
não pediu para bloquear.
"""

from datetime import date

# ATENÇÃO: confirme os rótulos exatos no campo "Tipo de solicitação" do pipe.
# O primeiro veio do card real; o segundo é suposição e precisa casar com a
# opção existente, senão a criação do card falha na validação do Pipefy.
TIPO_SISTEMA_E_BOMBA = "Bloqueio do sistema e da bomba"
TIPO_SO_BOMBA = "Bloqueio da bomba"

MOTIVO = "Bloqueio devido inadimplência."


def _dma(d) -> str:
    return d.strftime("%d/%m/%Y") if isinstance(d, date) else str(d)


def _brl(v) -> str:
    return "R$ " + f"{float(v):,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


def montar_observacoes(bombas_snapshot: list,
                       campanha=None,
                       total_devido=None,
                       incluir_contexto: bool = True,
                       grupo=None) -> str:
    """Texto das Observações, no formato que o suporte já lê hoje.

    bombas_snapshot: linhas CONGELADAS na campanha, com bomba_nome (cru) e
                     serial_equipamento.
    """
    linhas = [MOTIVO]

    if incluir_contexto and campanha is not None:
        linhas.append("")
        ctx = f"Aviso enviado em {_dma(campanha.data_envio)}, prazo até {_dma(campanha.data_limite)}"
        if getattr(campanha, "id_campanha", None):
            ctx += f" (campanha {campanha.id_campanha})"
        linhas.append(ctx + ".")
        if total_devido is not None:
            linhas.append(f"Total em aberto na data do aviso: {_brl(total_devido)}.")

    linhas += ["", "Nome da bomba - Serial:"]

    vistos = set()
    for b in bombas_snapshot:
        nome = str(b.get("bomba_nome") or "").strip()
        serial = b.get("serial_equipamento")
        # Duas bombas do mesmo equipamento compartilham serial (canal 1 e 2).
        # O bloqueio é do equipamento, então a linha repetida seria ruído para
        # quem executa.
        if serial in vistos:
            continue
        vistos.add(serial)
        linhas.append(f"{nome} - {serial}")

    # Conflito de sistema: cortar o cliente_id derrubaria quem está em dia.
    if grupo is not None and not grupo.bloqueio_de_sistema_seguro:
        linhas += [
            "",
            "*** NAO BLOQUEAR O SISTEMA - BLOQUEAR APENAS OS SERIAIS ACIMA ***",
            f"O(s) cliente(s) de sistema {', '.join(grupo.cliente_ids)} tem "
            f"{grupo.bombas_de_adimplentes} bomba(s) de pagante ADIMPLENTE.",
            "Bloquear o acesso derrubaria operacao de quem esta em dia:",
        ]
        linhas += [f"- {n}" for n in grupo.terceiros_adimplentes[:6]]
        if len(grupo.terceiros_adimplentes) > 6:
            linhas.append(f"- (+{len(grupo.terceiros_adimplentes) - 6} outro(s))")

    terceiros = sorted({
        b.get("local_nome") for b in bombas_snapshot if b.get("instalada_em_terceiro")
    } - {None})
    if terceiros:
        linhas += [
            "",
            "ATENCAO: equipamento(s) em operacao de terceiro, que nao foi notificado "
            "e tambem sera afetado:",
        ]
        linhas += [f"- {t}" for t in terceiros]

    return "\n".join(linhas)


def formatar_documento(valor) -> str:
    """CNPJ/CPF com máscara, restaurando o zero à esquerda.

    O Sheets come o zero quando o valor entra como número: 07.636.657/0012-41
    chega como 7636657001241, com 13 dígitos. Sem o zero-padding a máscara
    sai deslocada, e o card vai para o suporte com CNPJ que não existe.

    14 dígitos -> CNPJ, 11 -> CPF. Comprimentos intermediários são o zero
    perdido; acima ou abaixo, devolve como está em vez de inventar formato.
    """
    d = re.sub(r"\D", "", str(valor or ""))
    if not d:
        return ""
    if 11 < len(d) <= 14:
        d = d.zfill(14)
    elif 8 < len(d) <= 11:
        d = d.zfill(11)

    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return str(valor or "")


def montar_card(cliente: dict, bombas_snapshot: list, campanha=None,
                total_devido=None, grupo=None) -> dict:
    """Payload do card. Os campos de valor único ficam vazios de propósito:
    são um-para-um e o cliente tem N bombas — a lista mora nas Observações.

    grupo: agrupamento.Grupo. Quando o bloqueio de sistema é inseguro, o tipo
    de solicitação muda para bloqueio só da bomba. Não é preferência: cortar o
    cliente_id compartilhado derruba operação de quem pagou em dia.
    """
    if not bombas_snapshot:
        raise ValueError(
            f"{cliente.get('codigo_cliente')}: snapshot sem bomba. Abrir card de "
            f"bloqueio sem equipamento a bloquear é pedido vazio para o suporte."
        )

    inseguro = grupo is not None and not grupo.bloqueio_de_sistema_seguro

    return {
        "titulo": cliente.get("nome_cliente", ""),
        "cnpj": formatar_documento(cliente.get("cnpj_cpf")),
        "tipo_solicitacao": TIPO_SO_BOMBA if inseguro else TIPO_SISTEMA_E_BOMBA,
        "serial_da_placa": "",
        "nome_da_bomba_sistema_cta": "",
        "observacoes": montar_observacoes(
            bombas_snapshot, campanha=campanha, total_devido=total_devido, grupo=grupo
        ),
        "_bloqueio_de_sistema_seguro": not inseguro,
        "_codigo_cliente": cliente.get("codigo_cliente"),
        "_qtd_equipamentos": len({b.get("serial_equipamento") for b in bombas_snapshot}),
    }


def detectar_bloqueios_concluidos(bombas_snapshot: list, seriais_no_dw: set) -> dict:
    """Serial some de gold.bombas_alocadas quando o bloqueio é concluído.
    Comparar o snapshot com a view de hoje diz o que já foi executado, sem
    consultar o Pipefy nem pedir retorno para ninguém."""
    do_snapshot = {b.get("serial_equipamento") for b in bombas_snapshot
                   if b.get("serial_equipamento") is not None}
    concluidos = do_snapshot - seriais_no_dw
    return {
        "total": len(do_snapshot),
        "concluidos": sorted(concluidos),
        "pendentes": sorted(do_snapshot & seriais_no_dw),
        "pct_concluido": round(100 * len(concluidos) / len(do_snapshot), 1) if do_snapshot else 0.0,
    }
