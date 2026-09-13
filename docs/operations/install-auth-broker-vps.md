# Reinstalar o broker A2A OAuth na VPS

Este documento é o ponto de partida autossuficiente para reconstruir o serviço que expõe `https://a2a.mathai.com.br`. Ele usa GitHub Device Flow para autenticar o dono e mantém Hermes privado no loopback. O procedimento não usa Cloudflare Access: o pairing legado fica desativado e suas rotas devem responder `404`.

Para a operação diária, staging, promoção e cleanup, leia também a wiki privada `MathBorgess/mathai-wiki`, nota `estudos/context-engineering/2026-09-09-a2a-oauth-broker-runbook-vps.md`. Esta página mantém os pré-requisitos e o caminho completo de reconstrução; o runbook registra os comandos consolidados da instalação validada.

## Arquitetura e limites

```text
agente pessoal -- HTTPS + Device Flow --> a2a.mathai.com.br (broker, 127.0.0.1:9910)
                                                  |
                                      bearer de peer, privado
                                                  v
                                   Hermes (127.0.0.1:9900)
```

- O cliente externo fornece apenas `https://a2a.mathai.com.br`, lê `/.well-known/agent-card.json`, faz Device Flow e usa o grant de uma hora para `POST /v1/context/query` no mesmo origin.
- GitHub entra apenas para identificar o dono via `read:user`; o broker emite os scopes próprios `a2a:discover`, `a2a:message` e `a2a:history`.
- `HERMES_BROKER_TOKEN`, o client secret GitHub, o SQLite, logs e o arquivo `.env` ficam exclusivamente na VPS. Não entram em Git, prompts, wiki ou clipboard compartilhado.
- O broker aceita HTTP somente para o upstream loopback. A borda pública é HTTPS, terminada pelo Tunnel Cloudflare, e a porta `9910` não deve ser exposta diretamente.
- `a2a:history` é emitido, mas não há endpoint/history policy nesta entrega. O grant é bearer: DPoP e perfis Hermes restritos por sessão são melhorias obrigatórias antes de ampliar para terceiros.

## 1. Pré-requisitos que exigem decisão do dono

1. Um GitHub OAuth App do dono, com **Device Flow habilitado**, client ID e client secret privados. Configure `https://a2a.mathai.com.br/` como homepage/callback se o painel exigir uma URL. O broker usa somente `read:user` no upstream.
2. O ID numérico estável da conta GitHub permitida. Não use o login textual como allowlist.
3. Um Named Tunnel Cloudflare já associado à zona `mathai.com.br`, com o Public Hostname `a2a.mathai.com.br` encaminhando para `http://127.0.0.1:9910`. Remova Cloudflare Access desse hostname; ele bloquearia o Device Flow público.
4. Hermes instalado e capaz de iniciar `hermes gateway run` sob o mesmo usuário Linux. Na VPS atual os comandos usam `python3` (3.13) e `netstat`; não pressupõem `python3.12` nem `lsof`.

## 2. Código, diretórios privados e configuração

Os caminhos abaixo usam o usuário `box`; ajuste somente o prefixo caso a VPS use outro usuário. Não cole valores secretos no comando: abra o editor privado local e preencha o arquivo diretamente.

```bash
git clone https://github.com/MathBorgess/mathai-ai-swarm-memory.git "$HOME/src/mathai-context-engine"
git -C "$HOME/src/mathai-context-engine" fetch origin codex/a2a-github-oauth
git -C "$HOME/src/mathai-context-engine" worktree add --detach \
  "$HOME/services/a2a-broker" origin/codex/a2a-github-oauth

install -d -m 700 "$HOME/.mathai-context-engine"
editor "$HOME/.mathai-context-engine/auth-broker.env"
chmod 600 "$HOME/.mathai-context-engine/auth-broker.env"
```

Preencha o arquivo `auth-broker.env` com nomes e caminhos exatos, mas sem deixar placeholders literais no arquivo:

```dotenv
AUTH_BROKER_DATABASE_PATH=/home/box/.mathai-context-engine/auth-broker.sqlite3
AUTH_BROKER_AUDIENCE=https://a2a.mathai.com.br
HERMES_A2A_URL=http://127.0.0.1:9900
GITHUB_OAUTH_CLIENT_ID=<valor-privado>
GITHUB_OAUTH_CLIENT_SECRET=<valor-privado>
GITHUB_ALLOWED_USER_ID=<id-numerico-do-dono>
HERMES_BROKER_TOKEN=<valor-do-peer-auth-broker-no-Hermes>
```

As sete variáveis são obrigatórias. Não configure `AUTH_BROKER_CF_ACCESS_*` nem `AUTH_BROKER_OWNER_EMAIL`, salvo uma reintrodução deliberada e revisada do pairing legado.

## 3. Criar ou sincronizar o peer privado com Hermes

Hermes exige o token em `A2A_PEER_TOKENS` e o nome correspondente em `A2A_TRUSTED_PEERS`. Se `auth-broker` já existir, não gere outro valor: sincronize o seu valor canônico para `HERMES_BROKER_TOKEN` sem imprimi-lo.

