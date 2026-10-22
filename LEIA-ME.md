# Avenida Livre — como rodar o multiplayer

O jogo tem dois arquivos:

- `index.html` — o jogo inteiro (3D, interface, bots).
- `server.py` — o servidor: entrega o jogo e conecta os jogadores (posição, chat, spray, superchat e PvP em tempo real). Só usa o Python, sem instalar nada.

## 1. Jogar no seu computador

Precisa do Python 3.8 ou mais novo. Abra um terminal nesta pasta e rode:

```
python server.py
```

Depois abra **http://localhost:8080** no navegador. Abra uma segunda aba no mesmo endereço para ver dois jogadores ao mesmo tempo.

> Se abrir o `index.html` com dois cliques, o jogo funciona, mas fica **offline** (só com bots). O multiplayer só funciona aberto pelo endereço do servidor.

## 2. Jogar com amigos na mesma rede (Wi-Fi da casa)

1. Rode `python server.py` no seu PC.
2. Descubra o IP do PC: rode `ipconfig` e veja o "Endereço IPv4" (ex.: `192.168.0.15`).
3. Os amigos abrem `http://192.168.0.15:8080` no navegador ou no celular.
4. Se o Windows perguntar sobre o Firewall, permita o Python em "Redes privadas".

## 3. Colocar na internet (qualquer pessoa entra)

**Opção A — Render (link fixo, funciona com o seu PC desligado)**

A pasta já tem tudo o que o Render precisa: `render.yaml` (configuração) e `requirements.txt`.

*Parte 1 — GitHub (onde ficam os arquivos)*

1. Crie uma conta grátis em **github.com** (botão *Sign up*).
2. Clique no **+** no canto de cima → **New repository**.
3. Nome: `avenida-livre`. Marque **Public**. Clique em **Create repository**.
4. Na página que abrir, clique no link **uploading an existing file**.
5. Arraste estes 5 arquivos da pasta `avenida-livre`: `index.html`, `server.py`, `render.yaml`, `requirements.txt` e `LEIA-ME.md`.
6. Clique em **Commit changes**.

*Parte 2 — Render (onde o jogo roda)*

1. Entre em **render.com** → **Get Started** → **GitHub** (entra com a conta que você acabou de criar e autoriza o Render).
2. No painel, clique em **New +** → **Blueprint**.
3. Escolha o repositório `avenida-livre` → **Connect**.
4. O Render lê o `render.yaml` sozinho. Clique em **Apply** (ou **Deploy Blueprint**).
5. Espere uns 2 minutos até aparecer **Live** em verde.
6. Abra o serviço `avenida-livre`: o link do jogo aparece no topo, tipo `https://avenida-livre-xxxx.onrender.com`. É esse link que você manda pros amigos.

*Como funciona o plano grátis*

- Não precisa de cartão de crédito.
- Se ninguém jogar por 15 minutos, o servidor "dorme". O próximo a abrir o link espera uns 30 a 60 segundos e ele acorda sozinho.
- Para nunca dormir: no Render, mude o serviço para o plano **Starter** (cerca de US$ 7 por mês).
- As denúncias (`denuncias.log`) se apagam quando o servidor reinicia no plano grátis.

*Para atualizar o jogo depois*

Suba de novo o arquivo alterado no GitHub (*Add file → Upload files* → *Commit changes*). O Render publica a nova versão sozinho em 1 ou 2 minutos.

**Opção B — túnel rápido a partir do seu PC (para testar com amigos agora)**

Com o servidor rodando, instale o `cloudflared` e rode:

```
cloudflared tunnel --url http://localhost:8080
```

Ele mostra um link `https://....trycloudflare.com` para mandar aos amigos. O link funciona enquanto o seu PC e o comando estiverem ligados.

## O que é sincronizado

| Recurso | Multiplayer real |
|---|---|
| Personagem, time, roupa, bandeira, cartaz, objeto | Sim |
| Posição, animações, veículos | Sim (10 atualizações por segundo) |
| Chat da avenida e do time | Sim (com filtro de palavrões e limite de spam no servidor) |
| Spray no chão | Sim |
| Superchat (fixado, com voz a partir de R$ 10, nos telões a partir de R$ 100) | Sim |
| Anúncios nos telões (leilão: 5 vagas por telão, 2 h garantidas, até 20 dias) | Sim, salvos em `ads/` no servidor |
| PvP entre jogadores | Sim (o servidor confere distância, times e se os dois estão no PvP) |
| Mapas | Cada mapa é uma sala separada |
| Contagem "online" | Jogadores reais conectados |
| Bots, corrida, debates, Crush, missões | Locais (cada jogador tem os seus) |

- Jogadores reais aparecem com a **etiqueta do nome em azul**. Os bots têm etiqueta escura e podem ser desligados em ⚙ Configurações.
- Denúncias feitas pelo cartão de um jogador real ficam salvas em `denuncias.log` no servidor.
- Limite: 300 jogadores por mapa (dá para mudar em `MAX_PER_ROOM` no `server.py`).

## Anúncios e o disco do servidor

Os anúncios ficam na pasta `ads/` do servidor (imagens + `ads.json`). No plano grátis do Render o disco é apagado sempre que o servidor reinicia ou recebe uma atualização, então **os anúncios somem**. Para vender anúncios de verdade, use um plano pago com **Persistent Disk** montado em `ads/`, ou um banco de dados. Não suba a pasta `ads/` para o GitHub.

## Ainda não tem

- **Login e contas:** cada navegador guarda o próprio progresso, e o apelido não é exclusivo.
- **Pagamentos reais:** as compras são de demonstração. O ponto de integração está marcado no `index.html` (procure por "integre o pagamento real aqui").
- **Moderação ativa** (banir, silenciar) além do filtro e do log de denúncias.
