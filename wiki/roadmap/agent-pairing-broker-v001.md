# Roadmap — Agent Pairing Broker v0.0.1

## Fundação — concluída neste change set

- [x] Reposicionamento para `mathai-context-engine`.
- [x] Layout com `src/hermes-identity` e `src/auth-broker`.
- [x] Compatibilidade de links para instalações Hermes existentes.
- [x] ADR de fronteira de confiança e mini-wiki navegável.
- [x] Plano de implementação rastreável.

## Entrega 1 — protocolo local

- [ ] Definir request, resposta e erros para descoberta, pedido, aprovação, emissão e revogação.
- [ ] Implementar prova de posse da chave e credencial curta com `aud` do broker.
- [ ] Cobrir expiração, replay, pedido duplicado e chave revogada com testes.

**Saída:** um processo local cria pedido, aprova com identidade de teste, aceita uma chamada assinada e rejeita a mesma chamada após revogação.

## Entrega 2 — SQLite e auditoria

- [ ] Criar migrações versionadas e transações para pedidos, agentes, chaves, revogações e eventos.
- [ ] Garantir que consultas de status não incluam credenciais nem material de desafio em claro.
- [ ] Exercitar upgrade de banco vazio e reabertura do banco com testes.

**Saída:** o processo reinicia e preserva somente o estado necessário para admissão e revogação.

## Entrega 3 — borda e aprovação do dono

- [x] Configurar o hostname `auth-broker.mathai.com.br` no Tunnel Cloudflare da conta da zona (trocado de `pair.a2a.mathai.com.br`; ver [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]).
- [x] Proteger a UI de aprovação com Cloudflare Access e validar o JWT no broker.
- [x] Configurar política que aceite somente a identidade do dono.
- [x] Descoberta do endpoint de pareamento pelo Agent Card do Hermes A2A, sem intervenção humana ([[wiki/architecture/0003-agent-card-discovery-instructions]]).

**Saída:** pedido remoto fica pendente; sem login do dono, nenhuma aprovação é possível. Um agente que só lê o Agent Card público consegue localizar o broker e iniciar o pedido sozinho.

## Entrega 4 — integração Hermes

- [ ] Criar o adaptador broker→Hermes com credencial distinta e local à VPS.
- [ ] Limitar v0.0.1 ao perfil `owner-agent` e às operações de contexto aprovadas.
- [ ] Testar falha fechada quando Hermes, Access ou SQLite não respondem.

**Saída:** um agente aprovado consulta contexto via broker; um agente não pareado não chega ao Hermes.

## Gate para v0.1

V0.1 só abre após a primeira operação estável registrar pares, revogações e chamadas sem expor segredo. Ela introduz escopos por recurso e compartilhamento explícito com terceiros; não reaproveita o perfil global como autorização implícita.

## Relações

- [[wiki/architecture/0001-agent-pairing-broker-v001]]
- [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]
- [[wiki/architecture/0003-agent-card-discovery-instructions]]
- [[src/auth-broker/README]]
