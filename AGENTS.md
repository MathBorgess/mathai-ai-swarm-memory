# AGENTS — mathai-context-engine

## Propósito

Manter uma camada de contexto privada e revisável para agentes do dono. O repositório contém a identidade Hermes e o contrato do futuro broker de pareamento; o conhecimento compilado pertence ao `mathai-wiki`.

## Orientação obrigatória

Leia nesta ordem: `CLAUDE.md` → `wiki/index.md` → ADR ou roadmap aplicável → README do componente. Não presuma que uma credencial, uma sessão MCP ou um arquivo privado prove identidade para um serviço remoto.

## Limites de escrita

- `src/hermes-identity/`: apenas identidade sincronizável e seu setup.
- `src/auth-broker/`: protocolo, implementação e testes do broker.
- `wiki/`: decisões e operação deste repositório, sem segredos.
- `docs/`: planos e guias operacionais sem segredos; não é estado operacional vivo.

## Segurança inegociável

1. Nunca comitar `.env`, `auth.json`, tokens A2A, token de tunnel, cookies, sessões, cache, chaves privadas ou SQLite real.
2. O broker guarda chaves públicas, hashes de desafios, estados de pareamento e auditoria. Segredos de assinatura e o bearer privado broker→Hermes ficam somente no ambiente da VPS.
3. MCP autenticado é capacidade local do agente; não é uma credencial que pode ser encaminhada ao broker.
4. O broker termina a autenticação do agente e usa outra credencial para chamar Hermes. Nunca encaminhe o token apresentado pelo agente.
5. A aprovação do dono usa a rota de aprovação protegida por Cloudflare Access; v0.0.1 não aceita autoaprovação nem convidados de terceiros.

## Compatibilidade Hermes

Os arquivos canônicos vivem em `src/hermes-identity/`. Os caminhos raiz `SOUL.md` e `memories/*` são links de compatibilidade para instalações existentes. Em cada máquina:

```bash
./hermes-sync-identity.sh pull
./hermes-sync-identity.sh link
```

Após Hermes alterar memória, rode `./hermes-sync-identity.sh push`. Antes de iniciar Hermes numa máquina fria, rode `pull`.

## A2A atual e destino

`https://a2a.mathai.com.br` continua sendo o gateway Hermes. O broker tem hostname próprio, `auth-broker.mathai.com.br` (trocado de `pair.a2a.mathai.com.br`, ver [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]); ele não expõe o bearer estático do gateway e não amplia a superfície pública do card Hermes.

## Verificação mínima

```bash
bash tests/test-hermes-identity-sync.sh
```

Testes de protocolo e migração SQLite são obrigatórios antes de qualquer deploy. Não crie workflow GitHub para a descoberta ou o pareamento: leitura via API/MCP não deve consumir GitHub Actions.
