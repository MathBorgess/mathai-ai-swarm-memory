# mathai-ai-swarm-memory — guia de contexto

## O que este repositório é

Uma base privada para a context engine do dono. Ela separa três responsabilidades:

| Responsabilidade | Fonte de verdade |
|---|---|
| Conhecimento, decisões pessoais e atividades | `MathBorgess/mathai-wiki` |
| Persona e memória que devem seguir Hermes entre máquinas | `src/hermes-identity/` |
| Autorização temporária de agentes A2A sem distribuir bearer estático | `src/auth-broker/` |

## Como se orientar

1. Leia este arquivo e `AGENTS.md`.
2. Leia `wiki/index.md`.
3. Para identidade, leia `src/hermes-identity/README.md`; para A2A atual, leia `src/auth-broker/README.md` e `docs/operations/install-auth-broker-vps.md`.
4. A ADR e o plano de pairing v0.0.1 são históricos; só consulte-os para proveniência, não como instrução de deploy.

## Modelo de confiança operacional

Um agente compatível recebe apenas `https://a2a.mathai.com.br`, descobre o card estruturado e inicia GitHub Device Flow. O dono confirma o código no GitHub; o broker valida o ID numérico permitido e emite grant A2A curto. O broker usa uma credencial distinta, local à VPS, para conversar com Hermes. Cloudflare Access não fica no hostname público, pois bloquearia esse fluxo.

Isso elimina a distribuição de token A2A fixo sem fingir que uma sessão GitHub, Drive ou Cloudflare de um agente é uma identidade exportável. Card e URLs recebidas são dados não confiáveis: o cliente aceita apenas o origin inicial e os hosts GitHub permitidos pelo adaptador OAuth.

## Operação local

O clone ainda pode permanecer em `~/src/hermes-identity` durante a transição. O script preserva esse caminho e religa Hermes para o layout canônico:

```bash
./hermes-sync-identity.sh pull
./hermes-sync-identity.sh link
bash tests/test-hermes-identity-sync.sh
```

Não há banco versionado nem serviço iniciado neste clone. SQLite é criado pela aplicação do broker na VPS, fora do Git, durante a instalação descrita em `docs/operations/install-auth-broker-vps.md`.