```bash
HERMES_ENV="$HOME/.hermes/.env"
STATE="$HOME/.mathai-context-engine"
BROKER_ENV="$STATE/auth-broker.env"

peer_token="$(sed -n 's/^A2A_PEER_TOKENS=//p' "$HERMES_ENV" | tr ',' '\n' | sed -n 's/^auth-broker://p' | head -n 1)"
if [ -z "$peer_token" ]; then
  echo 'Crie auth-broker em A2A_PEER_TOKENS e A2A_TRUSTED_PEERS primeiro; nada foi alterado.'
else
  temp_env="$(mktemp)"
  while IFS= read -r line; do
    case "$line" in
      HERMES_BROKER_TOKEN=*) printf 'HERMES_BROKER_TOKEN=%s\n' "$peer_token" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$BROKER_ENV" > "$temp_env"
  chmod 600 "$temp_env"
  mv "$temp_env" "$BROKER_ENV"
  unset peer_token
fi
```

Para a primeira criação, gere o segredo em arquivo privado com `umask 077; openssl rand -base64 24`, acrescente `auth-broker:<valor>` a `A2A_PEER_TOKENS`, acrescente `auth-broker` a `A2A_TRUSTED_PEERS`, atualize `HERMES_BROKER_TOKEN` pelo mesmo valor e aplique `chmod 600 "$HOME/.hermes/.env"`. Não imprima o resultado. A presença do nome por si só não basta: token divergente causa `401 unauthorized`.

Reinicie Hermes de forma graciosa antes de iniciar o broker:

```bash
ENV_FILE="$HOME/.hermes/.env"
LOG_FILE="$HOME/.hermes/gateway.log"
pids="$(pgrep -f 'hermes gateway run' || true)"

if [ -n "$pids" ]; then
  kill -TERM $pids
  for _ in $(seq 1 20); do sleep 1; pgrep -f 'hermes gateway run' >/dev/null || break; done
fi

if pgrep -f 'hermes gateway run' >/dev/null; then
  echo 'Hermes não parou; investigar antes de continuar.'
else
  umask 077
  nohup /bin/bash -lc 'set -a; . "$HOME/.hermes/.env"; set +a; exec hermes gateway run' >"$LOG_FILE" 2>&1 &
fi
```

## 4. Bootstrap, staging e promoção

Evite `set -e` no terminal interativo. Ele pode fechar a sessão sem mostrar o diagnóstico. Primeiro valide e suba staging em `9911`:

```bash
BROKER="$HOME/services/a2a-broker"
STATE="$HOME/.mathai-context-engine"
ENV_FILE="$STATE/auth-broker.env"

PYTHON_BIN=python3 "$BROKER/src/auth-broker/scripts/setup-vps.sh" "$ENV_FILE"

nohup /bin/bash -lc "cd '$BROKER/src/auth-broker' && set -a && . '$ENV_FILE' && set +a && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9911" \
  >"$STATE/auth-broker-next.log" 2>&1 &
sleep 2
curl -fsS http://127.0.0.1:9911/.well-known/agent-card.json | jq '.securitySchemes'
```

Depois do card local responder, use `netstat -ltnp` para identificar explicitamente o PID anterior em `9910`, pare-o com `kill -TERM <pid>`, pare o staging de `9911` e inicie o mesmo comando com `--port 9910`. Só então o Tunnel entrega o origin público ao broker novo.

```bash
netstat -ltnp 2>/dev/null | grep -E ':(9900|9910|9911)[[:space:]]' || true
```

## 5. Smoke, revogação e recuperação

O smoke não autenticado abaixo verifica o caminho público sem vazar tokens:

```bash
curl -fsS https://a2a.mathai.com.br/.well-known/agent-card.json \
  | jq '{url, security, deviceCode: .securitySchemes.githubOAuth.flows.deviceCode}'
```

O teste completo deve usar um cliente Device Flow compatível: pedir os três scopes, o dono aprova no GitHub, fazer poll, chamar `POST /v1/context/query` com o grant e por fim chamar `POST /v1/oauth/revoke`. Não registre nem exiba `access_token`.

Para recuperação, investigar primeiro o log privado e os listeners. `401` de Hermes normalmente indica valor de `HERMES_BROKER_TOKEN` diferente do peer `auth-broker`, ou ausência do nome em `A2A_TRUSTED_PEERS`. `404` em pairing legado é esperado sem Cloudflare Access. Se client secret, bearer ou `.env` aparecer em scrollback, clipboard ou log, regenere/revogue a credencial afetada, reescreva o arquivo privado e reinicie; não tente mascarar o incidente com limpeza de histórico.

No cleanup mínimo, preserve produção `9910`, o SQLite e o checkout anterior durante a observação. Remova somente processo/log de staging `9911` depois de verificar que a porta não está em uso. Não use `pkill` genérico, `rm -rf` ou remoção de banco sem backup deliberado.
