# Job Hunter AI — Fase 2

## Objetivo

Transformar o MVP atual — vaga manual até aprovação de CV e carta — num fluxo completo e auditável:

```text
Descobrir vagas
    → filtrar e priorizar
    → gerar CV e carta
    → rever e corrigir
    → preparar formulário e anexos
    → mostrar o pacote final ao utilizador
    → receber confirmação explícita
    → submeter
    → guardar recibo e estado
```

“Completo” não significa candidaturas autónomas sem controlo. Cada submissão exige confirmação humana e só pode usar factos verificados e os ficheiros exatos que foram aprovados.

## Estado de partida

Já existe:

- Interface web local e CLI.
- Perfil único com factos verificados, contacto e disponibilidade.
- Entrada manual e deduplicação de vagas.
- Scout, Coordinator, Creator e Analyzer.
- CV e carta em LaTeX/PDF.
- Até três rondas de revisão.
- Aprovação humana dos documentos.
- SQLite, máquina de estados, idempotência e EventLog.
- Testes automatizados do domínio, pipeline, documentos, persistência e HTTP.

Ainda falta:

- Descoberta automática de vagas.
- Perfil completamente editável e versionado.
- Avaliação semântica da qualidade dos documentos.
- Preparação e revisão de formulários.
- Applicant com automação de navegador.
- Confirmação do pacote final antes do envio.
- Submissão e recibo.
- Migrações, jobs em background, configuração e observabilidade adequadas à operação diária.

## Decisão de fluxo: duas aprovações diferentes

O estado atual `USER_APPROVED` representa apenas a aprovação de CV e carta. Isso não basta para autorizar uma submissão: respostas do formulário, consentimentos e anexos ainda podem ser acrescentados depois.

A Fase 2 deve distinguir as duas decisões:

1. **Aprovação dos documentos:** confirma CV e carta e fixa os seus hashes.
2. **Aprovação da submissão:** confirma respostas, anexos, destino e ação de envio.

Fluxo proposto:

```text
CV_REVIEW
  ├─→ REVISION_REQUIRED → CV_GENERATING
  └─→ DOCUMENTS_READY
          → DOCUMENTS_APPROVED
          → FORM_PREPARING
          → FORM_NEEDS_INFORMATION ──→ FORM_PREPARING
          → READY_FOR_SUBMISSION_APPROVAL
          → USER_APPROVED
          → SUBMITTING
          ├─→ SUBMITTED
          ├─→ SUBMISSION_FAILED
          └─→ MANUAL_ACTION_REQUIRED
```

Os estados atuais devem ser migrados, não reinterpretados silenciosamente. Candidaturas antigas em `READY_FOR_APPROVAL` continuam aguardando aprovação dos documentos.

## Arquitetura-alvo

```text
                        ┌─ Source adapters ─ APIs/RSS/carreiras
Interface web / CLI ────┤
                        └─ Profile editor
                                  │
                                  ▼
                           Coordinator
               ┌──────────┬───────┼──────────┐
               ▼          ▼       ▼          ▼
             Scout      Creator  Analyzer  Applicant
               │          │       │          │
               └──────────┴───────┴──────────┘
                                  │
                       Domain services + gates
                                  │
               ┌──────────────────┼──────────────────┐
               ▼                  ▼                  ▼
             SQLite          Artifact store      Browser adapter
          + migrations       PDFs/screenshots    + supported ATS
```

### Regras de separação

- A interface não decide estados nem critérios; chama serviços de aplicação.
- O Coordinator é o único responsável pelo avanço normal do pipeline.
- Cada fonte de vagas implementa o mesmo contrato de adapter.
- Cada portal de candidatura implementa capacidades explícitas; não existe “automação universal”.
- Applicant prepara primeiro e submete depois, em operações diferentes.
- Analyzer não confia no Creator nem no texto da vaga.
- Conteúdo de anúncios e formulários é dado não confiável, nunca instrução para o agente.
- SQLite guarda metadados e eventos; ficheiros grandes ficam no artifact store local.

## Entregas

### 2.1 — Fundação operacional

- Adicionar migrações numeradas da base de dados e tabela `schema_version`.
- Criar backup automático antes de cada migração.
- Introduzir configuração tipada para caminhos, fontes, modelo, limites e browser.
- Ler segredos apenas de variáveis de ambiente ou keyring; nunca da base, logs ou Git.
- Executar tarefas demoradas em background, com estado, tentativas e cancelamento.
- Manter servidor web responsivo durante scraping, geração e navegação.
- Redigir logs estruturados com `run_id`, `application_id` e `task_id`, sem dados sensíveis desnecessários.
- Criar health check para SQLite, Tectonic, modelo e browser.

**Aceite:** uma base da versão anterior migra sem perda; falha de migração restaura o backup; uma tarefa interrompida pode ser retomada sem duplicar resultados.

