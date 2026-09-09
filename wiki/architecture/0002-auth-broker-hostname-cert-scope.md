# ADR 0002 — Hostname do broker trocado para `auth-broker.mathai.com.br`

**Status:** accepted — amenda [[wiki/architecture/0001-agent-pairing-broker-v001]]

## Contexto

A ADR 0001 e o guia operacional fixaram o hostname do broker como `pair.a2a.mathai.com.br`, reaproveitando o mesmo Named Tunnel de `a2a.mathai.com.br`. Ao configurar o Public Hostname no Tunnel e tentar validar a rota (`curl -X POST https://pair.a2a.mathai.com.br/v1/pairing-requests`), a conexão falhou em handshake TLS (`TLS alert, handshake failure`), mesmo com o Tunnel já roteando corretamente para `http://127.0.0.1:9910` (confirmado no log do `cloudflared`, que registrou o ingress atualizado).

Causa raiz: a zona `mathai.com.br` só tem Universal SSL ativo, que cobre wildcard de **um nível** (`*.mathai.com.br`). `pair.a2a.mathai.com.br` é um subdomínio de **dois níveis** abaixo da zona — fora do escopo desse certificado. Não existe Advanced Certificate Manager configurado para cobrir `*.a2a.mathai.com.br`.

## Decisão

Renomear o hostname público do broker para `auth-broker.mathai.com.br` — um nível abaixo da zona, já coberto pelo Universal SSL existente, sem custo ou configuração adicional de certificado.

Consequências diretas em toda a cadeia:

- Public Hostname do Tunnel: `auth-broker.mathai.com.br` → `http://127.0.0.1:9910` (era `pair.a2a.mathai.com.br`).
- Cloudflare Access Application domain: `auth-broker.mathai.com.br`, mesmo path `/v1/pairing-requests/*/approve`.
- `AUTH_BROKER_AUDIENCE=https://auth-broker.mathai.com.br` no `auth-broker.env` da VPS.
- Toda documentação e wikilink que citava `pair.a2a.mathai.com.br` deve apontar para `auth-broker.mathai.com.br`.

## Alternativas rejeitadas

- **Ativar Total TLS / Advanced Certificate Manager** para cobrir `*.a2a.mathai.com.br`: resolveria sem trocar o nome, mas pode ter custo dependendo do plano da zona e não foi a escolha do dono.
- **Manter `pair.a2a.mathai.com.br` sem TLS de borda** (`no_tls_verify` ou similar): rejeitado — o guia operacional é explícito que TLS termina na Cloudflare e a origem HTTP só é segura por rodar atrás do Tunnel; contornar a checagem de certificado no cliente enfraquece essa garantia sem necessidade.

## Relações

- [[wiki/architecture/0001-agent-pairing-broker-v001]] — decisão original que esta ADR amenda (hostname apenas; identidade, aprovação e limites de escopo continuam válidos).
- [[docs/operations/install-auth-broker-vps]] — guia operacional atualizado com o novo hostname.
- [[wiki/roadmap/agent-pairing-broker-v001]]
- [[src/auth-broker/README]]
