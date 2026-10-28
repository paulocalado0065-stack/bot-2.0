# Loja Neon — painel administrativo por botões

Esta versão prioriza a configuração pelo painel de botões do Discord. Há apenas um comando para abrir o painel: `/painel_loja`. Depois disso, use botões e formulários.

## Instalação Windows
1. Instale Python 3.10+.
2. Extraia o ZIP e abra CMD na pasta.
3. Execute:
   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Copie `.env.example` para `.env` e preencha as credenciais.
5. Crie um bot no https://discord.com/developers/applications e convide-o com os escopos `bot` e `applications.commands`, com permissões View Channels, Send Messages, Embed Links, Attach Files, Read Message History.
6. Rode `python bot.py`.
7. Em outro terminal rode `uvicorn webhook:app --host 0.0.0.0 --port 8000`.

## Uso
No Discord, execute `/painel_loja` uma única vez para abrir o painel. Em seguida, gerencie a loja pelos botões:
- Criar/editar/excluir/visualizar catálogos.
- Adicionar/editar/excluir produtos.
- Alterar estoque.
- Publicar catálogo.
- Ver pedidos.
- Configurar a entrega de cada produto pelo botão **Configurar entrega**: `key` (código único), `link` (URL) ou `text` (texto/instruções).
- Para produtos `key`, use **Adicionar keys** e cole uma chave por linha. As keys disponíveis são contadas automaticamente como estoque; não ajuste manualmente o estoque desses produtos.
Os botões administrativos exigem a permissão **Gerenciar servidor**. Para imagens, esta versão usa URL de imagem (por exemplo, link direto público para um arquivo .png/.jpg). Na compra, o cliente escolhe entre **Link de pagamento** e **Pix QR Code**. Para gerar Pix, o bot pede o e-mail do comprador e mostra o QR Code e o código Pix copia e cola.

## Entrega automática após pagamento
Configure o tipo de entrega de cada produto antes de publicar/vender:
- `key`: cadastre códigos únicos pelo botão **Adicionar keys**. Após a confirmação do pagamento, uma key não usada é reservada para o pedido e enviada por DM.
- `link`: configure uma URL HTTP/HTTPS; ela é enviada por DM depois da confirmação.
- `text`: configure o texto/instruções; ele é enviado por DM depois da confirmação.

A entrega é disparada pelo webhook após consultar o pagamento diretamente na API do Mercado Pago. O serviço do webhook também precisa ter acesso ao mesmo banco SQLite e às variáveis `DISCORD_TOKEN`, `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET` e `DATABASE_PATH`. O cliente precisa permitir mensagens diretas do servidor/bot. Pedidos sem estoque/key ou sem conteúdo de entrega ficam pendentes de revisão; não marque pagamentos manualmente como aprovados.

## Mercado Pago
Configure `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET` e `PUBLIC_BASE_URL` no `.env`. O webhook deve estar hospedado em uma URL HTTPS pública e configurado no painel de desenvolvedores do Mercado Pago para eventos de pagamento. A URL usada ao criar cada pagamento também aponta para `/webhook/mercadopago`. Comece com credenciais de teste. O endpoint verifica a assinatura e consulta o pagamento à API antes de marcar o pedido como pago. Para Pix, o QR Code é dinâmico e exclusivo para cada pedido; não reutilize a imagem/código de outro pedido.

## Hospedagem gratuita
Planos gratuitos mudam, podem suspender serviços e podem não preservar arquivos locais. O bot precisa de um worker contínuo, o webhook precisa de HTTPS público e o banco precisa de armazenamento persistente. Para produção, prefira PostgreSQL com armazenamento persistente, monitore falhas de entrega e implemente processo operacional para pedidos em revisão, reembolsos/chargebacks e reenvio idempotente de entrega. Faça testes de ponta a ponta antes de aceitar pagamentos reais.

## Segurança
Nunca compartilhe token Discord, Access Token do Mercado Pago ou segredo do webhook. Este projeto é uma base inicial; faça testes completos antes de aceitar pagamentos reais.
