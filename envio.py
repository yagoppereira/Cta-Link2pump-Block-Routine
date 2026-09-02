"""
Disparo do aviso de bloqueio.

TRÊS PROTEÇÕES, e nenhuma é opcional:

  1. redirecionar_para  Todos os destinatários viram um só endereço. O assunto
                        recebe prefixo [TESTE] e o corpo um aviso no topo com
                        os destinatários REAIS que teriam recebido. É assim que
                        se testa sem descobrir do jeito ruim.

  2. teto_diario        Conta Gmail comum corta em 500 destinatários/dia,
                        Workspace em 2.000. Ao bater o teto o loop PARA e a
                        fila fica pendente; a execução seguinte continua.

  3. log por envio      Gravado a cada mensagem, nunca no fim. Runtime do Colab
                        cai; sem isso, reexecutar manda tudo de novo (aviso
                        duplicado) ou nada (cliente bloqueado sem aviso).

A função de envio é INJETADA (`enviar_fn`). A lógica de fila é testável com um
stub; o SMTP real entra por fora. Não há credencial neste módulo.
"""

from dataclasses import dataclass, field
from datetime import datetime
import time


@dataclass
class Job:
    """Uma mensagem a enviar. `chave` é o que o log usa para idempotência."""
    codigo_cliente: str
    nome_cliente: str
    para: list                      # destinatários reais (cobrança do cliente)
    cc: list                        # vendedor + cópias fixas
    assunto: str
    html: str
    texto: str

    @property
    def chave(self) -> str:
        return self.codigo_cliente

    @property
    def n_destinatarios(self) -> int:
        return len(self.para) + len(self.cc)


@dataclass
class Resultado:
    enviados: list = field(default_factory=list)
    pulados: list = field(default_factory=list)     # (chave, motivo)
    falhas: list = field(default_factory=list)      # (chave, erro)
    pendentes: list = field(default_factory=list)   # não couberam no teto
    destinatarios_usados: int = 0      # o que saiu de fato
    destinatarios_producao: int = 0    # o que sairia sem redirecionamento

    def resumo(self) -> str:
        L = [f"{len(self.enviados)} enviado(s), {self.destinatarios_usados} destinatário(s) "
             f"(em produção seriam {self.destinatarios_producao})."]
        if self.pulados:
            L.append(f"{len(self.pulados)} pulado(s):")
            for k, m in self.pulados[:10]:
                L.append(f"   {k}: {m}")
        if self.falhas:
            L.append(f"{len(self.falhas)} FALHA(S) — reexecutar retoma daqui:")
            for k, e in self.falhas[:10]:
                L.append(f"   {k}: {e}")
        if self.pendentes:
            L.append(f"{len(self.pendentes)} na fila para a próxima execução "
                     f"(teto diário atingido): {', '.join(self.pendentes[:8])}"
                     + (" ..." if len(self.pendentes) > 8 else ""))
        return "\n".join(L)


AVISO_TESTE = (
    '<div style="border:2px solid #d85a30;background:#faece7;padding:12px;'
    'margin-bottom:18px;font-family:Arial,sans-serif;font-size:13px;color:#712b13">'
    '<strong>MODO TESTE — esta mensagem NÃO foi para o cliente.</strong><br>'
    'Destinatários reais: {para}<br>Cópia real: {cc}'
    '</div>'
)


def aplicar_redirecionamento(job: Job, redirecionar_para: str) -> Job:
    """Reescreve o job para ir a um único endereço, preservando no corpo quem
    teria recebido. Sem isso o teste não vale nada: você olharia um e-mail que
    não mostra para onde iria."""
    aviso = AVISO_TESTE.format(
        para=", ".join(job.para) or "(nenhum)",
        cc=", ".join(job.cc) or "(nenhum)",
    )
    cabecalho_txt = (
        f"*** MODO TESTE - nao foi para o cliente ***\n"
        f"Destinatarios reais: {', '.join(job.para) or '(nenhum)'}\n"
        f"Copia real: {', '.join(job.cc) or '(nenhum)'}\n"
        f"{'-' * 60}\n\n"
    )
    return Job(
        codigo_cliente=job.codigo_cliente,
        nome_cliente=job.nome_cliente,
        para=[redirecionar_para],
        cc=[],
        assunto=f"[TESTE {job.codigo_cliente}] {job.assunto}",
        html=aviso + job.html,
        texto=cabecalho_txt + job.texto,
    )


