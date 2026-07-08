# 7Sense Operations Manager — v3.2.0

## Ciclo de vida dos contratos

Esta versão adiciona a gestão completa de encerramento de contratos.

### Incluído
- Botão **Encerrar contrato** na ficha do contrato.
- Tela de encerramento com data, motivo e observações.
- Validação para impedir encerramento com câmeras vinculadas ou ocorrências abertas.
- Aba/filtro para **Contratos Encerrados**.
- Filtros de contratos: Ativos, Vence em 60d, Vence em 30d, Vencidos e Encerrados.
- Opção para criar novo contrato baseado em contrato encerrado.
- Migração automática dos campos de encerramento no banco.

### Atualização
1. Copie os arquivos para a pasta do GitHub.
2. Commit: `v3.2.0 ciclo vida contratos`.
3. Push origin.
4. Render: Manual Deploy > Clear build cache & deploy.
