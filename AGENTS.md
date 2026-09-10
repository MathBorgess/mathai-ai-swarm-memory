# AGENTS — mathai-context-engine

## Propósito

Manter uma camada de contexto privada e revisável para agentes do dono. O repositório contém a identidade Hermes e o broker A2A OAuth; o conhecimento compilado pertence ao `mathai-wiki`.

## Orientação obrigatória

Leia nesta ordem: `CLAUDE.md` → `wiki/index.md` → ADR ou roadmap aplicável → README do componente. Não presuma que uma credencial, uma sessão MCP ou um arquivo privado prove identidade para um serviço remoto.

## Limites de escrita

- `src/hermes-identity/`: apenas identidade sincronizável e seu setup.
- `src/auth-broker/`: protocolo, implementação e testes do broker.
- `wiki/`: decisões e operação deste repositório, sem segredos.
- `docs/`: planos e guias operacionais sem segredos; não é estado operacional vivo.

## Segurança inegociável

1. Nunca comitar `.env`, `auth.json`, tokens A2A, token de tunnel, cookies, sessões, cache, chaves privadas ou SQLite real.
2. O broker guarda estado de grants/transações e auditoria sem tokens upstream. Client secret GitHub, bearer privado broker→Hermes e SQLite real ficam somente no ambiente da VPS.
3. MCP autenticado é capacidade local do agente; não é uma credencial que pode ser encaminhada ao broker.
4. O broker termina a autenticação do agente e usa outra credencial para chamar Hermes. Nunca encaminhe o token apresentado pelo agente.
5. A aprovação do dono usa GitHub Device Flow e uma allowlist de ID numérico. Não adicione Cloudflare Access ao hostname A2A público; o fluxo legado de pairing permanece desativado.

## Compatibilidade Hermes

Os arquivos canônicos vivem em `src/hermes-identity/`. Os caminhos raiz `SOUL.md` e `memories/*` são links de compatibilidade para instalações existentes. Em cada máquina:

```bash
./hermes-sync-identity.sh pull
./hermes-sync-identity.sh link
```

Após Hermes alterar memória, rode `./hermes-sync-identity.sh push`. Antes de iniciar Hermes numa máquina fria, rode `pull`.

## A2A atual e destino

`https://a2a.mathai.com.br` é o origin público único do broker; Hermes continua privado em `127.0.0.1:9900`. O cliente descobre o card no mesmo origin, executa Device Flow e usa grant A2A curto. Nunca entregue o bearer Hermes ao cliente.

## Verificação mínima

```bash
bash tests/test-hermes-identity-sync.sh
```

Testes de protocolo e SQLite são obrigatórios antes de qualquer deploy. Para repetir o setup, siga `docs/operations/install-auth-broker-vps.md` e o runbook da wiki; não crie workflow GitHub para descoberta ou autenticação.
