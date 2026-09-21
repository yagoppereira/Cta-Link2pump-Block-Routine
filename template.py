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

# ASSINATURA REAL, colada do Gmail.
#
# A assinatura do Gmail NÃO viaja por SMTP: ela é aplicada pelo cliente web na
# hora de compor, não pelo servidor. Enviando por smtplib, ela simplesmente
# não existe — por isso a versão fabricada abaixo.
#
# Para usar a sua de verdade: Gmail → Configurações → Ver todas as
# configurações → Assinatura. Selecione o conteúdo, copie, e cole o HTML aqui.
# (Para pegar o HTML: componha um e-mail com a assinatura, envie para você
# mesmo, abra, três pontos → "Mostrar original" e copie o trecho da assinatura.)
#
# CUIDADO COM IMAGEM: assinatura do Gmail costuma referenciar imagem hospedada
# no Google com URL autenticada. Ela aparece para você e vira quadrado vazio
# para o cliente. Se a sua tiver logo, hospede a imagem numa URL pública.
#
# Vazio = usa a assinatura montada abaixo, com o logo embutido.
ASSINATURA_HTML = ""

# Logo da assinatura, EMBUTIDO na mensagem (não hospedado).
#
# Hospedar não resolve: URL `raw` de repositório privado do GitHub pede
# autenticação e o cliente vê quadrado vazio. Link do Drive também falha na
# maioria dos clientes. E a URL que o Gmail usa na assinatura dele é
# autenticada — aparece para você e não para quem recebe.
#
# A saída é anexar a imagem como parte inline e referenciá-la por cid:. Como
# somos nós que montamos o MIME, isso funciona em qualquer cliente e não
# depende de nada externo. Aponte LOGO_PATH para o arquivo no repositório.
LOGO_CID = "logo_ctasmart"
LOGO_PATH = "logo_ctasmart.png"     # relativo à raiz do repositório

CORPO_HTML = """<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1a1a2e;line-height:1.6;max-width:860px">
<p>Prezado Cliente,</p>
<p>Identificamos que sua empresa mantém o uso ativo de nossos serviços; no entanto,
constam em nosso sistema débitos residuais pendentes de regularização.</p>
{destaque}
<p>Nosso objetivo é evitar qualquer impacto em suas operações. Por isso, apresentamos
abaixo o quadro de cobranças em aberto e estamos à disposição para definirmos,
juntos, a melhor forma de quitação:</p>
{quadro}
<p>Informamos que, para mantermos a continuidade do fornecimento, a regularização deve
ocorrer até o dia <strong>{data_limite}</strong>. Após este prazo, o acesso ao serviço
poderá ser temporariamente suspenso até que resolvam as pendências.</p>
<p>Aguardamos seu retorno via WhatsApp {whatsapp} ou respondendo a este e-mail.</p>
<p>Atenciosamente,</p>
{assinatura}
</div>"""

ASSINATURA_PADRAO = """<table cellpadding="0" cellspacing="0" role="presentation"
 style="border-collapse:collapse;margin-top:20px;border-top:1px solid #d3d1c7">
<tr>
<td style="padding:14px 18px 0 0;vertical-align:middle">{logo}</td>
<td style="padding:14px 0 0;vertical-align:middle;font-family:Arial,Helvetica,sans-serif">
<div style="font-size:15px;font-weight:bold;color:#1a1a2e">{remetente_nome}</div>
<div style="font-size:13px;color:#5c5c6b">{remetente_cargo}</div>
<div style="font-size:13px"><a href="https://{site}" style="color:#382fd8">{site}</a></div>
</td>
</tr></table>"""


