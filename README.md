# 7Sense CM Cloud v2.0.3

Atualizações:

- Contadores nos filtros da tela de câmeras.
- Remoção do filtro/status "Retirada" da lista principal.
- Inclusão do status "Em retorno".
- Fluxo de retirada revisado:
  - retirada em campo muda a câmera para "Em retorno";
  - dados da obra e ocorrências permanecem vinculados até a câmera ser recebida na central;
  - no painel, o gestor confirma "Recebida na central";
  - somente então o ciclo é arquivado e a câmera volta para "Aguardando teste".
- Foto da instalação passa a ser obrigatória para ativar a câmera.
- Etiqueta QR mantém o código fixo da câmera, mas exibe cliente e obra atuais quando houver vínculo.

Deploy:
1. Copie os arquivos para a pasta GitHub `7sense-cm-cloud`.
2. Commit: `v2.0.3 fluxo retorno foto obrigatoria`
3. Push origin.
4. Render: Manual Deploy > Clear build cache & deploy.
