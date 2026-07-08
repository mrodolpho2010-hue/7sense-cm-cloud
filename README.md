# 7Sense Operations Manager v3.1.1

## Controle de Qualidade e Manutenção

Esta versão adiciona o fluxo completo de teste, reprovação, manutenção e condenação de câmeras.

### Incluído

- Tela **🧪 Testar câmera** com itens aprovados/reprovados individualmente.
- Itens do checklist:
  - Carregada
  - Cartão SD verificado
  - Limpeza realizada
  - Teste de imagem
  - Teste de comunicação
  - Estado físico
- Se algum item for reprovado, é obrigatório informar a observação do problema.
- Se houver reprovação, a câmera vai para **Em manutenção**.
- Tela **🔧 Manutenção** com:
  - últimos registros do histórico;
  - campo para serviços realizados;
  - botão **Manutenção concluída**;
  - botão **Condenar equipamento**.
- Manutenção concluída retorna a câmera para **Aguardando teste**.
- Condenar equipamento envia a câmera para **Inutilizada**, preservando o dossiê.
- Registros entram no histórico/dossiê da câmera.

### Atualização

1. Copie os arquivos desta pasta para a pasta GitHub `7sense-cm-cloud`.
2. Commit: `v3.1.1 controle qualidade manutencao`
3. Push origin.
4. No Render: **Manual Deploy > Clear build cache & deploy**.
