"""
Corpo do e-mail de aviso de bloqueio.

Porte do modelo que o Yago já usava. O texto está isolado nas constantes
ASSUNTO / CORPO_HTML para ser editado sem mexer na montagem do quadro.

DUAS DATAS, DOIS PAPÉIS — não troque:
  data_envio   rotula o quadro ("posição em DD/MM"). Base dos juros.
  data_limite  aparece no texto como prazo de regularização.

O quadro NÃO promete quitação. O valor é a posição do dia; o acordo é
negociado no retorno do cliente. Por isso "Total em <data>" e nunca
"Valor para quitação".

Nenhuma taxa é declarada no texto. O juro diário é arredondado a centavo antes
de multiplicar pelos dias (ver encargos.py), o que dá efetivo ~1,04% a.m. em
vez de 1,00%. Escrever "juros de 1% ao mês" ao lado de um número que não
corresponde a isso é criar contestação de graça.
"""

from datetime import date

ASSUNTO = ("Aviso importante: Regularização de acesso e pendências financeiras "
           "– Cta Smart - {razao_social} - {cnpj}")

REMETENTE_NOME = "Yago Gutterres"
REMETENTE_CARGO = "Analista de Cobrança"
WHATSAPP = "(51) 99888-0734"
SITE = "www.ctasmart.com.br"

CORPO_HTML = """<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1a1a2e;line-height:1.6;max-width:720px">
<p>Prezado Cliente,</p>
<p>Identificamos que sua empresa mantém o uso ativo de nossos serviços; no entanto,
constam em nosso sistema débitos residuais pendentes de regularização.</p>
<p>Nosso objetivo é evitar qualquer impacto em suas operações. Por isso, apresentamos
abaixo o quadro de cobranças em aberto e estamos à disposição para definirmos,
juntos, a melhor forma de quitação:</p>
{quadro}
<p>Informamos que, para mantermos a continuidade do fornecimento, a regularização deve
ocorrer até o dia <strong>{data_limite}</strong>. Após este prazo, o acesso ao serviço
poderá ser temporariamente suspenso até que resolvam as pendências.</p>
<p>Aguardamos seu retorno via WhatsApp {whatsapp} ou respondendo a este e-mail.</p>
<p>Atenciosamente,</p>
<p style="margin-top:18px;border-top:1px solid #d3d1c7;padding-top:12px">
<strong>{remetente_nome}</strong><br>
{remetente_cargo}<br>
<a href="https://{site}" style="color:#382fd8">{site}</a>
</p>
</div>"""


def limpar_nome_bomba(bomba_nome: str, nome_cliente: str) -> str:
    """O bomba_nome do DW repete a razão social do cliente em toda linha:
    ' AUTOTRANS TRANSPORTES URBANOS E RODOVIARIOS LTDA - Sarzedo/MG S500 1'.
    Num quadro de 19 bombas isso é 19 vezes o mesmo prefixo. Removemos o nome
    do próprio cliente e sobra o que identifica a bomba: 'Sarzedo/MG S500 1'.
    """
    import re as _re
    bruto = _re.sub(r"\s+", " ", str(bomba_nome or "")).strip()
    if not bruto:
        return "(sem nome)"

    def _norm(x):
        return _re.sub(r"[^A-Z0-9]", "", str(x or "").upper())

    alvo = _norm(nome_cliente)
    # Corta o prefixo token a token enquanto ele ainda é parte do nome do
    # cliente. Não usa startswith direto porque o cadastro abrevia
    # ("RODOVIARIOS" vs "RODOVIARIOS LTDA") e a comparação exata falharia.
    tokens = bruto.split(" ")
    corte = 0
    acumulado = ""
    for i, tk in enumerate(tokens):
        if tk == "-":
            continue
        acumulado += _norm(tk)
        if alvo.startswith(acumulado) and acumulado:
            corte = i + 1
        else:
            break

    resto = " ".join(tokens[corte:]).strip(" -")
    return resto or bruto


