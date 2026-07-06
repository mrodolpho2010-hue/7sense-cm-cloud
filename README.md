# 7Sense CM v2.0 – Release Comercial

## Principais alterações

- Remove o Modo Demonstração da interface principal.
- Dashboard profissional com indicadores:
  - Contratos ativos
  - Câmeras em operação
  - Câmeras disponíveis
  - Ocorrências em aberto
  - Agenda do dia
- Cabeçalho simplificado sem “Modo: Operação Oficial”.
- Menu ⚙️ Configurações com:
  - Meu perfil
  - Alterar senha
  - Gerenciar usuários
  - Limpar banco de dados com confirmação e senha de administrador
  - Sobre o sistema
  - Sair
- Botão “Retirada” no campo só fica liberado após autorização no painel.
- Nova ação no painel da câmera: “Autorizar retirada”.
- Retirada arquiva ocorrências do ciclo atual e limpa vínculo operacional da câmera.
- Mantém Supabase/PostgreSQL e QR Code de campo.

## Atualização

1. Copie os arquivos desta pasta para o repositório `7sense-cm-cloud`.
2. Commit: `v2.0 release comercial`
3. Push origin.
4. Render: `Manual Deploy > Clear build cache & deploy`.