def quadro_destaque(titulos: list, campanha) -> str:
    """Total e prazo no topo, antes da tabela.

    O total estava só no rodapé, depois de 37 linhas — quem abre o e-mail no
    celular rola tudo antes de descobrir quanto deve. Aqui é a primeira coisa
    que aparece depois do parágrafo de abertura.

    Montado com <table> e não com div/flex: Outlook ignora flexbox, e o bloco
    apareceria empilhado e sem alinhamento.
    """
    total = sum(float(t.get("total") or 0) for t in titulos)
    saldo = sum(float(t.get("saldo") or 0) for t in titulos)
    encargos = total - saldo

    celula = ("padding:14px 18px;background:#1a1a2e;color:#ffffff;"
              "font-family:Arial,Helvetica,sans-serif;vertical-align:middle")
    rotulo = "font-size:11px;color:#19e098;letter-spacing:.5px;text-transform:uppercase"

    return (
        '<table cellpadding="0" cellspacing="0" role="presentation" '
        'style="border-collapse:collapse;width:100%;margin:18px 0">'
        f'<tr>'
        f'<td style="{celula}">'
        f'<div style="{rotulo}">Total a regularizar</div>'
        f'<div style="font-size:30px;font-weight:bold;line-height:1.2;'
        f'white-space:nowrap">{brl(total)}</div>'
        f'<div style="font-size:11px;color:#a9b0c0">'
        f'{brl(saldo)} de principal + {brl(encargos)} de juros e multa</div>'
        f'</td>'
        f'<td style="{celula};text-align:right;border-left:1px solid #3a3a52">'
        f'<div style="{rotulo}">Regularizar até</div>'
        f'<div style="font-size:22px;font-weight:bold;line-height:1.2;'
        f'white-space:nowrap">{_dma(campanha.data_limite)}</div>'
        f'<div style="font-size:11px;color:#a9b0c0">'
        f'{len(titulos)} título(s) em aberto</div>'
        f'</td>'
        f'</tr></table>'
    )


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
    # COLUNAS CONDICIONAIS. Canal só distingue algo quando um equipamento
    # atende mais de uma bomba (o mesmo serial aparece duas vezes). Observação
    # só é preenchida quando a bomba está em operação de terceiro. Fora desses
    # casos, as duas viram colunas de valor único ocupando espaço — foi o que
    # aconteceu no Barbosa Mello, cinco bombas todas canal 1 e sem terceiro.
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

    seriais_repetidos = {}
    for b in bombas:
        s = b.get("serial_equipamento")
        seriais_repetidos[s] = seriais_repetidos.get(s, 0) + 1
    mostrar_canal = any(n > 1 for n in seriais_repetidos.values())
    mostrar_obs = any(b.get("instalada_em_terceiro") for b in bombas)

    linhas = []
    for i, b in enumerate(bombas):
        fundo = "#ffffff" if i % 2 == 0 else "#f7f7f4"
        nome = limpar_nome_bomba(b.get("bomba_nome"), nome_cliente)
        celulas = [f'<td style="{td}">{nome}</td>']
        if mostrar_canal:
            celulas.append(f'<td style="{td};text-align:center">'
                           f'{b.get("canal") or "—"}</td>')
        celulas.append(f'<td style="{td};text-align:center">'
                       f'{b.get("serial_equipamento") or "—"}</td>')
        if mostrar_obs:
            obs = (f'operação de {b.get("local_nome") or "terceiro"}'
                   if b.get("instalada_em_terceiro") else "—")
            celulas.append(f'<td style="{td};font-size:12px;color:#6b7280">{obs}</td>')
        linhas.append(f'<tr style="background:{fundo}">' + "".join(celulas) + "</tr>")

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
        + (f'<th style="{th};text-align:center">Canal</th>' if mostrar_canal else "")
        + f'<th style="{th};text-align:center">Série</th>'
        + (f'<th style="{th};text-align:left">Observação</th>' if mostrar_obs else "")
        + '</tr></thead>'
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

# COLUNAS QUE SAÍRAM, e por quê:
#
#   NFS-e  o número real da nota de serviço é o campo NF_SERVICO do relatório
#          do CIGAM (ex.: 202400000016587) e ele NÃO é ingerido no warehouse —
#          procurei em todos os campos do lançamento. O que a query devolve em
#          `nf` é igual ao `fatura`, então a coluna repetia o documento em
#          todas as linhas. Melhor não ter do que ter errado.
#
#   Tipo   `tipo_cobranca` só existe na gold.inadimplencia, que saiu do
#          circuito. Vinha "—" em 100% das linhas.
#
# ENTROU `Contrato`: é o que dá sentido à repetição. No Barbosa Mello, oito
# linhas de R$ 720 no mesmo vencimento parecem duplicata e são os oito
# contratos dele.
#
# O `doc` também é mais curto que no relatório do CIGAM: lá aparece
# "20261095/" ou "200099586/1", com a parcela colada. Esse sufixo também não
# está no DW.
COLUNA_NFSE = ("nfse", "NFS-e", "left")