def quadro_equipamentos(bombas: list, nome_cliente: str) -> str:
    """Quadro dos equipamentos vinculados ao contrato.

    Fala de VÍNCULO, não de localização física: alocacao_atualizada_em mede
    edição de registro, não verificação em campo (16,6% da view tem a data da
    carga inicial). Terceiro só aparece quando é outra EMPRESA — outra filial
    do mesmo grupo, medida por raiz de CNPJ, não vira destaque.
    """
    if not bombas:
        return ""

    th = ('padding:8px 10px;background:#1a1a2e;color:#19e098;'
          'font-weight:bold;font-size:12px;border:1px solid #1a1a2e')
    td = 'padding:7px 10px;border:1px solid #d3d1c7;font-size:13px'

    linhas = []
    for i, b in enumerate(bombas):
        fundo = "#ffffff" if i % 2 == 0 else "#f7f7f4"
        nome = limpar_nome_bomba(b.get("bomba_nome"), nome_cliente)
        obs = ""
        if b.get("instalada_em_terceiro"):
            obs = f'operação de {b.get("local_nome") or "terceiro"}'
        linhas.append(
            f'<tr style="background:{fundo}">'
            f'<td style="{td}">{nome}</td>'
            f'<td style="{td};text-align:center">{b.get("canal") or "—"}</td>'
            f'<td style="{td};text-align:center">{b.get("serial_equipamento") or "—"}</td>'
            f'<td style="{td};font-size:12px;color:#6b7280">{obs or "—"}</td>'
            f'</tr>'
        )

    n_eq = len({b.get("serial_equipamento") for b in bombas
                if b.get("serial_equipamento") is not None})
    n_terceiro = sum(1 for b in bombas if b.get("instalada_em_terceiro"))

    nota = (f'{len(bombas)} bomba(s) em {n_eq} equipamento(s).')
    if n_terceiro:
        nota += (f' {n_terceiro} atende(m) operação de outra empresa, que não '
                 f'foi notificada e também seria afetada.')

    return (
        '<p style="margin:20px 0 6px"><strong>Equipamentos vinculados ao seu '
        'contrato</strong></p>'
        '<table cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;margin-bottom:6px">'
        f'<thead><tr><th style="{th};text-align:left">Bomba</th>'
        f'<th style="{th};text-align:center">Canal</th>'
        f'<th style="{th};text-align:center">Série</th>'
        f'<th style="{th};text-align:left">Observação</th></tr></thead>'
        f'<tbody>{"".join(linhas)}</tbody></table>'
        f'<p style="font-size:12px;color:#6b7280;margin:0 0 16px">{nota}</p>'
    )


def brl(valor) -> str:
    """1234.5 -> 'R$ 1.234,50'"""
    s = f"{float(valor):,.2f}"
    return "R$ " + s.replace(",", "@").replace(".", ",").replace("@", ".")


def _dma(d) -> str:
    return d.strftime("%d/%m/%Y") if isinstance(d, date) else str(d)


COLUNA_CNPJ = ("cnpj_cpf", "CNPJ", "left")

COLUNAS = [
    ("doc", "Documento", "left"),
    ("nfse", "NFS-e", "left"),
    ("tipo_cobranca", "Tipo", "left"),
    ("dataVencimento", "Vencimento", "center"),
    ("dias_atraso", "Atraso", "center"),
    ("saldo", "Saldo", "right"),
    ("encargos", "Juros e multa", "right"),
    ("total", "Total", "right"),
]


def montar_quadro(titulos: list, data_envio, mostrar_cnpj: bool = False) -> str:
    """Tabela HTML dos títulos.

    mostrar_cnpj: quando o e-mail cobre mais de um cadastro do mesmo grupo, o
    cliente precisa saber qual CNPJ deve o quê — a cobrança é centralizada,
    mas o débito continua sendo de cada inscrição.
    """
    colunas = ([COLUNA_CNPJ] + COLUNAS) if mostrar_cnpj else COLUNAS
    th = ('padding:8px 10px;background:#1a1a2e;color:#19e098;'
          'font-weight:bold;font-size:12px;border:1px solid #1a1a2e')
    td = 'padding:7px 10px;border:1px solid #d3d1c7;font-size:13px'

    cab = "".join(
        f'<th style="{th};text-align:{al}">{rot}</th>' for _, rot, al in colunas
    )

    linhas = []
    for i, t in enumerate(titulos):
        fundo = "#ffffff" if i % 2 == 0 else "#f7f7f4"
        celulas = []
        for campo, _, al in colunas:
            v = t.get(campo)
            if campo == "dataVencimento":
                txt = _dma(v)
            elif campo == "dias_atraso":
                txt = "a vencer" if not v else f"{v} dias"
            elif campo in ("saldo", "encargos", "total"):
                txt = brl(v or 0)
            else:
                txt = "—" if v in (None, "") else str(v)
            celulas.append(f'<td style="{td};text-align:{al}">{txt}</td>')
        linhas.append(f'<tr style="background:{fundo}">' + "".join(celulas) + "</tr>")

    soma_saldo = sum(float(t.get("saldo") or 0) for t in titulos)
    soma_enc = sum(float(t.get("encargos") or 0) for t in titulos)
    soma_tot = sum(float(t.get("total") or 0) for t in titulos)

    tf = ('padding:9px 10px;border:1px solid #d3d1c7;background:#e1f5ee;'
          'font-weight:bold;font-size:13px')
    rodape = (
        f'<tr>'
        f'<td style="{tf};text-align:right" colspan="{5 + (1 if mostrar_cnpj else 0)}">'
        f'Total ({len(titulos)} título(s))</td>'
        f'<td style="{tf};text-align:right">{brl(soma_saldo)}</td>'
        f'<td style="{tf};text-align:right">{brl(soma_enc)}</td>'
        f'<td style="{tf};text-align:right">{brl(soma_tot)}</td>'
        f'</tr>'
    )

    return (
        '<table cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;margin:16px 0">'
        f'<thead><tr>{cab}</tr></thead>'
        f'<tbody>{"".join(linhas)}{rodape}</tbody>'
        '</table>'
        f'<p style="font-size:12px;color:#6b7280;margin:4px 0 16px">'
        f'Valores calculados na posição de {_dma(data_envio)}. '
        f'Encargos seguem incidindo até a data do pagamento ou do acordo.</p>'
    )


