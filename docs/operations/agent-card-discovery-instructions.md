# Configurar a descoberta do broker no Agent Card do Hermes

Este guia configura o texto de descoberta do broker de pareamento no Agent Card público do gateway Hermes A2A. Não altera o broker nem o Tunnel; só o `description` servido em `https://a2a.mathai.com.br/.well-known/agent.json`.

## 1. Onde a variável vive

`A2A_AGENT_DESCRIPTION` é lida pelo adaptador A2A do próprio Hermes (hook de customização já existente, sem patch de código). Ela fica em `~/.hermes/.env` no host que roda `hermes gateway run` — o mesmo arquivo privado que guarda `A2A_PEER_TOKENS` e as demais variáveis `A2A_*`.

## 2. Formato do valor

`~/.hermes/.env` é carregado por `python-dotenv`. Para um valor multilinha, use aspas duplas e `\n` literal (duas letras, não uma quebra de linha real) dentro da string — `python-dotenv` expande para a quebra de linha real ao carregar:

```dotenv
A2A_AGENT_DESCRIPTION="Primeira linha.\n\nSegunda linha depois de linha em branco."
```

Aspas duplas dentro do texto precisam de escape (`\"`); barras invertidas literais precisam de escape duplo (`\\`).

## 3. Conteúdo mínimo exigido

O texto precisa, nesta ordem, para que um agente sem contexto prévio consiga agir só com o Agent Card:

1. Declarar que toda chamada exige Bearer token — nenhuma identidade autodeclarada é aceita.
2. Nomear o hostname público do broker (`https://auth-broker.mathai.com.br`) como o único caminho de admissão.
3. Descrever os quatro passos do protocolo com método HTTP, path e formato de corpo explícitos:
   - `POST /v1/pairing-requests` com `{"public_key": "<base64 Ed25519 pubkey>"}` → retorna `{id, challenge}`.
   - Assinar os bytes crus do desafio com a chave privada Ed25519; `POST /v1/pairing-requests/{id}/proof` com `{"challenge", "signature"}` (ambos base64) → status `proof_verified`.
   - Aguardar a aprovação do dono via IdP — não existe autoaprovação.
   - Chamar o A2A usando a credencial de vida curta emitida pelo broker como Bearer.

Não inclua nenhum segredo, chave, token ou identificador de agente específico no texto — o Agent Card é servido publicamente sem autenticação.

## 4. Aplicar e verificar

1. Edite `~/.hermes/.env`, adicionando ou substituindo a linha `A2A_AGENT_DESCRIPTION=...`.
2. Reinicie o processo `hermes gateway run` para que a nova variável seja carregada (a variável só é lida na inicialização do adaptador A2A).
3. Confirme publicamente:

   ```bash
   curl -s https://a2a.mathai.com.br/.well-known/agent.json | python3 -m json.tool
   ```

   O campo `description` deve conter o texto novo, e `securitySchemes.bearer` deve continuar presente.

## Relações

- [[wiki/architecture/0003-agent-card-discovery-instructions]] — decisão e motivação.
- [[docs/operations/install-auth-broker-vps]] — instalação do broker referenciado pelo texto de descoberta.