def disparar(fila: list,
             campanha,
             enviar_fn,
             log_fn,
             chaves_ja_enviadas: set | None = None,
             pausa_segundos: float = 1.5) -> Resultado:
    """Percorre a fila enviando e registrando uma linha de log por mensagem.

    enviar_fn(job) -> None. Levanta exceção em caso de falha.
    log_fn(dict)   -> None. Grava UMA linha. Chamado ANTES do próximo envio.
    chaves_ja_enviadas: lido do log da campanha; permite retomar.
    """
    r = Resultado()
    ja = set(chaves_ja_enviadas or ())
    campanha.checar_pode_enviar()          # recusa produção não confirmada
    redirecionar = campanha.redirecionar_para   # campo declarado, não getattr
    print(f"modo: {campanha.modo}"
          + (f" -> tudo para {redirecionar}" if redirecionar else ""))

    for job in fila:
        if job.chave in ja:
            r.pulados.append((job.chave, "já enviado nesta campanha"))
            continue

        # Pula SEMPRE, inclusive em modo teste. Se o redirecionamento
        # preenchesse o destinatário aqui, o teste marcaria como enviado
        # justamente o cliente que em produção seria pulado — e esse é o caso
        # que mais precisa de ação humana (bloqueio sem aviso).
        if not job.para:
            r.pulados.append((job.chave, "SEM DESTINATARIO — seria bloqueio sem aviso"))
            continue

        efetivo = aplicar_redirecionamento(job, redirecionar) if redirecionar else job

        # O teto conta o que REALMENTE sai (1 endereço em modo teste). Mas
        # acumulamos também o custo de produção, para o teste responder
        # "essa campanha caberia no limite diário?".
        r.destinatarios_producao += job.n_destinatarios
        if r.destinatarios_usados + efetivo.n_destinatarios > campanha.teto_diario:
            r.pendentes.append(job.chave)
            continue

        if campanha.dry_run:
            r.pulados.append((job.chave, "dry_run — nada enviado"))
            continue

        try:
            enviar_fn(efetivo)
        except Exception as exc:
            r.falhas.append((job.chave, f"{type(exc).__name__}: {exc}"))
            log_fn(_linha_log(campanha, job, efetivo, "FALHA", str(exc)))
            continue

        r.destinatarios_usados += efetivo.n_destinatarios
        r.enviados.append(job.chave)
        ja.add(job.chave)
        log_fn(_linha_log(campanha, job, efetivo, "ENVIADO", ""))

        if pausa_segundos:
            time.sleep(pausa_segundos)

    return r


def _linha_log(campanha, job: Job, efetivo: Job, status: str, erro: str) -> dict:
    """A linha do log é o comprovante. Guarda os destinatários REAIS (não os
    redirecionados) e o total enviado, para quando o cliente contestar."""
    return {
        "id_campanha": campanha.id_campanha,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "codigo_cliente": job.codigo_cliente,
        "nome_cliente": job.nome_cliente,
        "para_real": "; ".join(job.para),
        "cc_real": "; ".join(job.cc),
        "enviado_para": "; ".join(efetivo.para),
        "modo": campanha.modo,
        "assunto": efetivo.assunto,
        "data_base_calculo": str(campanha.data_base_calculo),
        "data_limite": str(campanha.data_limite),
        "status": status,
        "erro": erro,
    }


# --------------------------------------------------------------------------
# SMTP — não testável neste ambiente (sem credencial, sem saída de rede).
# No Colab: from google.colab import userdata; senha = userdata.get('SMTP_APP_PASSWORD')
# --------------------------------------------------------------------------

def criar_enviador_smtp(remetente: str, senha_app: str,
                        nome_exibicao: str = "", reply_to: str = "",
                        host: str = "smtp.gmail.com", porta: int = 587,
                        logo_path: str = None, logo_cid: str = None):
    """Devolve um enviar_fn(job) que manda via SMTP com HTML e texto puro.

    Use senha de app, nunca a senha da conta. Guarde em Secrets do Colab —
    uma senha colada na célula vai para o histórico do notebook.

    logo_path: imagem da assinatura, EMBUTIDA na mensagem como parte inline e
    referenciada no HTML por cid:. É o único jeito que funciona sem depender de
    hospedagem: URL `raw` de repositório privado do GitHub pede autenticação,
    link do Drive falha na maioria dos clientes, e a imagem da assinatura do
    Gmail tem URL autenticada que só aparece para o próprio remetente.
    Ausente ou inexistente, a assinatura sai sem imagem — e não quebra.
    """
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr
    from pathlib import Path

    logo_bytes = None
    if logo_path:
        p = Path(logo_path)
        if p.is_file():
            logo_bytes = p.read_bytes()
            print(f"  logo embutido: {p.name} ({len(logo_bytes) // 1024} KB)")
        else:
            print(f"  AVISO: logo não encontrado em '{logo_path}'. "
                  f"A assinatura sai sem imagem.")

    def enviar(job: Job):
        msg = EmailMessage()
        msg["From"] = formataddr((nome_exibicao or remetente, remetente))
        msg["To"] = ", ".join(job.para)
        if job.cc:
            msg["Cc"] = ", ".join(job.cc)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg["Subject"] = job.assunto
        msg.set_content(job.texto)
        msg.add_alternative(job.html, subtype="html")

        if logo_bytes:
            # Anexa na PARTE HTML, não na mensagem: assim o MIME vira
            # multipart/related e o cliente resolve o cid dentro do HTML.
            # Anexado na raiz, viraria anexo comum e o cid não resolveria.
            sub, _, ext = "image", None, Path(logo_path).suffix.lower().lstrip(".")
            msg.get_payload()[-1].add_related(
                logo_bytes, maintype=sub, subtype=ext or "png",
                cid=f"<{logo_cid}>", filename=Path(logo_path).name)

        with smtplib.SMTP(host, porta, timeout=30) as s:
            s.starttls()
            s.login(remetente, senha_app)
            s.send_message(msg)

    return enviar
