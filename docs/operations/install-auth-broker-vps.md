# Instalar o Agent Pairing Broker na VPS

Este guia prepara o **Agent Pairing Broker v0.0.1** na VPS que já expõe Hermes em `a2a.mathai.com.br`. Ele não faz deploy automaticamente e não autoriza a alterar a conta Cloudflare ou o gateway existente sem revisão humana.

O agente externo usa somente **`https://a2a.mathai.com.br`** e descobre o Agent Card e os endpoints OAuth no mesmo origin. `pair.a2a.mathai.com.br` é legado/privado para a UI de aprovação, se mantido; não deve aparecer no Agent Card nem ser exigido de agentes.

## Limites de segurança

- Agentes não recebem `HERMES_BROKER_TOKEN`. Esse bearer é usado apenas pelo processo broker no salto para Hermes.
- O SQLite fica fora do clone, com permissões privadas. Não copie banco, `.env` nem logs para GitHub.
- Sem Cloudflare Access, o pairing legado fica desativado (os caminhos `/v1/pairing-requests/*` retornam `404`). O fluxo público é somente o GitHub Device Flow do broker.
- O Device Flow emite somente `a2a:discover`, `a2a:message` e `a2a:history`; não há escopos de projeto, documento, ferramenta ou terceiro.

## 1. Instalar o código e as dependências

Na VPS, escolha um diretório privado de serviço. Os caminhos abaixo assumem o usuário `box`; ajuste somente o prefixo se a VPS usar outro usuário.

```bash
git clone https://github.com/MathBorgess/mathai-context-engine.git /home/box/github/mathai-context-engine
install -d -m 700 /home/box/.mathai-context-engine
```

O bootstrap é executado depois de criar o arquivo privado de configuração no passo 2. Ele exige Python 3.12 ou superior e instala/testa o componente sem iniciar o serviço. O banco será criado em `/home/box/.mathai-context-engine`, não dentro do repositório.

## 2. Criar a configuração local do broker

Crie `/home/box/.mathai-context-engine/auth-broker.env` com modo `600`. Não versionar esse arquivo.

```dotenv
AUTH_BROKER_DATABASE_PATH=/home/box/.mathai-context-engine/auth-broker.sqlite3
AUTH_BROKER_AUDIENCE=https://a2a.mathai.com.br
HERMES_A2A_URL=http://127.0.0.1:9900
# OAuth App registrada previamente; nunca comite estes valores.
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
GITHUB_ALLOWED_USER_ID=...
HERMES_BROKER_TOKEN=<token-de-peer-exclusivo-do-broker>
```

As sete variáveis listadas são obrigatórias: valor ausente, vazio ou composto só de espaços impede a inicialização. O `AUTH_BROKER_AUDIENCE` precisa ser exatamente a URL pública do broker. As três variáveis `AUTH_BROKER_CF_ACCESS_*` e `AUTH_BROKER_OWNER_EMAIL` são opcionais e só devem existir juntas se o pairing legado for reativado.

Com o arquivo pronto, execute o bootstrap repetível:

```bash
/home/box/github/mathai-context-engine/src/auth-broker/scripts/setup-vps.sh \
  /home/box/.mathai-context-engine/auth-broker.env
```

Ele cria o ambiente virtual local, valida todas as variáveis sem imprimi-las, cria o diretório privado do banco e roda os testes. Ele não inicia Uvicorn e não altera Hermes, Tunnel ou Cloudflare Access.

## 3. Criar a credencial privada broker → Hermes

No host que mantém o gateway Hermes, adicione um peer exclusivo para o broker em `~/.hermes/.env`. Gere o segredo localmente e o entregue somente ao arquivo do serviço:

```bash
openssl rand -base64 24
```

Acrescente o resultado em `A2A_PEER_TOKENS` como `auth-broker:<token>` e use o mesmo valor somente em `HERMES_BROKER_TOKEN` no arquivo anterior. Reinicie o processo atual `hermes gateway run` para que a alteração seja carregada.

Esse token não é credencial de pareamento e não deve aparecer em `AGENTS.md`, na wiki, em Pull Requests, em logs, nem na configuração de qualquer agente cloud. Se o broker for desativado, remova esse peer e reinicie Hermes.

## 4. Iniciar somente no loopback

Teste primeiro em primeiro plano:

```bash
cd /home/box/github/mathai-context-engine/src/auth-broker
set -a
. /home/box/.mathai-context-engine/auth-broker.env
set +a
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9910
```

