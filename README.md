# 7Sense CM Cloud v1.9.6

Correção de ambiente Operação x Demonstração.

## Alterações
- Sistema entra por padrão em **Modo Operação Oficial** no login.
- Modo Demonstração virou ambiente separado, ativado manualmente.
- Dados oficiais e dados demo não se misturam.
- QR Codes de demonstração usam prefixo **7S-DEMO-CAM-***.
- QR Codes oficiais continuam usando **7S-CAM-***.
- Botão **Voltar ao modo operação** retorna ao painel oficial sem apagar dados reais.
- Botão **Reiniciar demonstração** recarrega somente os dados demo.

## Atualização
1. Copie os arquivos desta pasta para o repositório `7sense-cm-cloud`.
2. Commit: `v1.9.6 separacao operacao demonstracao`
3. Push origin.
4. Render: Manual Deploy > Clear build cache & deploy.
