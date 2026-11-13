# Job Hunter AI

Código aberto sob licença MIT. Cada pessoa cria o próprio perfil no primeiro acesso; nenhum dado pessoal de candidato é fornecido com o projeto.

Pipeline local para candidaturas: **Scout → Creator → Analyzer**, orquestrado pelo **Coordinator**.
O Coordinator para em `READY_FOR_APPROVAL`. Só a aprovação humana explícita (`approve --yes`) pode avançar para `USER_APPROVED`; submissões continuam bloqueadas (`SUBMITTING`/`SUBMITTED`).

## Requisitos

- Python 3.12 (só biblioteca padrão; sem dependências).
- Tectonic 0.17 (motor LaTeX) em `.runtime/tectonic/tectonic.exe` (ou `JH_TECTONIC`, ou no `PATH`). Não vai para o Git. Verifica com `jh doctor`.
- O launcher `jh.cmd` usa o Python 3.12 do projeto `prospeccao`; muda com `JH_PYTHON`.

## Uso

```
jh init caminho/para/meu_perfil.json      # cria perfil com os seus dados (ver examples/demo_profile.json)
jh set-contact --email X --phone Y       # contacto para o cabeçalho (só o que o candidato fornecer)
jh confirm-availability --yes            # SÓ depois de o candidato confirmar a disponibilidade atual
jh run examples/demo_job.json            # avalia uma vaga manual e avança a candidatura
jh run vaga.json --resume                # retoma depois de NEEDS_INFORMATION
jh approve APPLICATION_ID                # mostra o resumo e os ficheiros
jh approve APPLICATION_ID --yes           # regista a aprovação; ainda não envia
jh status                                # candidaturas e estados
jh web                                   # abre a interface web local
jh demo                                  # ponta a ponta em BD descartável (data/demo.sqlite3)
python -m unittest discover -s tests     # com PYTHONPATH=src
```

PDFs ficam em `artifacts/<application_id>/v<N>/{cv,letter}.{tex,pdf}`.

## Fluxo e regras

| Passo | Quem | Regra |
|---|---|---|
| Avaliar vaga | Scout (`scout.py`) | Anúncio é dado não confiável. Requisito sem facto verificado = "desconhecido", nunca "cumprido". |
| Gates | Coordinator + `storage.py` | SHORTLISTED exige `recommend` sem informação em falta; com horário na vaga exige disponibilidade confirmada. |
| Gerar | Creator (`creator.py`) | Só factos verificados, texto verbatim; do anúncio usa só empresa/cargo/local, com escape LaTeX. |
| Rever | Analyzer (`analyzer.py`) | Independente: relê BD + ficheiros. Fidelidade, requisitos, completude, PDF (sha256, páginas), segurança LaTeX. Aprova com score ≥ 90 e sem críticos. |
| Rever de novo | Coordinator | Até 3 revisões; depois `NEEDS_INFORMATION` (escalado ao humano). |

O Analyzer é **determinístico** (regras), não um juízo de modelo: garante fidelidade e integridade, não qualidade de redação.

## Limites conhecidos

- Só vagas fornecidas manualmente (JSON; ver `examples/`). Sem pesquisa externa.
- O Scout só reconhece requisitos do mapa `MATCHES` em `scout.py` ou etiquetas dos factos; o resto fica "desconhecido" (conservador).
- Um anúncio já guardado não é atualizado (snapshot original preservado); um horário em falta exige novo anúncio.
- O papel Job Applicant ainda não submete candidaturas; depois de `USER_APPROVED`, a submissão continua fora do MVP.
- Documentos em português; carta e CV são modelos fixos.

Os dados do utilizador ficam na base SQLite local (`data/`, ignorada pelo Git). A fixture em `examples/` é inteiramente fictícia.

Para usar o terminal, copie `examples/demo_profile.json` para `data/meu_perfil.json`, substitua todos os campos pelos seus próprios dados e execute `jh init data/meu_perfil.json`. Pela interface web, o primeiro acesso apresenta o formulário de criação do perfil. Guarde ficheiros pessoais apenas em `data/`.

Plano do próximo incremento: [Fase 2](docs/PHASE-2.md).
