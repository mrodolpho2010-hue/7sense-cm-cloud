# 7Sense Operations Manager v3.0.2

## Links de campo por obra

Incluído:
- Menu Campo agora abre uma lista de clientes/obras/contratos.
- Botão para copiar link de campo específico da obra.
- Botão para copiar mensagem pronta para WhatsApp.
- Novo endpoint `/campo/contrato/<id>`.
- Técnico abre o link da obra, lê o QR Code e registra a movimentação no contexto daquele contrato.
- Se a câmera estiver livre e testada/aprovada, ao iniciar transporte pelo link da obra ela é vinculada ao contrato.
- Se a câmera estiver vinculada a outra obra, o sistema bloqueia a operação para evitar erro.

Mantido:
- Banco Supabase/PostgreSQL.
- Fluxo operacional existente.
- Layout Operations Center.
