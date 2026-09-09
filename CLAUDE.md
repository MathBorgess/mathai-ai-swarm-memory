# mathai-context-engine — guia de contexto

## O que este repositório é

Uma base privada para a context engine do dono. Ela separa três responsabilidades:

| Responsabilidade | Fonte de verdade |
|---|---|
| Conhecimento, decisões pessoais e atividades | `MathBorgess/mathai-wiki` |
| Persona e memória que devem seguir Hermes entre máquinas | `src/hermes-identity/` |
| Admissão de agentes no A2A sem distribuir bearer estático | `src/auth-broker/` |

## Como se orientar

1. Leia este arquivo e `AGENTS.md`.
2. Leia `wiki/index.md`.
3. Para identidade, leia `src/hermes-identity/README.md`; para pareamento, leia `src/auth-broker/README.md` e `wiki/architecture/0001-agent-pairing-broker-v001.md`.
4. Para executar uma mudança planejada, leia `docs/superpowers/plans/2026-09-09-agent-pairing-broker-v001.md`.

## Modelo de confiança v0.0.1

Um agente pode descobrir o endpoint e abrir um pedido sozinho. Ele não pode se admitir. O dono, autenticado na UI de aprovação, confirma a chave pública apresentada pelo agente. O broker emite uma credencial curta vinculada a essa chave e usa uma credencial distinta, local à VPS, para conversar com Hermes.

Isso resolve o problema de tokens A2A fixos sem fingir que a sessão GitHub, Drive ou Cloudflare de um agente é uma identidade exportável.

## Operação local

O clone ainda pode permanecer em `~/src/hermes-identity` durante a transição. O script preserva esse caminho e religa Hermes para o layout canônico:

```bash
./hermes-sync-identity.sh pull
./hermes-sync-identity.sh link
bash tests/test-hermes-identity-sync.sh
```

Não há banco versionado nem serviço iniciado neste estágio. SQLite será criado pela aplicação do broker na VPS, fora do Git, quando a implementação começar.
