# ADR 0003 — Descoberta do broker via Agent Card do Hermes A2A

**Status:** accepted — complementa [[wiki/architecture/0001-agent-pairing-broker-v001]] e [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]

## Contexto

O Agent Card público em `https://a2a.mathai.com.br/.well-known/agent.json` anunciava `securitySchemes: {bearer}` mas não dizia a um agente não pareado **onde** obter esse bearer nem **como** iniciar o protocolo de pareamento. Um agente que só lê o card ficava sem caminho de descoberta: precisava saber do endpoint do broker por fora de banda (documentação lida por um humano), o que contradiz o objetivo de admissão autônoma que motivou o broker em primeiro lugar (ADR 0001).

O roadmap já registrava essa lacuna como aceita para v0.0.1 ("descoberta manual/fora de banda"), mas o Agent Card é exatamente o lugar certo para fechá-la: é o único artefato que todo agente A2A lê antes de qualquer chamada, e é servido publicamente sem autenticação.

## Decisão

Adicionar ao campo `description` do Agent Card, via a variável de ambiente `A2A_AGENT_DESCRIPTION` do gateway Hermes (hook de customização já existente no adaptador A2A, nenhum código de framework alterado), o texto que:

1. Declara que toda chamada exige Bearer token — nenhuma identidade autodeclarada é aceita.
2. Aponta o hostname do broker (`https://auth-broker.mathai.com.br`) como o único caminho de admissão para um agente não pareado.
3. Descreve os quatro passos do protocolo em ordem executável por um agente sem intervenção humana: `POST /v1/pairing-requests` com a chave pública → assinar o desafio e `POST .../proof` → aguardar aprovação do dono via IdP (sem autoaprovação) → chamar o A2A com a credencial curta emitida pelo broker como Bearer.

O texto é literal o bastante para um agente executar sem interpretação de prosa solta: nomeia os métodos HTTP, os paths e o formato do corpo de cada chamada.

## Consequências

- Um agente cloud que só tem acesso ao card público consegue localizar e iniciar o pareamento sozinho, sem que o dono precise entregar o endpoint do broker por um canal separado.
- O texto de descoberta é público por natureza — o Agent Card já é servido sem autenticação — então não há segredo novo exposto; nenhuma credencial, chave ou token aparece no texto.
- A alteração vive inteiramente em configuração do gateway (`A2A_AGENT_DESCRIPTION` no `.env` privado do host), não em código: reduz o texto sem exigir deploy do broker ou do gateway em si, só um restart do processo A2A para recarregar a variável.
- Fecha a lacuna "descoberta manual" registrada no roadmap de v0.0.1 (Entrega 3) sem esperar por um capability card dedicado — que continua fora do escopo desta v0.0.1.

## Alternativas rejeitadas

- **Capability card dedicado do broker**, publicado separadamente e referenciado pelo card do Hermes: mais correto a longo prazo (seria o caminho de [[wiki/principles/capability-card-peer-discovery]] aplicado aqui), mas exige um endpoint novo no broker só para descoberta — escopo maior que o necessário para fechar a lacuna agora. Fica como candidato a v0.1.
- **Documentar a descoberta só em texto lido por humano** (README, wiki): mantém a lacuna que esta ADR resolve — um agente autônomo não lê READMEs de repositório antes de chamar um endpoint A2A.
- **Endpoint HTTP dedicado de descoberta** (`GET /v1/discovery` no gateway A2A) apontando para o broker: redundante com o Agent Card, que já cumpre esse papel no protocolo A2A padrão.

## Relações

- [[wiki/architecture/0001-agent-pairing-broker-v001]]
- [[wiki/architecture/0002-auth-broker-hostname-cert-scope]]
- [[wiki/roadmap/agent-pairing-broker-v001]]
- [[docs/operations/agent-card-discovery-instructions]]
- [[src/auth-broker/README]]