def montar_email(cliente, titulos: list, campanha) -> dict:
    """Devolve {'assunto', 'html', 'texto'}.

    cliente: um dict, ou uma LISTA de dicts quando o e-mail cobre vários
             cadastros do mesmo grupo. A trava de raiz de CNPJ no agrupamento
             garante que todos são da mesma empresa, então a razão social do
             assunto é a mesma — o que varia é a inscrição.
    """
    clientes = cliente if isinstance(cliente, (list, tuple)) else [cliente]
    if not clientes:
        raise ValueError("nenhum cliente para montar o e-mail.")
    if not titulos:
        raise ValueError(
            f"{clientes[0].get('codigo_cliente')}: nenhum título — "
            f"não se manda aviso de bloqueio com quadro vazio."
        )

    varios = len(clientes) > 1
    cnpjs = [c.get("cnpj_cpf", "") for c in clientes if c.get("cnpj_cpf")]
    assunto = ASSUNTO.format(
        razao_social=clientes[0].get("nome_cliente", ""),
        cnpj=" / ".join(cnpjs) if len(cnpjs) <= 2 else f"{len(cnpjs)} CNPJs",
    )

    html = CORPO_HTML.format(
        quadro=montar_quadro(titulos, campanha.data_envio, mostrar_cnpj=varios),
        data_limite=_dma(campanha.data_limite),
        whatsapp=WHATSAPP,
        remetente_nome=REMETENTE_NOME,
        remetente_cargo=REMETENTE_CARGO,
        site=SITE,
    )

    return {"assunto": assunto, "html": html,
            "texto": _versao_texto(titulos, campanha, mostrar_cnpj=varios)}


def _versao_texto(titulos: list, campanha, mostrar_cnpj: bool = False) -> str:
    """Alternativa em texto puro (multipart/alternative). Cliente de e-mail que
    bloqueia HTML mostra isto em vez de uma mensagem em branco."""
    linhas = [
        "Prezado Cliente,",
        "",
        "Identificamos que sua empresa mantem o uso ativo de nossos servicos; no",
        "entanto, constam em nosso sistema debitos residuais pendentes de",
        "regularizacao.",
        "",
        f"QUADRO DE COBRANCAS - posicao de {_dma(campanha.data_envio)}",
        "",
    ]
    for t in titulos:
        atraso = "a vencer" if not t.get("dias_atraso") else f"{t['dias_atraso']} dias"
        pref = f"  [{t.get('cnpj_cpf') or '-'}] " if mostrar_cnpj else "  "
        linhas.append(
            f"{pref}Doc {t.get('doc') or '-'} | venc {_dma(t.get('dataVencimento'))} | "
            f"{atraso} | saldo {brl(t.get('saldo') or 0)} | "
            f"encargos {brl(t.get('encargos') or 0)} | total {brl(t.get('total') or 0)}"
        )
    total = sum(float(t.get("total") or 0) for t in titulos)
    linhas += [
        "",
        f"  TOTAL ({len(titulos)} titulo(s)): {brl(total)}",
        "",
        f"A regularizacao deve ocorrer até o dia {_dma(campanha.data_limite)}. Apos este",
        "prazo, o acesso ao servico podera ser temporariamente suspenso.",
        "",
        f"Retorno via WhatsApp {WHATSAPP} ou respondendo a este e-mail.",
        "",
        "Atenciosamente,",
        f"{REMETENTE_NOME} - {REMETENTE_CARGO}",
        SITE,
    ]
    return "\n".join(linhas)