Como a VPS atual não pressupõe `systemd`, uma primeira execução persistente pode usar `nohup` depois da validação manual:

```bash
nohup /bin/bash -lc 'cd /home/box/github/mathai-context-engine/src/auth-broker && set -a && . /home/box/.mathai-context-engine/auth-broker.env && set +a && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9910' \
  >/home/box/.mathai-context-engine/auth-broker.log 2>&1 &
```

O origin fica em HTTP porque o Tunnel alcança `127.0.0.1`; TLS termina na Cloudflare. Não exponha a porta `9910` diretamente na internet.

## 5. Alterar o hostname no Cloudflare Tunnel

Na **mesma conta Cloudflare que controla a zona `mathai.com.br` e o Named Tunnel existente**, acrescente o Public Hostname:

| Campo | Valor |
| --- | --- |
| Hostname | `a2a.mathai.com.br` |
| Service | `http://127.0.0.1:9910` |
| Tunnel | o mesmo Named Tunnel já usado por `a2a.mathai.com.br` |

A criação do Public Hostname mantém o CNAME gerenciado pelo Tunnel. Não crie CNAME para `trycloudflare.com`, não use outra conta Cloudflare e não aponte esse hostname para a porta do Hermes.

## 6. Proteger somente a aprovação do dono com Cloudflare Access

Mantenha uma aplicação **Self-hosted** no Cloudflare Access para o hostname `a2a.mathai.com.br`, limitada somente ao caminho de aprovação:

```text
/v1/pairing-requests/<request-id>/approve
```

Na interface de Application paths, configure a variante curinga suportada para cobrir apenas esse último segmento variável. A sintaxe exibida no painel deve ser conferida com a documentação atual de [Application paths / Cloudflare Access](https://developers.cloudflare.com/workers/configuration/cloudflare-access/); não converta isso numa regra que proteja o hostname inteiro.

Adicione uma política **Allow** que autentique exclusivamente o dono pelo IdP escolhido (por exemplo, GitHub) e pelo e-mail exato configurado em `AUTH_BROKER_OWNER_EMAIL`. Uma aplicação Access sem política permite acesso a ninguém. Copie o `AUD` da aplicação e o issuer do team domain para o arquivo de ambiente do broker.

O origin valida o JWT recebido no header `Cf-Access-Jwt-Assertion` por JWKS, issuer, audience, algoritmo RS256 e e-mail do dono. A referência oficial para essa validação é [Validating JSON Web Tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/).

Não use Service Token, header de e-mail ou um token MCP como substituto dessa aprovação. Também não proteja `/v1/pairing-requests` ou `/v1/context/query` com Access: isso impediria o fluxo autônomo de agentes.

## 7. Validação e operação

Depois do Tunnel publicar o hostname, confirme que a rota pública chega ao aplicativo sem criar um pedido:

```bash
curl -i -X POST https://a2a.mathai.com.br/v1/pairing-requests \
  -H 'content-type: application/json' \
  --data '{}'
```

O resultado esperado é `400 Invalid pairing request`. Ele prova rota, Tunnel e serviço, mas não o fluxo de autenticação.

Para o smoke completo, use uma chave Ed25519 descartável:

1. Faça `POST /v1/pairing-requests` com a chave pública em base64 padrão.
2. Assine o desafio retornado e envie prova para `POST /v1/pairing-requests/{id}/proof`.
3. Autentique o dono via Access e aprove em `POST /v1/pairing-requests/{id}/approve`.
4. Envie uma consulta a `POST /v1/context/query` com `X-Agent-Envelope` canônico e `X-Agent-Signature` Ed25519. O envelope tem `agent_id`, `audience`, `timestamp` UTC, nonce de 32 bytes e SHA-256 dos bytes exatos do corpo.

Repita a mesma consulta idêntica: ela deve falhar por replay. Teste também audiência errada, assinatura errada e pedido não aprovado; todos devem falhar. Não inclua texto da consulta nos logs de validação.

## Rollback e revogação

Para rollback, restaure temporariamente o Public Hostname `a2a.mathai.com.br` para `http://127.0.0.1:9900` (Hermes direto), pare o broker e remova o peer `auth-broker` de `A2A_PEER_TOKENS`; depois reinicie Hermes. Preserve o SQLite para auditoria e faça backup protegido antes de qualquer exclusão deliberada.

Uma operação futura de revogação deve marcar o agente no broker antes de alterar credenciais do upstream. A revogação de sessão é imediata; a revogação de agente continua disponível por agente.
