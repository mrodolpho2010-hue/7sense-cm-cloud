# 7Sense CM Cloud v1.5 Campo

Versão com fluxo operacional sequencial no módulo `/campo`.

## Novidades

- Etapas do campo em ordem obrigatória:
  1. Em transporte
  2. Chegou na obra
  3. Instalando
  4. Ativar câmera
  5. Retirada
- Etapas concluídas ficam verdes e desabilitadas.
- Somente a próxima etapa fica liberada.
- Registrar problema fica sempre disponível.
- Histórico recente da câmera exibido no celular.

## Deploy no Render

Depois de substituir os arquivos no repositório:

1. Commit: `Fluxo sequencial do campo v1.5`
2. Push origin
3. O Render atualiza automaticamente.

## Links

- Admin: `/login`
- Campo: `/campo`


## v1.5.1
- Corrige fluxo de campo para reiniciar quando câmera estiver com status Em estoque.
- Evita que histórico antigo deixe botões verdes em novos testes ou novas implantações.