### 2.2 — Perfil verificável e editável

- Permitir criar, editar, confirmar, expirar e arquivar factos pela interface.
- Guardar fonte, data, estado e histórico de cada facto.
- Separar claramente facto confirmado, preferência e informação desconhecida.
- Editar disponibilidade por dia e janela horária.
- Modelar autorização de trabalho, localização, mobilidade, idiomas e carta de condução sem assumir respostas.
- Importar um CV apenas como rascunho; cada afirmação importada precisa de confirmação.
- Mostrar quais documentos e candidaturas usam cada facto.

**Aceite:** nenhuma alteração de perfil reescreve o histórico; facto não confirmado nunca entra num CV, carta ou formulário.

### 2.3 — Descoberta de vagas

- Definir `JobSourceAdapter` com `fetch()`, cursor, rate limit, proveniência e estado de erro.
- Começar por APIs, RSS e páginas de carreiras que permitam acesso automatizado.
- Consultar e respeitar termos, `robots.txt`, rate limits e política de cada fonte.
- Não contornar login, CAPTCHA, bloqueios ou mecanismos anti-bot.
- Não fazer scraping de LinkedIn ou Indeed sem autorização/API adequada.
- Guardar o anúncio original, URL canónica, data da consulta e hash do conteúdo.
- Criar versões quando uma vaga muda; não sobrescrever snapshots.
- Deduplicar entre fontes sem colapsar vagas materialmente diferentes.
- Permitir filtros por local, distância, remoto, horário, área, nível e palavras excluídas.
- Executar pesquisa manual pela interface e, depois, agendamento configurável.
- Criar caixa de entrada: nova, relevante, ignorada, expirada e já processada.

**Aceite:** pelo menos três adapters permitidos funcionam; duas fontes da mesma vaga geram uma oportunidade com proveniência múltipla; reexecução não duplica vagas.

### 2.4 — Scout melhorado

- Substituir o mapa fixo de palavras por taxonomia configurável e aliases.
- Separar competências, requisitos obrigatórios, preferências, localização e horário.
- Explicar cada parcela da pontuação e a evidência associada.
- Manter `unknown` distinto de `unmet`.
- Permitir rejeição e shortlist manuais sem apagar a avaliação automática.
- Extrair requisitos de texto para uma estrutura proposta, que deve passar por validação.
- Versionar regras e modelo utilizados em cada avaliação.

**Aceite:** a mesma entrada e versão de regras produz o mesmo resultado; toda pontuação mostra evidência; requisito desconhecido nunca vira requisito cumprido.

### 2.5 — Creator e Analyzer semânticos

- Criar uma interface de provider para modelo de linguagem, sem acoplar o domínio a um fornecedor.
- Exigir saída estruturada e validada por contrato.
- Versionar prompts, modelo, parâmetros, custo e resultado.
- Manter o verificador determinístico atual como gate obrigatório.
- Acrescentar avaliação semântica: clareza, relevância, linguagem, impacto e alinhamento com a vaga.
- Gerar correções específicas e rastreáveis, sem inventar experiência.
- Permitir modelos e idiomas diferentes de CV/carta.
- Mostrar diff entre versões para a revisão humana.
- Manter no máximo três rondas automáticas; depois escalar.

**Aceite:** nenhuma saída do modelo ignora os gates determinísticos; cada afirmação continua ligada a factos; custo e versão do modelo ficam auditáveis.

### 2.6 — Applicant em modo de preparação

- Criar `BrowserAdapter` e adapters específicos para portais suportados.
- Abrir a URL da candidatura e identificar campos, anexos e consentimentos.
- Normalizar campos num `ApplicationForm` versionado.
- Mapear respostas somente a partir de factos e preferências confirmados.
- Parar em `FORM_NEEDS_INFORMATION` quando faltar resposta.
- Nunca escolher consentimento, expectativa salarial, disponibilidade, autorização legal ou informação demográfica por inferência.
- Anexar apenas os PDFs cujos hashes foram aprovados.
- Guardar capturas de tela e evidências da preparação.
- Detectar login, CAPTCHA e passos não suportados e mudar para `MANUAL_ACTION_REQUIRED`.
- Proibir o clique final de envio durante esta entrega.

**Aceite:** num portal sintético de testes, o Applicant preenche tudo, anexa os ficheiros corretos, captura a revisão e para antes de enviar.

### 2.7 — Aprovação final e submissão