COLUNAS = [
    ("doc", "Documento", "left"),
    ("tipo_pendencia", "Tipo", "left"),
    ("dataVencimento", "Vencimento", "center"),
    ("dias_atraso", "Atraso", "center"),
    ("saldo", "Saldo", "right"),
    ("encargos", "Encargos", "right"),
    ("total", "Total", "right"),
]


def _rotulo_contrato(v) -> str:
    v = str(v or "").strip().lstrip("0")
    return f"Contrato {v}" if v else "Sem contrato"


def montar_quadro(titulos: list, data_envio, mostrar_cnpj: bool = False) -> str:
    """Tabela HTML dos títulos.

    mostrar_cnpj: quando o e-mail cobre mais de um cadastro do mesmo grupo, o
    cliente precisa saber qual CNPJ deve o quê — a cobrança é centralizada,
    mas o débito continua sendo de cada inscrição.
    """
    colunas = list(COLUNAS)
    # NFS-e só entra quando ALGUM título tem o número. O campo NF_SERVICO do
    # CIGAM não existe em nenhuma tabela do DW (procurei em
    # lancamentos_enriquecidos e em cigam__notas_fiscais), então ele chega por
    # ponte: a aba NFSe_Titulos, colada do export do CIGAM.
    # Sem a ponte a coluna não aparece — em vez de repetir o documento, que foi
    # o que aconteceu antes e passou por número de nota em 37 linhas.
    # Só mostra se houver NFS-e de verdade — ou seja, diferente do documento.
    # Trava dupla: se algum dia outra fonte voltar a preencher `nfse` com a
    # fatura, a coluna continua fora em vez de repetir o documento.
    if any(str(t.get("nfse") or "").strip()
           and str(t.get("nfse")).strip() != str(t.get("doc") or "").strip()
           for t in titulos):
        colunas.insert(1, COLUNA_NFSE)
    if mostrar_cnpj:
        colunas = [COLUNA_CNPJ] + colunas
    th = ('padding:8px 10px;background:#1a1a2e;color:#19e098;'
          'font-weight:bold;font-size:12px;border:1px solid #1a1a2e;'
          'white-space:nowrap')
    td = ('padding:7px 10px;border:1px solid #d3d1c7;font-size:13px;'
          'white-space:nowrap')

    cab = "".join(
        f'<th style="{th};text-align:{al}">{rot}</th>' for _, rot, al in colunas
    )

    linhas = []
    # Agrupado por contrato. Sem isso, oito parcelas de R$ 720 no mesmo
    # vencimento parecem duplicata — e são oito contratos distintos. O subtotal
    # por contrato é o que o cliente confere contra o controle dele.
    # Separação de contrato DISCRETA. O número do contrato interessa mais à CTA
    # do que ao cliente: a versão anterior usava barra cinza, negrito e
    # subtotal no mesmo peso das linhas de valor, e o quadro ficava bagunçado.
    # Agora é um filete com texto pequeno e apagado — separa sem competir.
    tg = ('padding:10px 10px 3px;border:none;border-top:1px solid #e6e4dc;'
          'font-size:11px;color:#8b8b8b;letter-spacing:.3px')

    grupos: dict = {}
    for t in titulos:
        grupos.setdefault(_rotulo_contrato(t.get("codigoContrato")), []).append(t)

    def _ordem(item):
        rotulo, ts = item
        # Sem contrato por último; entre os demais, o maior débito primeiro.
        return (rotulo == "Sem contrato", -sum(float(x.get("total") or 0) for x in ts))

    i = 0
    for rotulo, ts in sorted(grupos.items(), key=_ordem):
        if len(grupos) > 1:
            sub = sum(float(x.get("total") or 0) for x in ts)
            linhas.append(
                f'<tr><td style="{tg}" colspan="{len(colunas) - 1}">'
                f'{rotulo} · {len(ts)} título(s)</td>'
                f'<td style="{tg};text-align:right;white-space:nowrap">'
                f'{brl(sub)}</td></tr>')
        for t in sorted(ts, key=lambda x: str(x.get("dataVencimento"))):
            fundo = "#ffffff" if i % 2 == 0 else "#f7f7f4"
            i += 1
            celulas = []
            for campo, _, al in colunas:
                v = t.get(campo)
                if campo == "dataVencimento":
                    txt = _dma(v)
                elif campo == "dias_atraso":
                    txt = "a vencer" if not v else f"{v} dias"
                elif campo in ("saldo", "encargos", "total"):
                    txt = brl(v or 0)
                elif campo == "codigoContrato":
                    txt = str(v or "").strip().lstrip("0") or "—"
                else:
                    txt = "—" if v in (None, "") else str(v)
                celulas.append(f'<td style="{td};text-align:{al}">{txt}</td>')
            linhas.append(f'<tr style="background:{fundo}">' + "".join(celulas) + "</tr>")

    soma_saldo = sum(float(t.get("saldo") or 0) for t in titulos)
    soma_enc = sum(float(t.get("encargos") or 0) for t in titulos)
    soma_tot = sum(float(t.get("total") or 0) for t in titulos)

    # Total do rodapé SÓBRIO. A faixa verde sólida competia com o destaque do
    # topo, que já mostra o valor em 30px — dois pontos focais na mesma peça.
    # Aqui basta fechar a tabela: linha dupla em cima, fundo branco, negrito.
    tf = ('padding:12px 10px 10px;border:none;border-top:2px solid #1a1a2e;'
          'font-weight:bold;font-size:14px;color:#1a1a2e;white-space:nowrap')
    rodape = (
        f'<tr>'
        f'<td style="{tf};text-align:right" colspan="{len(colunas) - 3}">'
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
    # Com máscara. O assunto dos 38 e-mails da 2026-09-B saiu com o número
    # cru e sem o zero à esquerda — "7636657001241" em vez de
    # "07.636.657/0012-41". O cliente confere o CNPJ para saber se o aviso é
    # dele; número deslocado gera dúvida em vez de identificação.
    import pipefy as _pipefy
    cnpjs = [_pipefy.formatar_documento(c.get("cnpj_cpf"))
             for c in clientes if c.get("cnpj_cpf")]
    assunto = ASSUNTO.format(
        razao_social=clientes[0].get("nome_cliente", ""),
        cnpj=" / ".join(cnpjs) if len(cnpjs) <= 2 else f"{len(cnpjs)} CNPJs",
    )

    logo = (f'<img src="cid:{LOGO_CID}" alt="Cta Smart" '
            f'width="150" style="display:block;border:0">' if LOGO_PATH else "")
    assinatura = ASSINATURA_HTML.strip() or ASSINATURA_PADRAO.format(
        logo=logo, remetente_nome=REMETENTE_NOME,
        remetente_cargo=REMETENTE_CARGO, site=SITE)

    html = CORPO_HTML.format(
        assinatura=assinatura,
        destaque=quadro_destaque(titulos, campanha),
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


# ============================================================================
# E-MAIL PARA GESTORES — formato interno, não é cobrança
# ============================================================================
#
# Deliberadamente diferente do aviso ao cliente. Aquele é um documento formal
# que pede pagamento; este é um relatório operacional que pede revisão. Usar o
# mesmo template faria o gestor ler como se fosse cobrança dirigida a ele, e
# esconderia o que importa: a evidência por trás de cada entrada e os clientes
# que foram retirados.

CORPO_GESTOR = """<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1a1a2e;line-height:1.55;max-width:900px">
<p>{saudacao},</p>
<p>Segue a relação de clientes da sua equipe na régua de bloqueio por
inadimplência da campanha <strong>{id_campanha}</strong>.</p>
{destaque}
<p>O aviso de cobrança é enviado a estes clientes em seguida.
<strong>O bloqueio ocorre a partir de {data_limite}</strong>, caso a pendência
não seja regularizada até lá.</p>
<p>Se você tiver informação sobre algum dos clientes abaixo — negociação em
andamento, cobrança indevida, contato já feito — avise o financeiro.</p>
{blocos}
<p style="font-size:12px;color:#6b7280;margin-top:22px">
Critério desta campanha: {criterio}<br>
Dados posicionados em {data_envio}. Cliente em acordo vigente, em cobrança
jurídica ou em recuperação judicial não entra na régua automaticamente.</p>
{assinatura}
</div>"""


def _destaque_gestor(cobrados: list, vetados: list, bloqueio: str = "") -> str:
    total = sum(float(l.get("total") or 0) for l in cobrados)
    celula = ("padding:12px 16px;background:#1a1a2e;color:#ffffff;"
              "font-family:Arial,Helvetica,sans-serif;vertical-align:middle")
    rot = "font-size:11px;color:#19e098;letter-spacing:.5px;text-transform:uppercase"
    cel = (f'<td style="{celula}"><div style="{rot}">Na régua</div>'
           f'<div style="font-size:26px;font-weight:bold;line-height:1.2">'
           f'{len(cobrados)}</div>'
           f'<div style="font-size:11px;color:#a9b0c0">cliente(s)</div></td>'
           f'<td style="{celula};border-left:1px solid #3a3a52">'
           f'<div style="{rot}">Total em aberto</div>'
           f'<div style="font-size:26px;font-weight:bold;line-height:1.2;'
           f'white-space:nowrap">{brl(total)}</div></td>')
    if bloqueio:
        cel += (f'<td style="{celula};border-left:1px solid #3a3a52">'
                f'<div style="{rot}">Bloqueio a partir de</div>'
                f'<div style="font-size:26px;font-weight:bold;line-height:1.2;'
                f'white-space:nowrap">{bloqueio}</div></td>')
    if vetados:
        cel += (f'<td style="{celula};border-left:1px solid #3a3a52">'
                f'<div style="{rot}">Já retirados</div>'
                f'<div style="font-size:26px;font-weight:bold;line-height:1.2">'
                f'{len(vetados)}</div>'
                f'<div style="font-size:11px;color:#a9b0c0">por decisão</div></td>')
    return ('<table cellpadding="0" cellspacing="0" role="presentation" '
            'style="border-collapse:collapse;width:100%;margin:16px 0">'
            f'<tr>{cel}</tr></table>')


def _bloco_vendedor(vendedor: str, itens: list, vetado: bool = False) -> str:
    th = ('padding:7px 9px;background:#f0efe9;color:#1a1a2e;font-weight:bold;'
          'font-size:11px;border:1px solid #d3d1c7;text-align:left')
    td = 'padding:7px 9px;border:1px solid #d3d1c7;font-size:12px'
    cor = "#8b8b8b" if vetado else "#1a1a2e"

    linhas = []
    for l in itens:
        extra = (f'<div style="font-size:11px;color:#b45309">retirado: '
                 f'{l.get("motivo_veto")}</div>' if vetado else "")

        # Parque do cliente. O gestor pediu, e muda a leitura: 1 bomba parada
        # e 14 bombas paradas são conversas diferentes com o mesmo cliente.
        eq = l.get("equipamentos")
        if eq not in (None, "", 0):
            seriais = str(l.get("seriais") or "")
            curto = (seriais if len(seriais) <= 70 else seriais[:67] + "...")
            extra += (f'<div style="font-size:11px;color:#374151;margin-top:3px">'
                      f'{eq} equipamento(s)'
                      + (f' — {curto}' if curto else "") + '</div>')
        if l.get("em_terceiro"):
            extra += (f'<div style="font-size:11px;color:#b45309">'
                      f'em operação de terceiro: {l["em_terceiro"]}</div>')
        if l.get("sistema_com_adimplente"):
            extra += (f'<div style="font-size:11px;color:#b45309">'
                      f'sistema compartilhado com {l["sistema_com_adimplente"]} '
                      f'bomba(s) de pagante em dia — bloqueio só da bomba</div>')
        res = l.get("resultado") or ""
        marca = (f' <span style="font-size:11px;color:#059669">[{res}]</span>'
                 if res else "")
        linhas.append(
            f'<tr>'
            f'<td style="{td}"><strong>{l.get("cliente")}</strong>{marca}'
            f'<div style="font-size:11px;color:#6b7280">'
            f'{l.get("por_que_entrou")}</div>{extra}</td>'
            f'<td style="{td};text-align:right;white-space:nowrap">'
            f'{brl(float(l.get("total") or 0))}</td>'
            f'<td style="{td};text-align:center;white-space:nowrap">'
            f'{l.get("devendo_desde") or "—"}</td>'
            f'</tr>')

    titulo = f'{vendedor} — {len(itens)} cliente(s)'
    return (f'<p style="margin:18px 0 6px;font-weight:bold;color:{cor}">{titulo}</p>'
            '<table cellpadding="0" cellspacing="0" role="presentation" '
            'style="border-collapse:collapse;width:100%">'
            f'<thead><tr><th style="{th}">Cliente e motivo</th>'
            f'<th style="{th};text-align:right">Em aberto</th>'
            f'<th style="{th};text-align:center">Devendo desde</th></tr></thead>'
            f'<tbody>{"".join(linhas)}</tbody></table>')


def montar_email_gestor(gerente: str, linhas: list, campanha,
                        criterio: str = "", prazo_revisao=None) -> dict:
    """Relatório do gestor: quem entrou, por quê, e quem já foi retirado.

    NÃO pede revisão nem dá prazo ao gestor: o aviso ao cliente sai em
    seguida. O que ele informa é a data do BLOQUEIO, e o pedido é que
    qualquer informação sobre os clientes chegue ao financeiro — negociação em
    curso, cobrança indevida, contato já feito.

    A diferença não é de texto. Prazo de revisão criava a expectativa de que
    a lista ficaria parada esperando resposta, e ela não fica.

    Os vetados vão junto de propósito. Sem eles o gestor vê só o que foi
    cobrado e reage àquilo; com eles, vê que a régua também poupou clientes da
    equipe e por quais motivos — e a conversa passa a ser sobre o critério.

    prazo_revisao: aceito e ignorado, para não quebrar chamadas antigas.
    """
    cobrados = [l for l in linhas if l.get("situacao") == "cobrado"]
    vetados = [l for l in linhas if l.get("situacao") == "VETADO"]

    blocos = []
    for vend in sorted({l["vendedor"] for l in cobrados}):
        blocos.append(_bloco_vendedor(
            vend, sorted([l for l in cobrados if l["vendedor"] == vend],
                         key=lambda x: -float(x.get("total") or 0))))
    if vetados:
        blocos.append('<p style="margin:26px 0 4px;font-weight:bold;'
                      'color:#8b8b8b">NÃO entraram — retirados por decisão</p>')
        for vend in sorted({l["vendedor"] for l in vetados}):
            blocos.append(_bloco_vendedor(
                vend, [l for l in vetados if l["vendedor"] == vend], vetado=True))

    logo = (f'<img src="cid:{LOGO_CID}" alt="Cta Smart" width="150" '
            f'style="display:block;border:0">' if LOGO_PATH else "")
    assinatura = ASSINATURA_HTML.strip() or ASSINATURA_PADRAO.format(
        logo=logo, remetente_nome=REMETENTE_NOME,
        remetente_cargo=REMETENTE_CARGO, site=SITE)

    html = CORPO_GESTOR.format(
        saudacao=f"Olá, {gerente}" if gerente else "Olá",
        id_campanha=campanha.id_campanha,
        destaque=_destaque_gestor(cobrados, vetados,
                                  _dma(campanha.data_limite)),
        data_limite=_dma(campanha.data_limite),
        blocos="".join(blocos) or "<p>Nenhum cliente da sua equipe na régua.</p>",
        criterio=criterio,
        data_envio=_dma(campanha.data_envio),
        assinatura=assinatura,
    )
    texto = "\n".join(
        [f"{gerente} — régua de bloqueio {campanha.id_campanha}", "",
         f"{len(cobrados)} cliente(s) na régua, "
         f"{brl(sum(float(l.get('total') or 0) for l in cobrados))}.",
         f"Bloqueio a partir de {_dma(campanha.data_limite)}. "
         f"Informação sobre algum cliente: avise o financeiro.", ""]
        + [f"  {l['vendedor']} | {l['cliente']} | "
           f"{brl(float(l.get('total') or 0))} | {l.get('por_que_entrou')}"
           for l in cobrados])

    return {
        "assunto": (f"Régua de bloqueio {campanha.id_campanha} — "
                    f"{len(cobrados)} cliente(s) da sua equipe"),
        "html": html, "texto": texto,
        "cobrados": len(cobrados), "vetados": len(vetados),
    }