- Mostrar destino, empresa, cargo, respostas, consentimentos e hashes dos anexos numa única revisão.
- Gerar hash canónico do pacote completo de submissão.
- Exigir confirmação explícita vinculada a esse hash.
- Invalidar a aprovação se qualquer resposta, anexo ou destino mudar.
- Permitir uma única tentativa ativa por pacote, com idempotência.
- Submeter apenas em adapters cujo botão final e confirmação sejam conhecidos.
- Guardar timestamp, URL final, identificador do portal, captura e recibo.
- Distinguir sucesso confirmado, falha, resultado desconhecido e ação manual necessária.
- Nunca repetir automaticamente uma submissão cujo resultado seja desconhecido.

**Aceite:** o portal sintético percorre preparação → aprovação → envio → recibo; pacote alterado exige nova aprovação; retry não cria candidatura duplicada.

### 2.8 — Interface completa

- Caixa de entrada de vagas com filtros e ações em lote não destrutivas.
- Página da vaga com fonte, requisitos, score e evidências.
- Kanban/lista de candidaturas por estado.
- Detalhe com timeline, tarefas, versões, diff e documentos.
- Editor do perfil e factos.
- Revisão dos documentos lado a lado.
- Revisão final do formulário e anexos.
- Configuração de fontes, horários, modelo, orçamento e browser.
- Centro de erros e ações manuais.
- Indicadores de custo, vagas encontradas, candidaturas preparadas e submetidas.
- Acessibilidade por teclado, foco visível, contraste e layout móvel.

**Aceite:** o fluxo principal pode ser concluído sem terminal; ações irreversíveis têm confirmação clara; nenhum botão promete capacidade inexistente.

### 2.9 — Qualidade, segurança e operação

- Testes unitários, integração, concorrência e recuperação.
- Portal sintético para testes end-to-end do Applicant.
- Fixtures de páginas alteradas, campos desconhecidos, CAPTCHA e falhas de rede.
- CI no GitHub para Python suportado, testes e verificação de segredos.
- Auditoria de dependências e política de atualização.
- Redação de dados pessoais em logs e relatórios de erro.
- Retenção configurável para screenshots, anúncios e documentos.
- Exportação e eliminação dos dados locais do candidato.
- Backup e restauração documentados.
- Métricas locais sem telemetria externa por padrão.

**Aceite:** CI verde; recuperação testada após interrupção em cada fase; nenhum segredo ou artefacto pessoal entra no Git.

## Novos contratos de dados

Adicionar, no mínimo:

- `JobSource`: adapter, configuração não secreta, cursor, última execução e estado.
- `JobPostingVersion`: snapshot, hash, fonte e data.
- `ProfileFactRevision`: valor anterior, novo valor, estado e motivo.
- `ModelRun`: provider, modelo, prompt versionado, custo, tokens e referências.
- `ApplicationForm`: campos normalizados, respostas, proveniência e versão.
- `SubmissionPackage`: formulário, documentos, destino e hash canónico.
- `ApprovalDecision`: tipo, pacote/hash aprovado, timestamp e evidência explícita.
- `SubmissionAttempt`: pacote, tentativa, estado, timestamps, erro e recibo.
- `BrowserEvidence`: screenshot, URL, passo e hash.

Todos precisam de validação estrita, IDs, timestamps, referências verificadas e política de imutabilidade/versionamento.

## Ordem de implementação

1. Fundação operacional e migrações.
2. Perfil editável e versionado.
3. Contratos de fontes e descoberta manual.
4. Primeiro conjunto de adapters de vagas.
5. Scout melhorado.
6. Provider de modelo + Creator/Analyzer semânticos.
7. Portal sintético e Applicant em preparação.
8. Estados e tela de aprovação final.
9. Primeiro adapter real de candidatura.
10. Submissão confirmada e recibo.
11. Agendamento, métricas e refinamento da interface.

Essa ordem é deliberada: automatizar navegador ou adicionar um modelo antes de migrações, factos editáveis e pacote final aprovado criaria automação sobre uma base sem garantias suficientes.

## Fora da Fase 2

- Envio sem confirmação humana por candidatura.
- Contorno de CAPTCHA, login, paywall ou proteção anti-bot.
- Scraping proibido pelos termos da fonte.
- Respostas inventadas para completar formulários.
- Plataforma pública multiutilizador.
- Aplicação móvel nativa.
- Decisões automáticas sobre dados demográficos ou consentimentos.

## Definição de concluído

A Fase 2 termina quando, para pelo menos uma fonte de vagas e um portal de candidatura reais suportados, o utilizador consegue:

1. Encontrar uma vaga pela interface.
2. Ver score e evidências.
3. Gerar e rever CV e carta.
4. Completar informações desconhecidas.
5. Preparar o formulário e anexos.
6. Rever o pacote final completo.
7. Confirmar explicitamente esse pacote.
8. Submeter uma única vez.
9. Ver o recibo e todo o histórico auditável.

O mesmo fluxo deve passar integralmente no portal sintético de testes, inclusive falhas, retomada e idempotência.

